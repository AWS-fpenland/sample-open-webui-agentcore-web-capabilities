# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AiqEngine — drives the upstream NVIDIA AI-Q ``chat_deepresearcher_agent`` workflow
(NeMo Agent Toolkit) in-process on AgentCore Runtime with Amazon Bedrock models.

What is preserved from upstream (unchanged code, pinned commit):
  intent routing (meta / shallow / deep), shallow researcher (bounded, cited),
  clarifier (questions before deep work), deep researcher (source router, planner,
  concurrent researchers, writer), escalation, citation verification + report
  sanitisation, data-source registry, Jinja prompts, budgets.

What this adapter owns:
  Bedrock model wiring (configs/aiq_bedrock.yml), AgentCore-native tools
  (web search via Gateway connector, tenant-filtered Knowledge Base), explicit
  depth control (``preclassified_depth``), turn-based clarification over the
  durable job (the NAT HITL callback is bridged to a pause/resume protocol),
  progress events from NAT intermediate steps, a second deterministic citation
  check against the sources actually retrieved in this job, usage accounting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from typing import Any

import yaml

from . import bedrock_compat
from .contracts import EventType, ResearchMode
from .engine_base import Engine, EngineEvent, EngineRequest
from .run_context import RunContext, reset_run_context, set_run_context

log = logging.getLogger(__name__)

PHASE_LABELS = {
    "intent_classifier": "Classifying intent and depth",
    "clarifier_agent": "Checking whether clarification is needed",
    "shallow_research_agent": "Quick research (bounded, cited)",
    "deep_research_agent": "Deep research (planner → researchers → writer)",
    "chat_deepresearcher_agent": "AI-Q workflow",
}
SKIP_ANSWER = "skip"
MARKER_RE = re.compile(r"\[(\d+)\]")


class ClarificationPending(Exception):
    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


class AiqEngine(Engine):
    name = "aiq"

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.environ.get("AIQ_CONFIG", "/app/configs/aiq_bedrock.yml")
        self._sessions: dict[bool, Any] = {}  # clarifier_enabled -> SessionManager
        self._stacks: dict[bool, contextlib.AsyncExitStack] = {}
        self._lock = asyncio.Lock()
        bedrock_compat.apply()

    # ------------------------------------------------------------------ workflow lifecycle --
    def _variant_config(self, clarifier: bool) -> str:
        with open(self.config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("workflow", {})["enable_clarifier"] = bool(clarifier)
        db = os.environ.get("AIQ_CHECKPOINT_DB", "/app/state/checkpoints.db")
        root, ext = os.path.splitext(db)
        cfg["workflow"]["checkpoint_db"] = f"{root}-{'clarify' if clarifier else 'direct'}{ext or '.db'}"
        os.makedirs(os.path.dirname(cfg["workflow"]["checkpoint_db"]) or ".", exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=f"aiq-{'clarify' if clarifier else 'direct'}-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        return path

    async def _session_manager(self, clarifier: bool):
        if clarifier in self._sessions:
            return self._sessions[clarifier]
        async with self._lock:
            if clarifier in self._sessions:
                return self._sessions[clarifier]
            from nat.runtime.loader import load_workflow

            t0 = time.monotonic()
            path = self._variant_config(clarifier)
            stack = contextlib.AsyncExitStack()
            sm = await stack.enter_async_context(load_workflow(path))
            self._stacks[clarifier] = stack
            self._sessions[clarifier] = sm
            log.info(json.dumps({"event": "workflow.loaded", "clarifier": clarifier, "seconds": round(time.monotonic() - t0, 1)}))
            return sm

    async def warmup(self) -> None:
        await self._session_manager(False)

    # ------------------------------------------------------------------ helpers --
    @staticmethod
    def _query_payload(req: EngineRequest, clarification: list[tuple[str, str]] | None) -> str:
        """AI-Q accepts a JSON query with explicit data_sources; append clarification context the way
        upstream does for its async deep jobs (``## Clarification Context``)."""
        question = req.question
        if clarification:
            lines = ["", "", "## Clarification Context"]
            for q, a in clarification:
                lines.append(f"**Q:** {q}\n**A:** {a}")
            question = question + "\n".join(lines)
        sources = ["web_search"]
        if req.collection:
            sources.append("documents")
        return json.dumps({"query": question, "data_sources": sources})

    @staticmethod
    def _response_text(result: Any) -> str:
        choices = getattr(result, "choices", None)
        if choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                return content
        if isinstance(result, dict):
            return str(result.get("content") or result.get("result") or result)
        return str(result)

    @staticmethod
    def _outcome(result: Any) -> tuple[str, str | None]:
        outcome = getattr(result, "workflow_outcome", None)
        status = getattr(outcome, "status", None) or "success"
        return status, getattr(outcome, "error", None)

    def _verify(self, report: str, ctx: RunContext) -> dict[str, Any]:
        """Deterministic check of the report's citations against sources retrieved in THIS job,
        using upstream's SourceRegistry/verify_citations (pure functions)."""
        from aiq_agent.common.citation_verification import SourceEntry, SourceRegistry, verify_citations

        registry = SourceRegistry()
        for src in ctx.sources.values():
            if src.url:
                registry.add(SourceEntry(url=src.url, title=src.title, citation_key=None, source_type="generic",
                                         tool_name=src.tool))
            else:
                registry.add(SourceEntry(url=None, title=src.title, citation_key=src.title, source_type="knowledge_layer",
                                         tool_name=src.tool))
        res = verify_citations(report, registry)
        by_url = {s.url: s.source_id for s in ctx.sources.values() if s.url}
        citations = []
        for c in res.valid_citations:
            url = c.get("url")
            citations.append({"marker": f"[{c.get('number')}]", "source_id": by_url.get(url), "url": url,
                              "verified": True, "match_level": "exact" if url in by_url else "normalized"})
        for c in res.removed_citations:
            citations.append({"marker": f"[{c.get('number')}]", "source_id": None, "url": c.get("url"),
                              "verified": False, "match_level": "unmatched", "reason": c.get("reason")})
        return {"citations": citations, "verified": len(res.valid_citations), "unverified": len(res.removed_citations),
                "sources_retrieved": len(ctx.sources), "verified_report": res.verified_report}

    # ------------------------------------------------------------------ run --
    async def run(self, req: EngineRequest, cancelled: asyncio.Event) -> AsyncIterator[EngineEvent]:
        from nat.builder.context import ContextState
        from nat.data_models.interactive import HumanResponseText
        from nat.data_models.intermediate_step import IntermediateStepType
        from aiq_agent.agents.chat_researcher.preclassification import preclassified_depth

        mode = req.mode
        clarifier = mode in (ResearchMode.AUTO, ResearchMode.DEEP_CLARIFY)
        depth = {"shallow": "shallow", "deep": "deep", "deep_clarify": "deep"}.get(mode.value)
        # Previously answered clarification turns travel in the job's plan (set by the adapter on resume).
        answered: list[tuple[str, str]] = [(q, a) for q, a in (req.__dict__.get("clarification") or [])]
        answers_iter = iter(a for _, a in answered)
        skip_all = req.approval == "approve" and not req.revision

        queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(etype: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, EngineEvent(EventType(etype), data))

        ctx = RunContext(job_id=req.job_id, tenant_key=req.principal.tenant_key, collection=req.collection,
                         emit=emit, cancelled=cancelled)
        question_holder: asyncio.Future[str] = loop.create_future()
        usage = {"input_tokens": 0, "output_tokens": 0}
        llm_calls = {"n": 0}

        async def user_input_callback(prompt):  # NAT HITL bridge
            text = getattr(getattr(prompt, "content", None), "text", "") or str(prompt)
            if skip_all:
                return HumanResponseText(text=SKIP_ANSWER)
            try:
                return HumanResponseText(text=next(answers_iter))
            except StopIteration:
                if not question_holder.done():
                    question_holder.set_result(text)
                await asyncio.Event().wait()  # park until the engine cancels this run
                return HumanResponseText(text=SKIP_ANSWER)  # pragma: no cover

        def on_step(step) -> None:
            p = getattr(step, "payload", step)
            et = getattr(p, "event_type", None)
            name = getattr(p, "name", None) or ""
            try:
                if et == IntermediateStepType.FUNCTION_START and name in PHASE_LABELS:
                    emit("status", {"description": PHASE_LABELS[name], "done": False, "phase": name})
                elif et == IntermediateStepType.FUNCTION_END and name in PHASE_LABELS and name != "chat_deepresearcher_agent":
                    emit("status", {"description": f"{PHASE_LABELS[name]} — done", "done": True, "phase": name})
                elif et == IntermediateStepType.TOOL_START and name and name not in ("web_search_tool", "knowledge_search"):
                    emit("status", {"description": f"Using {name}", "done": False, "tool": name})
                elif et == IntermediateStepType.LLM_START:
                    llm_calls["n"] += 1
                elif et == IntermediateStepType.LLM_END:
                    ui = getattr(p, "usage_info", None)
                    tu = getattr(ui, "token_usage", None) if ui else None
                    if tu is not None:
                        usage["input_tokens"] += int(getattr(tu, "prompt_tokens", 0) or 0)
                        usage["output_tokens"] += int(getattr(tu, "completion_tokens", 0) or 0)
            except Exception as e:  # noqa: BLE001 — never let telemetry break the run
                log.debug("on_step error: %s", e)

        yield EngineEvent(EventType.ROUTE, {"requested_mode": mode.value, "depth": depth or "auto",
                                            "clarifier": clarifier, "collection": req.collection})
        sm = await self._session_manager(clarifier)
        token = set_run_context(ctx)
        result_holder: dict[str, Any] = {}

        async def run_workflow() -> None:
            async with sm.session(user_input_callback=user_input_callback) as session:
                ContextState.get().conversation_id.set(req.job_id)
                with preclassified_depth(depth):
                    async with session.run(self._query_payload(req, answered)) as runner:
                        sub = runner.context.intermediate_step_manager.subscribe(on_next=on_step)
                        try:
                            result_holder["result"] = await runner.result()
                        finally:
                            with contextlib.suppress(Exception):
                                sub.unsubscribe()

        task = loop.create_task(run_workflow())
        started = time.monotonic()
        try:
            while True:
                if cancelled.is_set() and not task.done():
                    task.cancel()
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield ev
                    continue
                except asyncio.TimeoutError:
                    pass
                if question_holder.done() and not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
                    question = question_holder.result()
                    while not queue.empty():
                        yield queue.get_nowait()
                    yield EngineEvent(EventType.CLARIFICATION, {"question": question, "turn": len(answered) + 1,
                                                                "answered": [{"q": q, "a": a} for q, a in answered]})
                    yield EngineEvent(EventType.PLAN_APPROVAL_REQUIRED,
                                      {"prompt": "Reply with your answer, **approve** to proceed as-is, or **cancel**."})
                    return
                if task.done():
                    break
            while not queue.empty():
                yield queue.get_nowait()
            if task.cancelled() or cancelled.is_set():
                yield EngineEvent(EventType.CANCELLED, {"reason": "cancel_requested", "seconds": round(time.monotonic() - started, 1)})
                return
            exc = task.exception()
            if exc is not None:
                raise exc
            result = result_holder.get("result")
            text = self._response_text(result)
            status, error = self._outcome(result)
            if status != "success":
                yield EngineEvent(EventType.ERROR, {"error": {"code": "internal", "message": (error or text)[:800],
                                                              "retryable": True}, "terminal": True,
                                                    "workflow_outcome": status})
                return
            verification = self._verify(text, ctx) if ctx.sources else {"citations": [], "verified": 0, "unverified": 0,
                                                                          "sources_retrieved": 0, "verified_report": text}
            final_text = verification.pop("verified_report") or text
            yield EngineEvent(EventType.CITATIONS, verification)
            yield EngineEvent(EventType.REPORT, {"text": final_text, "sources": [s.model_dump() for s in ctx.sources.values()],
                                                 "mode": mode.value, "depth": depth})
            yield EngineEvent(EventType.USAGE, {**usage, "llm_calls": llm_calls["n"], **ctx.counters,
                                                "seconds": round(time.monotonic() - started, 1)})
            yield EngineEvent(EventType.COMPLETED, {"status": "completed"})
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            reset_run_context(token)

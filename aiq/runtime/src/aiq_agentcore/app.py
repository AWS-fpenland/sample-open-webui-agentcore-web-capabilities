# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Runtime entrypoint for AI-Q on AgentCore.

Contract (HTTP protocol runtime):
  POST /invocations  body = InvokeRequest (contracts.py)  → streamed JSON lines (one Event per line)
  GET  /ping         → {"status": "Healthy" | "HealthyBusy"}

Operations
  chat     synchronous streamed turn: runs the engine and streams its events; also
           journals them under a job so the pipe can replay if the stream drops.
  submit   starts a deep-research job as an in-session async task and returns
           immediately with {job_id, seq}. The session stays HealthyBusy until done.
  events   replays journal events with seq > after, then tails until terminal.
  status   returns the JobRecord.
  cancel   sets cancel_requested; the running task observes it cooperatively.
  approve  resumes a job waiting for plan approval (approve|revise|reject).
  ingest / collections / delete_collection  document collection management.

Identity: every operation derives the tenant from the verified Cognito access
token in the forwarded Authorization header. There is no owner field in requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from . import __version__, guardrails, metrics, modellab_runtime, packages
from .contracts import (
    ApiError,
    ErrorCode,
    Event,
    EventType,
    InvokeRequest,
    JobStatus,
    Op,
    Principal,
    ResearchMode,
)
from .engine_base import Engine, EngineRequest, MockEngine
from .identity import AuthError, principal_from_request
from .store import JobStore, now_iso

logging.basicConfig(
    level=os.environ.get("AIQ_LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("aiq_agentcore.app")

app = BedrockAgentCoreApp()
_store: JobStore | None = None
_engine: Engine | None = None
_tasks: dict[str, asyncio.Task] = {}
_cancel_flags: dict[str, asyncio.Event] = {}


def jlog(level: int, **kv: Any) -> None:
    kv.setdefault("run_id", os.environ.get("AIQ_RUN_ID"))
    log.log(level, json.dumps(kv, default=str))


def store() -> JobStore:
    global _store
    if _store is None:
        _store = JobStore(region=os.environ.get("AIQ_REGION"), ttl_days=int(os.environ.get("AIQ_RETENTION_DAYS", "30")))
    return _store


def engine() -> Engine:
    global _engine
    if _engine is None:
        kind = os.environ.get("AIQ_ENGINE", "aiq")
        if kind == "mock":
            _engine = MockEngine()
        else:
            from .engine_aiq import AiqEngine  # heavy import (NAT); only when selected

            _engine = AiqEngine()
        jlog(logging.INFO, event="engine.selected", engine=_engine.name)
    return _engine


def _err(code: ErrorCode, message: str, retryable: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "type": EventType.ERROR.value,
        "seq": -1,
        "data": {"error": ApiError(code=code, message=message, retryable=retryable).model_dump(), "terminal": True, **extra},
    }


def _line(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False) + "\n"


def _principal(context) -> Principal:
    headers = getattr(context, "request_headers", None) or {}
    return principal_from_request(dict(headers))


# --------------------------------------------------------------------------- core run loop --


def _refresh_index_status(st: JobStore, tenant: str, job_id: str, status: JobStatus) -> None:
    """Keep the PKG# library row's status in step with the job (queued → running → terminal); never fails the run."""
    try:
        st.update_package_index(tenant, job_id, status=status.value)
    except Exception:  # noqa: BLE001 — rows created with index=False, or a transient DynamoDB error
        log.debug("package index status refresh skipped", exc_info=True)


async def _journal_run(principal: Principal, job_id: str, req: EngineRequest) -> AsyncIterator[Event]:
    """Run the engine for a job, appending every event to the durable journal and yielding it."""
    st = store()
    tenant = principal.tenant_key
    cancelled = _cancel_flags.setdefault(job_id, asyncio.Event())
    st.update_job(tenant, job_id, status=JobStatus.RUNNING.value, heartbeat_at=now_iso())
    _refresh_index_status(st, tenant, job_id, JobStatus.RUNNING)  # the library row says "running", not "queued", in flight
    started = time.monotonic()
    last_cancel_check = 0.0
    terminal_seen = False
    outcome = "completed"
    usage_seen: dict[str, Any] = {}
    cit_ok = cit_bad = n_sources = 0
    finalized = False
    loop = asyncio.get_running_loop()

    async def heartbeat() -> None:  # keeps heartbeat_at fresh so the reaper never mistakes a live job for a dead one
        while True:
            await asyncio.sleep(30)
            try:
                await loop.run_in_executor(None, st.heartbeat, tenant, job_id)
            except Exception as e:  # noqa: BLE001
                jlog(logging.WARNING, event="heartbeat.failed", job_id=job_id, error=repr(e)[:200])

    hb_task = loop.create_task(heartbeat())
    try:
        # INPUT guardrail (opt-in): the question is checked before any model or tool call.
        gr_in = await loop.run_in_executor(None, guardrails.apply, req.question, "INPUT")
        if gr_in.action not in ("DISABLED", "NONE"):
            yield st.append_event(
                tenant,
                job_id,
                EventType.GUARDRAIL,
                {
                    "source": "INPUT",
                    "action": gr_in.action,
                    "mode": gr_in.mode,
                    "enforced": gr_in.enforced,
                    "reasons": gr_in.reasons,
                },
            )
            metrics.emit_guardrail_metric(source="INPUT", action=gr_in.action, mode=gr_in.mode)
        if gr_in.blocked or (gr_in.action == "ERROR" and gr_in.enforced):
            code = ErrorCode.INVALID_REQUEST if gr_in.blocked else ErrorCode.INTERNAL
            msg = gr_in.text if gr_in.blocked else "content policy check unavailable; request not processed"
            st.update_job(tenant, job_id, status=JobStatus.FAILED.value, error=msg[:500])
            yield st.append_event(
                tenant,
                job_id,
                EventType.ERROR,
                {
                    "error": ApiError(code=code, message=msg[:500], retryable=not gr_in.blocked).model_dump(),
                    "terminal": True,
                    "guardrail": True,
                },
            )
            outcome = "blocked"
            terminal_seen = True
            return
        async for ev in engine().run(req, cancelled):
            # Cooperative cancellation: poll the durable flag at most every second.
            now = time.monotonic()
            if now - last_cancel_check > 1:
                last_cancel_check = now
                if await asyncio.get_running_loop().run_in_executor(None, st.cancel_requested, tenant, job_id):
                    cancelled.set()
            if ev.type == EventType.ROUTE:  # the models in use travel with the route decision (ADR-23), engine-agnostic
                ev.data.setdefault("models", req.models or {})
            if ev.type == EventType.CLARIFICATION:
                rec_now = st.get_job(tenant, job_id)
                prev = dict(rec_now.plan) if rec_now and rec_now.plan else {}
                st.update_job(
                    tenant,
                    job_id,
                    plan={**prev, "pending_question": ev.data.get("question"), "clarification": ev.data.get("answered", [])},
                )
            if ev.type == EventType.PLAN_APPROVAL_REQUIRED:
                st.update_job(tenant, job_id, status=JobStatus.CLARIFYING.value)
            if ev.type == EventType.REPORT and ev.data.get("text"):
                # OUTPUT guardrail (opt-in) on the final report before it is stored or streamed.
                gr_out = await loop.run_in_executor(None, guardrails.apply, ev.data["text"], "OUTPUT")
                if gr_out.action not in ("DISABLED", "NONE"):
                    st.append_event(
                        tenant,
                        job_id,
                        EventType.GUARDRAIL,
                        {
                            "source": "OUTPUT",
                            "action": gr_out.action,
                            "mode": gr_out.mode,
                            "enforced": gr_out.enforced,
                            "reasons": gr_out.reasons,
                        },
                    )
                    metrics.emit_guardrail_metric(source="OUTPUT", action=gr_out.action, mode=gr_out.mode)
                    if gr_out.enforced and (gr_out.blocked or gr_out.action == "MODIFIED"):
                        ev.data["text"] = gr_out.text
                        ev.data["guardrail_action"] = gr_out.action
                key = st.package_prefix(tenant, job_id) + "report.md"
                st.put_text(key, ev.data["text"])
                ev.data["report_key"] = key
                n_sources = len(ev.data.get("sources") or [])
                st.update_job(tenant, job_id, report_key=key)
            if ev.type == EventType.CITATIONS:
                key = st.package_prefix(tenant, job_id) + "ledger.json"
                st.put_json(key, ev.data)
                ev.data["ledger_key"] = key
                cit_ok, cit_bad = int(ev.data.get("verified", 0) or 0), int(ev.data.get("unverified", 0) or 0)
                st.update_job(tenant, job_id, ledger_key=key)
            if ev.type == EventType.USAGE:
                usage_seen = {k: int(v) for k, v in ev.data.items() if isinstance(v, (int, float))}
                st.update_job(tenant, job_id, usage=usage_seen)
            is_terminal = ev.type in (EventType.COMPLETED, EventType.CANCELLED) or (
                ev.type == EventType.ERROR and ev.data.get("terminal")
            )
            final_status: JobStatus | None = None
            final_error: str | None = None
            if is_terminal:
                # ADR-21: the Research Package is finalised BEFORE the terminal event is journaled, so every tail (pipe,
                # workbench) receives the `package` summary and then the terminal marker, in that order — and the job
                # record flips to its terminal status only AFTER the terminal marker exists (status terminal ⇒ journal complete).
                if ev.type == EventType.COMPLETED:
                    final_status = JobStatus.COMPLETED
                elif ev.type == EventType.CANCELLED:
                    final_status, outcome = JobStatus.CANCELLED, "cancelled"
                else:
                    err = ev.data.get("error") or {}
                    final_status, outcome = JobStatus.FAILED, "failed"
                    final_error = str(err.get("message", ""))[:1000]
                pkg_ev = await _finalize_package(st, tenant, job_id, emit=True, status=final_status, error=final_error)
                if pkg_ev is not None:
                    yield pkg_ev
                finalized = True
                terminal_seen = True
            stored = await asyncio.get_running_loop().run_in_executor(None, st.append_event, tenant, job_id, ev.type, ev.data)
            if final_status is not None:
                st.update_job(tenant, job_id, status=final_status.value, **({"error": final_error} if final_error else {}))
                _refresh_index_status(st, tenant, job_id, final_status)
            yield stored
            if terminal_seen:
                break
        if not terminal_seen:
            rec = st.get_job(tenant, job_id)
            if rec and rec.status == JobStatus.CLARIFYING:
                outcome = "clarifying"
                return  # waiting on the user; not terminal
            if cancelled.is_set():
                outcome = "cancelled"
                end_status, terminal_type, terminal_data = (
                    JobStatus.CANCELLED,
                    EventType.CANCELLED,
                    {"reason": "cancel_requested"},
                )
            else:
                end_status, terminal_type = JobStatus.COMPLETED, EventType.COMPLETED
                terminal_data = {"status": "completed", "note": "engine ended without explicit completion"}
            # ADR-21 holds on this path too: package summary first, terminal marker last, then the status flip.
            pkg_ev = await _finalize_package(st, tenant, job_id, emit=True, status=end_status)
            if pkg_ev is not None:
                yield pkg_ev
            finalized = True
            stored = st.append_event(tenant, job_id, terminal_type, terminal_data)
            st.update_job(tenant, job_id, status=end_status.value)
            _refresh_index_status(st, tenant, job_id, end_status)
            yield stored
    except asyncio.CancelledError:
        outcome = "cancelled"
        st.update_job(tenant, job_id, status=JobStatus.CANCELLED.value)
        yield st.append_event(tenant, job_id, EventType.CANCELLED, {"reason": "task_cancelled"})
        raise
    except Exception as e:  # noqa: BLE001 — surface, never hide, engine failures
        outcome = "failed"
        jlog(logging.ERROR, event="job.failed", job_id=job_id, error=repr(e)[:500])
        st.update_job(tenant, job_id, status=JobStatus.FAILED.value, error=repr(e)[:1000])
        yield st.append_event(
            tenant,
            job_id,
            EventType.ERROR,
            {"error": ApiError(code=ErrorCode.INTERNAL, message=str(e)[:500], retryable=True).model_dump(), "terminal": True},
        )
    finally:
        hb_task.cancel()
        _cancel_flags.pop(job_id, None)
        secs = round(time.monotonic() - started, 2)
        jlog(logging.INFO, event="job.finished", job_id=job_id, seconds=secs, outcome=outcome)
        if not finalized:  # clarifying pause, cancelled-by-task or engine exception: refresh the package row silently
            await _finalize_package(st, tenant, job_id, emit=False)
        if outcome != "clarifying":
            try:
                metrics.emit_job_metrics(
                    mode=req.mode.value,
                    outcome=outcome,
                    seconds=secs,
                    usage=usage_seen,
                    citations_verified=cit_ok,
                    citations_unverified=cit_bad,
                    sources=n_sources,
                )
            except Exception as e:  # noqa: BLE001
                jlog(logging.WARNING, event="metrics.failed", error=repr(e)[:200])


def _artifact_publisher(principal: Principal, job_id: str):
    """ADR-27: called by the sandbox provider as artifacts appear (from a worker thread) — S3 + journal event now."""
    st = store()
    tenant = principal.tenant_key

    def publish(name: str, data: bytes, kind: str) -> dict[str, Any] | None:
        import base64 as _b64

        safe = os.path.basename(name).replace(" ", "_")[:120] or "artifact"
        key = st.package_prefix(tenant, job_id) + f"artifacts/{safe}"
        rec = packages.new_artifact_record(job_id, safe, data, kind, key, phase="checkpoint", sandbox_path=name)
        # dedupe by content: the same file re-checkpointed after an edit gets a new record; identical bytes do not
        for ev in st.read_events(job_id, after=0, limit=2000):
            if ev.type == EventType.ARTIFACT and (ev.data.get("record") or {}).get("sha256") == rec["sha256"]:
                return ev.data["record"]
        st.put_bytes(key, data, rec["mime_type"])
        payload: dict[str, Any] = {
            "name": safe,
            "kind": rec["kind"],
            "size_bytes": len(data),
            "s3_key": key,
            "record": rec,
            "artifact_id": rec["artifact_id"],
        }
        if rec["mime_type"].startswith("image/") and len(data) <= 1_000_000:
            payload["markdown"] = f"![{safe}](data:{rec['mime_type']};base64,{_b64.b64encode(data).decode()})"
        elif rec["kind"] in ("dataset", "text", "document") and len(data) <= 20_000:
            fence = "csv" if safe.lower().endswith(".csv") else ""
            payload["markdown"] = f"**{safe}**\n\n```{fence}\n{data.decode('utf-8', 'replace')[:20000]}\n```"
        st.append_event(tenant, job_id, EventType.ARTIFACT, payload)
        return rec

    return publish


def _resolve_models(principal: Principal, body: InvokeRequest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(resolved roles, error event) — the request is refused when a chosen model is not offered (ADR-23)."""
    st = store()
    mx = modellab_runtime.load_matrix(st)
    prefs = st.get_prefs(principal.tenant_key)
    req_models = {r: c.model_dump() for r, c in (body.models or {}).items()} or None
    roles, problems = modellab_runtime.resolve(mx, req_models, prefs)
    if problems:
        msg = "; ".join(f"{p['role']}: {p['model_id']} — {p['reason']}" for p in problems)
        return None, _err(ErrorCode.INVALID_REQUEST, f"model selection refused: {msg}"[:900], problems=problems)
    return roles, None


async def _finalize_package(
    st: JobStore, tenant: str, job_id: str, *, emit: bool, status: JobStatus | None = None, error: str | None = None
) -> Event | None:
    """Build/refresh the manifest + PKG# row; optionally return a journaled `package` summary event (ADR-21).

    `status`/`error` let the caller finalize with the *intended* terminal state before the job record flips, so that
    "status is terminal" always implies "the journal is complete" (package summary, then the terminal marker)."""
    try:
        rec_final = st.get_job(tenant, job_id)
        if rec_final is None:
            return None
        if status is not None:
            rec_final = rec_final.model_copy(update={"status": status, **({"error": error} if error else {})})
        arts = [
            e.data.get("record")
            for e in st.read_events(job_id, after=0, limit=2000)
            if e.type == EventType.ARTIFACT and e.data.get("record")
        ]
        loop = asyncio.get_running_loop()
        manifest = await loop.run_in_executor(
            None,
            lambda: packages.finalize(
                st,
                rec_final,
                matrix=modellab_runtime.load_matrix(st),
                runtime_version=os.environ.get("AIQ_RUNTIME_VERSION"),
                artifacts=arts,
            ),
        )
        if emit:
            return st.append_event(
                tenant, job_id, EventType.PACKAGE, packages.summary_of(manifest, st.index_for_job(tenant, job_id))
            )
    except Exception as e:  # noqa: BLE001 — a package refresh must never mask the run outcome
        jlog(logging.WARNING, event="package.finalize.failed", job_id=job_id, error=repr(e)[:300])
    return None


async def _background_job(principal: Principal, job_id: str, req: EngineRequest) -> None:
    async for _ in _journal_run(principal, job_id, req):
        pass


def _start_background(principal: Principal, job_id: str, req: EngineRequest) -> None:
    """Run the job as an in-session async task; the runtime reports HealthyBusy meanwhile."""
    task_id = app.add_async_task(f"aiq-job-{job_id}")

    async def runner() -> None:
        try:
            await _background_job(principal, job_id, req)
        finally:
            app.complete_async_task(task_id)
            _tasks.pop(job_id, None)

    _tasks[job_id] = asyncio.get_running_loop().create_task(runner())


# --------------------------------------------------------------------------- operations --


async def _op_chat(principal: Principal, body: InvokeRequest, session_id: str | None) -> AsyncIterator[str]:
    st = store()
    roles, err = _resolve_models(principal, body)
    if err:
        yield _line(err)
        return
    rec, created = st.create_job(
        tenant_key=principal.tenant_key,
        mode=body.mode,
        question=body.messages[-1].content if body.messages else "",
        runtime_session_id=session_id,
        client_request_id=body.client_request_id,
        conversation_id=body.conversation_id,
        models=roles,
        parent_job_id=body.parent_job_id or body.active_report_job_id,
        relation=body.relation or ("ask" if body.active_report_job_id else None),
    )
    if not created and rec.last_seq > 0:
        # Duplicate submit (retry after a dropped stream): replay instead of re-running the tools.
        yield _line(
            {
                "type": EventType.JOB_ACCEPTED.value,
                "seq": 0,
                "job_id": rec.job_id,
                "data": {"replayed": True, "status": rec.status.value},
            }
        )
        async for ev in st.tail_events(principal.tenant_key, rec.job_id, after=0):
            yield _line(ev.model_dump(mode="json"))
        return
    yield _line(
        {
            "type": EventType.JOB_ACCEPTED.value,
            "seq": 0,
            "job_id": rec.job_id,
            "data": {"mode": body.mode.value, "created": created},
        }
    )
    req = EngineRequest(
        principal=principal,
        mode=body.mode,
        messages=body.messages,
        job_id=rec.job_id,
        collection=body.collection,
        approval=body.approval,
        revision=body.revision,
        data_sources=body.data_sources,
        active_report_job_id=body.active_report_job_id,
        models=rec.models,
        publish_artifact=_artifact_publisher(principal, rec.job_id),
    )
    async for ev in _journal_run(principal, rec.job_id, req):
        yield _line(ev.model_dump(mode="json"))


async def _op_submit(principal: Principal, body: InvokeRequest, session_id: str | None) -> AsyncIterator[str]:
    st = store()
    mode = body.mode if body.mode in (ResearchMode.DEEP, ResearchMode.DEEP_CLARIFY) else ResearchMode.DEEP
    roles, err = _resolve_models(principal, body)
    if err:
        yield _line(err)
        return
    rec, created = st.create_job(
        tenant_key=principal.tenant_key,
        mode=mode,
        question=body.messages[-1].content if body.messages else "",
        runtime_session_id=session_id,
        client_request_id=body.client_request_id,
        conversation_id=body.conversation_id,
        models=roles,
        parent_job_id=body.parent_job_id,
        relation=body.relation,
    )
    if created:
        req = EngineRequest(
            principal=principal,
            mode=mode,
            messages=body.messages,
            job_id=rec.job_id,
            collection=body.collection,
            data_sources=body.data_sources,
            active_report_job_id=body.active_report_job_id,
            models=rec.models,
            publish_artifact=_artifact_publisher(principal, rec.job_id),
        )
        _start_background(principal, rec.job_id, req)
    yield _line(
        {
            "type": EventType.JOB_ACCEPTED.value,
            "seq": 0,
            "job_id": rec.job_id,
            "data": {
                "mode": mode.value,
                "created": created,
                "status": rec.status.value,
                "models": rec.models,
                "parent_job_id": rec.parent_job_id,
                "relation": rec.relation,
            },
        }
    )


async def _op_events(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = store()
    if not body.job_id:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "job_id is required"))
        return
    rec = st.get_job(principal.tenant_key, body.job_id)
    if rec is None:  # includes jobs owned by other tenants — indistinguishable by design
        yield _line(_err(ErrorCode.NOT_FOUND, "job not found"))
        return
    if not body.tail:
        for ev in st.read_events(body.job_id, after=body.after):
            yield _line(ev.model_dump(mode="json"))
        return
    async for ev in st.tail_events(principal.tenant_key, body.job_id, after=body.after, idle_timeout=3300):
        yield _line(ev.model_dump(mode="json"))


async def _op_status(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    if not body.job_id:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "job_id is required"))
        return
    rec = store().get_job(principal.tenant_key, body.job_id)
    if rec is None:
        yield _line(_err(ErrorCode.NOT_FOUND, "job not found"))
        return
    yield _line({"type": "job.status", "seq": rec.last_seq, "job_id": rec.job_id, "data": rec.model_dump(mode="json")})


async def _op_cancel(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    if not body.job_id:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "job_id is required"))
        return
    rec = store().request_cancel(principal.tenant_key, body.job_id)
    if rec is None:
        yield _line(_err(ErrorCode.NOT_FOUND, "job not found"))
        return
    flag = _cancel_flags.get(body.job_id)
    if flag is not None:
        flag.set()
    task = _tasks.get(body.job_id)
    if task is not None and not task.done():
        # The engine may be mid model-call; give cooperative cancel 20 s, then hard-cancel the task.
        async def hard_cancel() -> None:
            await asyncio.sleep(20)
            if not task.done():
                task.cancel()

        asyncio.get_running_loop().create_task(hard_cancel())
    yield _line({"type": "job.status", "seq": rec.last_seq, "job_id": rec.job_id, "data": rec.model_dump(mode="json")})


async def _op_approve(principal: Principal, body: InvokeRequest, session_id: str | None) -> AsyncIterator[str]:
    st = store()
    if not body.job_id or not body.approval:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "job_id and approval are required"))
        return
    rec = st.get_job(principal.tenant_key, body.job_id)
    if rec is None:
        yield _line(_err(ErrorCode.NOT_FOUND, "job not found"))
        return
    if rec.status != JobStatus.CLARIFYING:
        yield _line(_err(ErrorCode.INVALID_REQUEST, f"job is {rec.status.value}, not awaiting approval"))
        return
    if body.approval == "reject":
        st.update_job(principal.tenant_key, rec.job_id, status=JobStatus.CANCELLED.value)
        yield _line(
            st.append_event(principal.tenant_key, rec.job_id, EventType.CANCELLED, {"reason": "plan_rejected"}).model_dump(
                mode="json"
            )
        )
        return
    # Clarification transcript: previous Q/A pairs from the plan + this turn's answer (revision) for the last question.
    plan = dict(rec.plan or {})
    qa: list[tuple[str, str]] = [(x.get("q", ""), x.get("a", "")) for x in plan.get("clarification", []) if x.get("a")]
    last_q = plan.get("pending_question")
    if last_q is None:
        for ev in reversed(st.read_events(rec.job_id, after=0, limit=1000)):
            if ev.type == EventType.CLARIFICATION:
                last_q = ev.data.get("question")
                break
    if last_q:
        qa.append((last_q, body.revision or "skip"))
    st.update_job(
        principal.tenant_key,
        rec.job_id,
        plan={"clarification": [{"q": q, "a": a} for q, a in qa]},
        status=JobStatus.RUNNING.value,
    )
    from .contracts import ChatMessage

    req = EngineRequest(
        principal=principal,
        mode=rec.mode,
        messages=body.messages or [ChatMessage(role="user", content=rec.question)],
        job_id=rec.job_id,
        collection=body.collection,
        approval=body.approval,
        revision=body.revision,
        data_sources=body.data_sources,
        clarification=qa,
        models=rec.models,
        publish_artifact=_artifact_publisher(principal, rec.job_id),
    )
    _start_background(principal, rec.job_id, req)
    yield _line(
        {
            "type": EventType.JOB_ACCEPTED.value,
            "seq": rec.last_seq,
            "job_id": rec.job_id,
            "data": {"resumed": True, "approval": body.approval},
        }
    )


async def _op_ingest(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    from .knowledge import ingest_documents

    if not body.collection:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "collection is required"))
        return
    if not body.documents:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "documents[] is required"))
        return
    async for item in ingest_documents(store(), principal, body.collection, body.documents):
        yield _line(item)


async def _op_collections(principal: Principal) -> AsyncIterator[str]:
    docs = store().list_documents(principal.tenant_key)
    by_coll: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        by_coll.setdefault(d["collection"], []).append({k: v for k, v in d.items() if k != "collection"})
    yield _line(
        {
            "type": "collections",
            "seq": 0,
            "data": {"collections": [{"name": k, "documents": v} for k, v in sorted(by_coll.items())]},
        }
    )


async def _op_delete_collection(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    from .knowledge import start_sync

    if not body.collection:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "collection is required"))
        return
    n = store().delete_collection(principal.tenant_key, body.collection)
    sync = start_sync()
    yield _line(
        {
            "type": "collection.deleted",
            "seq": 0,
            "data": {"collection": body.collection, "objects_deleted": n, "ingestion_job": sync},
        }
    )


# --------------------------------------------------------------------------- entrypoint --


@app.entrypoint
async def invoke(payload: dict[str, Any], context) -> AsyncIterator[str]:
    session_id = getattr(context, "session_id", None)
    request_id = str(uuid.uuid4())
    t0 = time.monotonic()
    try:
        principal = _principal(context)
    except AuthError as e:
        jlog(logging.WARNING, event="auth.rejected", code=e.error.code.value, request_id=request_id)
        yield _line(_err(e.error.code, e.error.message, request_id=request_id))
        return
    try:
        body = InvokeRequest.model_validate(payload)
    except ValidationError as e:
        yield _line(
            _err(
                ErrorCode.INVALID_REQUEST,
                e.errors()[0].get("msg", "invalid request")[:300],
                request_id=request_id,
                field=".".join(str(x) for x in e.errors()[0].get("loc", [])),
            )
        )
        return
    jlog(
        logging.INFO,
        event="invoke",
        op=body.op.value,
        mode=body.mode.value,
        tenant=principal.tenant_key,
        job_id=body.job_id,
        session=session_id,
        request_id=request_id,
    )
    try:
        if body.op == Op.HEALTH:
            yield _line(
                {
                    "type": "health",
                    "seq": 0,
                    "data": {
                        "guardrail": guardrails.describe(),
                        "version": __version__,
                        "engine": os.environ.get("AIQ_ENGINE", "aiq"),
                        "source_commit": os.environ.get("AIQ_SOURCE_COMMIT"),
                        "upstream_ref": os.environ.get("AIQ_UPSTREAM_REF"),
                        "tenant": principal.tenant_key,
                        "active_jobs": len(_tasks),
                        "matrix": modellab_runtime.matrix_status() if modellab_runtime.load_matrix(store()) or True else None,
                        "runtime_version": os.environ.get("AIQ_RUNTIME_VERSION"),
                    },
                }
            )
        elif body.op == Op.CHAT:
            async for line in _op_chat(principal, body, session_id):
                yield line
        elif body.op == Op.SUBMIT:
            async for line in _op_submit(principal, body, session_id):
                yield line
        elif body.op == Op.EVENTS:
            async for line in _op_events(principal, body):
                yield line
        elif body.op == Op.STATUS:
            async for line in _op_status(principal, body):
                yield line
        elif body.op == Op.CANCEL:
            async for line in _op_cancel(principal, body):
                yield line
        elif body.op == Op.APPROVE:
            async for line in _op_approve(principal, body, session_id):
                yield line
        elif body.op == Op.INGEST:
            async for line in _op_ingest(principal, body):
                yield line
        elif body.op == Op.COLLECTIONS:
            async for line in _op_collections(principal):
                yield line
        elif body.op == Op.DELETE_COLLECTION:
            async for line in _op_delete_collection(principal, body):
                yield line
        else:
            from . import ops_phase3

            async for line in ops_phase3.dispatch(
                principal, body, session_id, _start_background, _artifact_publisher, _resolve_models
            ):
                yield line
    except Exception as e:  # noqa: BLE001
        jlog(logging.ERROR, event="invoke.failed", op=body.op.value, error=repr(e)[:500], request_id=request_id)
        yield _line(_err(ErrorCode.INTERNAL, "internal error; see runtime logs", retryable=True, request_id=request_id))
    finally:
        jlog(logging.INFO, event="invoke.done", op=body.op.value, request_id=request_id, ms=int((time.monotonic() - t0) * 1000))


if __name__ == "__main__":
    app.run()

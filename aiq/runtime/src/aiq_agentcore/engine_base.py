# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Engine interface: the boundary between the AgentCore adapter and AI-Q.

An engine turns a research request into a stream of ``EngineEvent``s. The
adapter (``app.py``) owns identity, job records, the durable journal, cancel
checks and streaming to the caller; engines own reasoning, tools and prose.

Two engines exist:
* ``MockEngine`` — deterministic, no model calls; used for plumbing tests and
  for the very first deployment so identity/jobs/replay/cancel can be proven
  independently of the research quality.
* ``AiqEngine`` (engine_aiq.py) — drives the upstream AI-Q NAT workflow with
  Amazon Bedrock models and AgentCore-native tools.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .contracts import ChatMessage, EventType, Principal, ResearchMode, Source


@dataclass
class EngineEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineRequest:
    principal: Principal
    mode: ResearchMode
    messages: list[ChatMessage]
    job_id: str
    collection: str | None = None
    approval: str | None = None
    revision: str | None = None
    data_sources: list[str] | None = None
    active_report_job_id: str | None = None
    clarification: list[tuple[str, str]] | None = None

    @property
    def question(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        return ""


class Engine:
    name = "base"

    async def run(self, req: EngineRequest, cancelled: asyncio.Event) -> AsyncIterator[EngineEvent]:  # pragma: no cover
        raise NotImplementedError
        yield  # noqa: unreachable — makes this an async generator for type checkers


class MockEngine(Engine):
    """Deterministic engine that exercises every event type without any model or tool call."""

    name = "mock"

    async def run(self, req: EngineRequest, cancelled: asyncio.Event) -> AsyncIterator[EngineEvent]:
        q = req.question.strip()
        deep = req.mode in (ResearchMode.DEEP, ResearchMode.DEEP_CLARIFY) or (
            req.mode == ResearchMode.AUTO and len(q.split()) > 12
        )
        yield EngineEvent(EventType.ROUTE, {"mode": "deep" if deep else "shallow", "reason": "mock router (word count)",
                                            "requested_mode": req.mode.value})
        if req.mode == ResearchMode.DEEP_CLARIFY and req.approval is None:
            yield EngineEvent(EventType.PLAN, {"steps": ["Define scope", "Gather sources", "Synthesize"],
                                               "question": q})
            yield EngineEvent(EventType.PLAN_APPROVAL_REQUIRED, {"prompt": "Approve this research plan?"})
            return
        src = Source(source_id=Source.make_id("https://example.com/mock"), url="https://example.com/mock",
                     title="Mock source", kind="web_search", retrieved_at="2026-01-01T00:00:00Z", tool="mock",
                     snippet="A deterministic snippet.")
        steps = 6 if deep else 2
        for i in range(steps):
            if cancelled.is_set():
                yield EngineEvent(EventType.CANCELLED, {"at_step": i})
                return
            yield EngineEvent(EventType.STATUS, {"description": f"mock step {i + 1}/{steps}", "done": False})
            if i == 0:
                yield EngineEvent(EventType.TOOL_CALL, {"tool": "mock_search", "input": {"query": q[:200]}})
                yield EngineEvent(EventType.SOURCE, src.model_dump())
                yield EngineEvent(EventType.TOOL_RESULT, {"tool": "mock_search", "sources": [src.source_id]})
            await asyncio.sleep(0.5 if deep else 0.05)
        text = f"Mock {'deep report' if deep else 'answer'} for: {q[:80]} [1]"
        for chunk in text.split(" "):
            yield EngineEvent(EventType.DELTA, {"text": chunk + " "})
        yield EngineEvent(EventType.CITATIONS, {"citations": [{"marker": "[1]", "source_id": src.source_id,
                                                               "url": src.url, "verified": True,
                                                               "match_level": "exact"}],
                                                "verified": 1, "unverified": 0})
        yield EngineEvent(EventType.REPORT, {"text": text, "sources": [src.model_dump()]})
        yield EngineEvent(EventType.USAGE, {"input_tokens": 0, "output_tokens": 0, "searches": 1, "pages": 0})
        yield EngineEvent(EventType.COMPLETED, {"status": "completed"})

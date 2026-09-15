# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end adapter tests with the deterministic MockEngine: identity, journaling, replay,
cancellation, approval and tenant isolation — no model or AWS calls (moto)."""
import asyncio
import json

import pytest

from aiq_agentcore import app as appmod
from aiq_agentcore.contracts import Principal


class Ctx:
    def __init__(self, principal: Principal | None, session_id="s" * 40):
        self.session_id = session_id
        self.request_headers = {"Authorization": "Bearer dummy"} if principal else {}
        self._principal = principal


@pytest.fixture(autouse=True)
def patch_identity(monkeypatch, aws_tables):
    monkeypatch.setattr(appmod, "_principal", lambda context: context._principal or (_ for _ in ()).throw(
        appmod.AuthError(appmod.ErrorCode.UNAUTHENTICATED, "no token")))
    monkeypatch.setattr(appmod, "_store", None)
    monkeypatch.setattr(appmod, "_engine", None)
    monkeypatch.setenv("AIQ_ENGINE", "mock")


async def collect(payload, principal, session_id="s" * 40):
    out = []
    async for line in appmod.invoke(payload, Ctx(principal, session_id)):
        out.append(json.loads(line))
    return out


def test_unauthenticated_rejected():
    events = asyncio.run(collect({"op": "health"}, None))
    assert events[0]["type"] == "error" and events[0]["data"]["error"]["code"] == "unauthenticated"


def test_chat_shallow_streams_and_journals(principal):
    events = asyncio.run(collect({"op": "chat", "mode": "shallow", "messages": [{"role": "user", "content": "What is S3?"}],
                                  "client_request_id": "m1"}, principal))
    types = [e["type"] for e in events]
    assert types[0] == "job.accepted" and "source" in types and "report" in types and types[-1] == "completed"
    job_id = events[0]["job_id"]
    # Replay: a duplicate submit (same client_request_id) returns the journal instead of re-running tools.
    replay = asyncio.run(collect({"op": "chat", "mode": "shallow", "messages": [{"role": "user", "content": "What is S3?"}],
                                  "client_request_id": "m1"}, principal))
    assert replay[0]["data"]["replayed"] is True and replay[-1]["type"] == "completed"
    seqs = [e["seq"] for e in replay[1:]]
    assert seqs == sorted(seqs) and seqs[0] == 1
    # Cursor replay
    tail = asyncio.run(collect({"op": "events", "job_id": job_id, "after": seqs[-2], "tail": False}, principal))
    assert [e["seq"] for e in tail] == [seqs[-1]]


def test_cross_user_cannot_read_job(principal, other_principal):
    events = asyncio.run(collect({"op": "chat", "mode": "shallow", "messages": [{"role": "user", "content": "secret q"}]},
                                 principal))
    job_id = events[0]["job_id"]
    other = asyncio.run(collect({"op": "events", "job_id": job_id, "tail": False}, other_principal))
    assert other[0]["type"] == "error" and other[0]["data"]["error"]["code"] == "not_found"
    other = asyncio.run(collect({"op": "cancel", "job_id": job_id}, other_principal))
    assert other[0]["data"]["error"]["code"] == "not_found"


def test_deep_submit_tail_cancel(principal):
    async def flow():
        acc = await collect({"op": "submit", "mode": "deep", "messages": [{"role": "user", "content": "long " * 20}],
                             "client_request_id": "deep-1"}, principal)
        job_id = acc[0]["job_id"]
        await asyncio.sleep(0.7)  # let the background task emit a few events
        cancelled = await collect({"op": "cancel", "job_id": job_id}, principal)
        assert cancelled[0]["data"]["cancel_requested"] is True
        for _ in range(60):
            st = await collect({"op": "status", "job_id": job_id}, principal)
            if st[0]["data"]["status"] in ("cancelled", "completed", "failed"):
                break
            await asyncio.sleep(0.2)
        assert st[0]["data"]["status"] == "cancelled"
        events = await collect({"op": "events", "job_id": job_id, "tail": False}, principal)
        assert events[-1]["type"] == "cancelled"
        # duplicate submit after terminal → same job, no new run
        again = await collect({"op": "submit", "mode": "deep", "messages": [{"role": "user", "content": "long " * 20}],
                               "client_request_id": "deep-1"}, principal)
        assert again[0]["job_id"] == job_id and again[0]["data"]["created"] is False

    asyncio.run(flow())


def test_clarify_pause_and_approve(principal):
    async def flow():
        acc = await collect({"op": "submit", "mode": "deep_clarify", "messages": [{"role": "user", "content": "plan me"}],
                             "client_request_id": "c1"}, principal)
        job_id = acc[0]["job_id"]
        for _ in range(50):
            st = await collect({"op": "status", "job_id": job_id}, principal)
            if st[0]["data"]["status"] == "clarifying":
                break
            await asyncio.sleep(0.1)
        assert st[0]["data"]["status"] == "clarifying"
        ev = await collect({"op": "events", "job_id": job_id, "tail": False}, principal)
        assert "plan.approval_required" in [e["type"] for e in ev]
        resumed = await collect({"op": "approve", "job_id": job_id, "approval": "approve",
                                 "messages": [{"role": "user", "content": "plan me"}]}, principal)
        assert resumed[0]["data"]["resumed"] is True
        for _ in range(80):
            st = await collect({"op": "status", "job_id": job_id}, principal)
            if st[0]["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.2)
        assert st[0]["data"]["status"] == "completed" and st[0]["data"]["report_key"]

    asyncio.run(flow())


def test_invalid_request_shape(principal):
    events = asyncio.run(collect({"op": "events"}, principal))
    assert events[0]["type"] == "error" and events[0]["data"]["error"]["code"] == "invalid_request"
    events = asyncio.run(collect({"op": "chat", "bogus": 1}, principal))
    assert events[0]["data"]["error"]["code"] == "invalid_request"


def test_terminal_error_marks_job_failed(principal, monkeypatch):
    from aiq_agentcore.engine_base import Engine, EngineEvent
    from aiq_agentcore.contracts import EventType

    class FailingEngine(Engine):
        name = "failing"

        async def run(self, req, cancelled):
            yield EngineEvent(EventType.STATUS, {"description": "about to fail"})
            yield EngineEvent(EventType.ERROR, {"error": {"code": "internal", "message": "boom", "retryable": True}, "terminal": True})

    monkeypatch.setattr(appmod, "_engine", FailingEngine())
    events = asyncio.run(collect({"op": "chat", "mode": "shallow", "messages": [{"role": "user", "content": "x"}]}, principal))
    assert [e["type"] for e in events][-1] == "error" and events[-1]["data"]["terminal"] is True
    st = asyncio.run(collect({"op": "status", "job_id": events[0]["job_id"]}, principal))
    assert st[0]["data"]["status"] == "failed" and st[0]["data"]["error"] == "boom"

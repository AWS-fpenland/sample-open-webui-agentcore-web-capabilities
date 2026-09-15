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

from . import __version__
from .contracts import (ApiError, ErrorCode, Event, EventType, InvokeRequest, JobStatus, Op, Principal,
                        ResearchMode)
from .engine_base import Engine, EngineRequest, MockEngine
from .identity import AuthError, principal_from_request
from .store import JobStore, now_iso

logging.basicConfig(level=os.environ.get("AIQ_LOG_LEVEL", "INFO"),
                    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}')
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
    return {"type": EventType.ERROR.value, "seq": -1, "data": {"error": ApiError(code=code, message=message,
                                                                                 retryable=retryable).model_dump(),
                                                              "terminal": True, **extra}}


def _line(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False) + "\n"


def _principal(context) -> Principal:
    headers = getattr(context, "request_headers", None) or {}
    return principal_from_request(dict(headers))


# --------------------------------------------------------------------------- core run loop --


async def _journal_run(principal: Principal, job_id: str, req: EngineRequest) -> AsyncIterator[Event]:
    """Run the engine for a job, appending every event to the durable journal and yielding it."""
    st = store()
    tenant = principal.tenant_key
    cancelled = _cancel_flags.setdefault(job_id, asyncio.Event())
    st.update_job(tenant, job_id, status=JobStatus.RUNNING.value, heartbeat_at=now_iso())
    started = time.monotonic()
    last_cancel_check = 0.0
    terminal_seen = False
    try:
        async for ev in engine().run(req, cancelled):
            # Cooperative cancellation: poll the durable flag at most every 3 s.
            now = time.monotonic()
            if now - last_cancel_check > 3:
                last_cancel_check = now
                if await asyncio.get_running_loop().run_in_executor(None, st.cancel_requested, tenant, job_id):
                    cancelled.set()
            if ev.type == EventType.PLAN_APPROVAL_REQUIRED:
                st.update_job(tenant, job_id, status=JobStatus.CLARIFYING.value)
            if ev.type == EventType.REPORT and ev.data.get("text"):
                key = st.report_key(tenant, job_id)
                st.put_text(key, ev.data["text"])
                ev.data["report_key"] = key
                st.update_job(tenant, job_id, report_key=key)
            if ev.type == EventType.CITATIONS:
                key = st.report_key(tenant, job_id, "ledger.json")
                st.put_json(key, ev.data)
                ev.data["ledger_key"] = key
                st.update_job(tenant, job_id, ledger_key=key)
            if ev.type == EventType.USAGE:
                st.update_job(tenant, job_id, usage={k: int(v) for k, v in ev.data.items() if isinstance(v, (int, float))})
            stored = await asyncio.get_running_loop().run_in_executor(None, st.append_event, tenant, job_id, ev.type,
                                                                      ev.data)
            if ev.type == EventType.COMPLETED:
                st.update_job(tenant, job_id, status=JobStatus.COMPLETED.value)
                terminal_seen = True
            elif ev.type == EventType.CANCELLED:
                st.update_job(tenant, job_id, status=JobStatus.CANCELLED.value)
                terminal_seen = True
            yield stored
            if terminal_seen:
                break
        if not terminal_seen:
            rec = st.get_job(tenant, job_id)
            if rec and rec.status == JobStatus.CLARIFYING:
                return  # waiting on the user; not terminal
            if cancelled.is_set():
                st.update_job(tenant, job_id, status=JobStatus.CANCELLED.value)
                yield st.append_event(tenant, job_id, EventType.CANCELLED, {"reason": "cancel_requested"})
            else:
                st.update_job(tenant, job_id, status=JobStatus.COMPLETED.value)
                yield st.append_event(tenant, job_id, EventType.COMPLETED, {"status": "completed",
                                                                            "note": "engine ended without explicit completion"})
    except asyncio.CancelledError:
        st.update_job(tenant, job_id, status=JobStatus.CANCELLED.value)
        yield st.append_event(tenant, job_id, EventType.CANCELLED, {"reason": "task_cancelled"})
        raise
    except Exception as e:  # noqa: BLE001 — surface, never hide, engine failures
        jlog(logging.ERROR, event="job.failed", job_id=job_id, error=repr(e)[:500])
        st.update_job(tenant, job_id, status=JobStatus.FAILED.value, error=repr(e)[:1000])
        yield st.append_event(tenant, job_id, EventType.ERROR, {"error": ApiError(code=ErrorCode.INTERNAL,
                                                                                  message=str(e)[:500],
                                                                                  retryable=True).model_dump(),
                                                                "terminal": True})
    finally:
        _cancel_flags.pop(job_id, None)
        jlog(logging.INFO, event="job.finished", job_id=job_id, seconds=round(time.monotonic() - started, 2))


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
    rec, created = st.create_job(tenant_key=principal.tenant_key, mode=body.mode, question=body.messages[-1].content
                                 if body.messages else "", runtime_session_id=session_id,
                                 client_request_id=body.client_request_id, conversation_id=body.conversation_id)
    if not created and rec.last_seq > 0:
        # Duplicate submit (retry after a dropped stream): replay instead of re-running the tools.
        yield _line({"type": EventType.JOB_ACCEPTED.value, "seq": 0, "job_id": rec.job_id,
                     "data": {"replayed": True, "status": rec.status.value}})
        async for ev in st.tail_events(principal.tenant_key, rec.job_id, after=0):
            yield _line(ev.model_dump(mode="json"))
        return
    yield _line({"type": EventType.JOB_ACCEPTED.value, "seq": 0, "job_id": rec.job_id,
                 "data": {"mode": body.mode.value, "created": created}})
    req = EngineRequest(principal=principal, mode=body.mode, messages=body.messages, job_id=rec.job_id,
                        collection=body.collection, approval=body.approval, revision=body.revision)
    async for ev in _journal_run(principal, rec.job_id, req):
        yield _line(ev.model_dump(mode="json"))


async def _op_submit(principal: Principal, body: InvokeRequest, session_id: str | None) -> AsyncIterator[str]:
    st = store()
    mode = body.mode if body.mode in (ResearchMode.DEEP, ResearchMode.DEEP_CLARIFY) else ResearchMode.DEEP
    rec, created = st.create_job(tenant_key=principal.tenant_key, mode=mode,
                                 question=body.messages[-1].content if body.messages else "",
                                 runtime_session_id=session_id, client_request_id=body.client_request_id,
                                 conversation_id=body.conversation_id)
    if created:
        req = EngineRequest(principal=principal, mode=mode, messages=body.messages, job_id=rec.job_id,
                            collection=body.collection)
        _start_background(principal, rec.job_id, req)
    yield _line({"type": EventType.JOB_ACCEPTED.value, "seq": 0, "job_id": rec.job_id,
                 "data": {"mode": mode.value, "created": created, "status": rec.status.value}})


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
        yield _line(st.append_event(principal.tenant_key, rec.job_id, EventType.CANCELLED,
                                    {"reason": "plan_rejected"}).model_dump(mode="json"))
        return
    req = EngineRequest(principal=principal, mode=rec.mode, messages=body.messages or
                        [{"role": "user", "content": rec.question}],  # type: ignore[list-item]
                        job_id=rec.job_id, collection=body.collection, approval=body.approval, revision=body.revision)
    _start_background(principal, rec.job_id, req)
    yield _line({"type": EventType.JOB_ACCEPTED.value, "seq": rec.last_seq, "job_id": rec.job_id,
                 "data": {"resumed": True, "approval": body.approval}})


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
    yield _line({"type": "collections", "seq": 0, "data": {"collections": [{"name": k, "documents": v}
                                                                              for k, v in sorted(by_coll.items())]}})


async def _op_delete_collection(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    from .knowledge import start_sync

    if not body.collection:
        yield _line(_err(ErrorCode.INVALID_REQUEST, "collection is required"))
        return
    n = store().delete_collection(principal.tenant_key, body.collection)
    sync = start_sync()
    yield _line({"type": "collection.deleted", "seq": 0, "data": {"collection": body.collection, "objects_deleted": n,
                                                                  "ingestion_job": sync}})


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
        yield _line(_err(ErrorCode.INVALID_REQUEST, e.errors()[0].get("msg", "invalid request")[:300],
                         request_id=request_id, field=".".join(str(x) for x in e.errors()[0].get("loc", []))))
        return
    jlog(logging.INFO, event="invoke", op=body.op.value, mode=body.mode.value, tenant=principal.tenant_key,
         job_id=body.job_id, session=session_id, request_id=request_id)
    try:
        if body.op == Op.HEALTH:
            yield _line({"type": "health", "seq": 0, "data": {
                "version": __version__, "engine": os.environ.get("AIQ_ENGINE", "aiq"),
                "source_commit": os.environ.get("AIQ_SOURCE_COMMIT"), "upstream_ref": os.environ.get("AIQ_UPSTREAM_REF"),
                "tenant": principal.tenant_key, "active_jobs": len(_tasks)}})
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
    except Exception as e:  # noqa: BLE001
        jlog(logging.ERROR, event="invoke.failed", op=body.op.value, error=repr(e)[:500], request_id=request_id)
        yield _line(_err(ErrorCode.INTERNAL, "internal error; see runtime logs", retryable=True, request_id=request_id))
    finally:
        jlog(logging.INFO, event="invoke.done", op=body.op.value, request_id=request_id,
             ms=int((time.monotonic() - t0) * 1000))


if __name__ == "__main__":
    app.run()

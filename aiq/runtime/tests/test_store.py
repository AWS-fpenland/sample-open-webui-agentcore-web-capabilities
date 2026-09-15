# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import asyncio

import pytest

from aiq_agentcore.contracts import EventType, JobStatus, ResearchMode


def test_create_job_idempotent_on_client_request_id(store, principal):
    rec, created = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.DEEP, question="q",
                                    runtime_session_id="s" * 33, client_request_id="msg-1", conversation_id="chat-1")
    rec2, created2 = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.DEEP, question="q",
                                      runtime_session_id="s" * 33, client_request_id="msg-1", conversation_id="chat-1")
    assert created and not created2 and rec.job_id == rec2.job_id
    assert rec.status is JobStatus.QUEUED and rec.expires_at


def test_events_are_monotonic_and_replayable(store, principal):
    rec, _ = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.SHALLOW, question="q",
                              runtime_session_id=None, client_request_id=None, conversation_id=None)
    e1 = store.append_event(principal.tenant_key, rec.job_id, EventType.STATUS, {"description": "a"})
    e2 = store.append_event(principal.tenant_key, rec.job_id, EventType.DELTA, {"text": "b"})
    e3 = store.append_event(principal.tenant_key, rec.job_id, EventType.COMPLETED, {})
    assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)
    assert [e.seq for e in store.read_events(rec.job_id, after=1)] == [2, 3]
    assert store.get_job(principal.tenant_key, rec.job_id).last_seq == 3


def test_cross_tenant_job_is_invisible(store, principal, other_principal):
    rec, _ = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.DEEP, question="secret",
                              runtime_session_id=None, client_request_id=None, conversation_id=None)
    assert store.get_job(other_principal.tenant_key, rec.job_id) is None
    assert store.request_cancel(other_principal.tenant_key, rec.job_id) is None
    assert store.list_jobs(other_principal.tenant_key) == []


def test_cancel_sets_flag(store, principal):
    rec, _ = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.DEEP, question="q",
                              runtime_session_id=None, client_request_id=None, conversation_id=None)
    store.update_job(principal.tenant_key, rec.job_id, status=JobStatus.RUNNING.value)
    out = store.request_cancel(principal.tenant_key, rec.job_id)
    assert out.cancel_requested and out.status is JobStatus.CANCELLING
    assert store.cancel_requested(principal.tenant_key, rec.job_id)


def test_tail_stops_at_terminal(store, principal):
    rec, _ = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.DEEP, question="q",
                              runtime_session_id=None, client_request_id=None, conversation_id=None)
    store.append_event(principal.tenant_key, rec.job_id, EventType.STATUS, {"description": "x"})
    store.append_event(principal.tenant_key, rec.job_id, EventType.COMPLETED, {})

    async def collect():
        return [e.type for e in [ev async for ev in store.tail_events(principal.tenant_key, rec.job_id, after=0)]]

    assert asyncio.run(collect()) == [EventType.STATUS, EventType.COMPLETED]


def test_documents_scoped_by_tenant(store, principal, other_principal):
    store.put_document(principal.tenant_key, "coll", "a.txt", b"hello", "text/plain")
    assert [d["name"] for d in store.list_documents(principal.tenant_key)] == ["a.txt"]
    assert store.list_documents(other_principal.tenant_key) == []
    assert store.delete_collection(principal.tenant_key, "coll") == 2  # object + metadata sidecar
    assert store.list_documents(principal.tenant_key) == []

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the AI-Q manifold pipe: model exposure, job footer parsing, session ids,
event rendering (statuses, citations, terminal states) — no network."""
import asyncio
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("aiq_agentcore_pipe", os.path.join(HERE, "..", "aiq_agentcore_pipe.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["aiq_agentcore_pipe"] = mod
spec.loader.exec_module(mod)


@pytest.fixture
def pipe():
    p = mod.Pipe()
    p.valves.RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/aiq_test-abcdefghij"
    return p


def test_models_are_explicit_modes(pipe):
    ids = [m["id"] for m in pipe.pipes()]
    assert ids == ["auto", "shallow", "deep", "deep_clarify"]


def test_endpoint_encodes_arn(pipe):
    url = pipe._endpoint()
    assert url.startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore")
    assert url.endswith("/invocations?qualifier=DEFAULT")


def test_session_ids_meet_runtime_minimum(pipe):
    for kind in ("job", "tail", "ctl"):
        sid = pipe._session_id(kind, "c", "u")
        assert 33 <= len(sid) <= 128
    assert pipe._session_id("job", "chat-1", "user-1") != pipe._session_id("tail", "chat-1", "user-1")


def test_pending_job_footer_parsing(pipe):
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": "Could you clarify?\n\n---\n_aiq-job:job_" + "a" * 32 + ":clarifying_"},
            {"role": "user", "content": "approve"}]
    assert pipe._pending_job(msgs) == ("job_" + "a" * 32, "clarifying")
    assert pipe._pending_job([{"role": "assistant", "content": "no footer"}]) is None


def test_source_event_shape_matches_owui_contract(pipe):
    ev = pipe._source_event({"url": "https://x.example/a", "title": "A", "snippet": "s", "retrieved_at": "2026-01-01T00:00:00Z"})
    assert ev["type"] == "source"
    d = ev["data"]
    assert d["source"] == {"name": "A", "url": "https://x.example/a"} and d["document"] == ["s"]
    assert d["metadata"][0]["source"] == "https://x.example/a" and d["metadata"][0]["name"] == "A"


def test_render_events(pipe):
    statuses, sources, out = [], [], []

    async def status(desc, done=False, hidden=False):
        statuses.append((desc, done))

    async def source(src):
        sources.append(src)

    async def run():
        state = {"cursor": 0}
        events = [
            {"type": "job.accepted", "seq": 0, "job_id": "job_" + "b" * 32, "data": {}},
            {"type": "route", "seq": 1, "data": {"mode": "deep", "reason": "forced"}},
            {"type": "status", "seq": 2, "data": {"description": "Planning", "done": False}},
            {"type": "tool.call", "seq": 3, "data": {"tool": "agentcore_web_search", "input": {"query": "q"}}},
            {"type": "source", "seq": 4, "data": {"url": "https://a.example", "title": "A", "snippet": "s"}},
            {"type": "report", "seq": 5, "data": {"text": "Answer [1]"}},
            {"type": "citations", "seq": 6, "data": {"verified": 1, "unverified": 0}},
            {"type": "usage", "seq": 7, "data": {"searches": 2, "input_tokens": 10, "output_tokens": 5}},
            {"type": "completed", "seq": 8, "data": {}},
        ]
        for ev in events:
            async for piece in pipe._render(ev, state, status, source):
                out.append(piece)
        return state

    state = asyncio.run(run())
    assert "".join(out) == "Answer [1]"
    assert state["terminal"] == "completed" and state["cursor"] == 8 and state["sources"] == 1
    assert any("deep" in s[0] for s in statuses) and any("Citations verified" in s[0] for s in statuses)
    assert any(s[0].startswith("Routing: deep research") for s in statuses)
    assert "2 searches" in pipe._usage_line(state) and "1 source" in pipe._usage_line(state)


def test_render_clarification_ends_turn(pipe):
    async def run():
        state = {"cursor": 0}
        out = []
        async for piece in pipe._render({"type": "clarification", "seq": 1, "data": {"question": "Which year?"}}, state,
                                        lambda *a, **k: asyncio.sleep(0), lambda s: asyncio.sleep(0)):
            out.append(piece)
        return state, "".join(out)

    state, text = asyncio.run(run())
    assert state["clarifying"] and "Which year?" in text and "approve" in text


def test_footer_format(pipe):
    f = pipe._footer("job_" + "c" * 32, "completed", "2 searches")
    assert f.startswith("\n\n_aiq-job:job_") and "aiq-package:job_" in f and f.endswith("· 2 searches_")


def test_resume_tails_after_pause_cursor(pipe, monkeypatch):
    """After approving a clarification, the pipe must tail from the seq returned by the runtime (past the replayed
    pause events), otherwise the old question is re-rendered and the report is never shown."""
    calls = []

    async def fake_invoke(bearer, session_id, payload, timeout_s=None):
        calls.append(payload)
        if payload["op"] == "approve":
            yield {"type": "job.accepted", "seq": 14, "job_id": payload["job_id"], "data": {"resumed": True}}
        elif payload["op"] == "events":
            assert payload["after"] == 14
            yield {"type": "report", "seq": 20, "job_id": payload["job_id"], "data": {"text": "The report"}}
            yield {"type": "completed", "seq": 21, "job_id": payload["job_id"], "data": {}}

    monkeypatch.setattr(pipe, "_invoke", fake_invoke)

    async def bearer(*a, **k):
        return "tok"

    monkeypatch.setattr(pipe, "_bearer", bearer)
    job = "job_" + "d" * 32
    body = {"model": "aiq_agentcore.deep_clarify", "messages": [
        {"role": "user", "content": "Tell me about performance"},
        {"role": "assistant", "content": "Which area?\n\n---\n_aiq-job:" + job + ":clarifying_"},
        {"role": "user", "content": "S3 Vectors latency"}]}

    async def run():
        out = await pipe.pipe(body, __user__={"id": "u1"}, __metadata__={"chat_id": "c1", "message_id": "m2"}, __event_emitter__=None)
        text = ""
        async for piece in out:
            text += piece
        return text

    text = asyncio.run(run())
    assert calls[0]["op"] == "approve" and calls[0]["revision"] == "S3 Vectors latency"
    assert "The report" in text and f"aiq-job:{job}:completed" in text


def test_user_valves_sources(pipe):
    class UV:
        SOURCES = "web_search, documents,bogus,news"
        PAGE_FETCH = False
        REPORT_FOLLOWUPS = True

    sources, page_fetch, followups = pipe._user_sources({"valves": UV()})
    assert sources == ["web_search", "documents", "news"] and page_fetch is False and followups is True
    assert pipe._user_sources({})[0] == ["web_search", "documents"]
    assert pipe._user_sources({"valves": {"SOURCES": "", "PAGE_FETCH": True}})[0] is None

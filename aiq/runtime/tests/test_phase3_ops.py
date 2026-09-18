# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase-3 ops with the deterministic MockEngine (moto): packages become real, tenancy holds, model selection is honest."""

import asyncio
import json

import pytest

from aiq_agentcore import app as appmod, modellab_runtime
from aiq_agentcore.contracts import Principal


class Ctx:
    def __init__(self, principal: Principal | None, session_id="s" * 40):
        self.session_id = session_id
        self.request_headers = {"Authorization": "Bearer dummy"} if principal else {}
        self._principal = principal


MATRIX_ENTRIES = [
    {
        "id": "nvidia.nemotron-nano-3-30b",
        "lane": "converse",
        "base_model_id": "nvidia.nemotron-nano-3-30b",
        "name": "Nemotron Nano 3 30B",
        "provider": "NVIDIA",
        "family": "nemotron",
        "region": "us-east-1",
        "routing": "in_region",
        "offered": True,
        "exclusion_reasons": [],
        "price": {"input_per_1m": 0.06, "output_per_1m": 0.24, "source": "aws-published"},
        "capabilities": {"tools": {"ok": True}},
        "latency": {"ttft_ms": 200},
        "roles": {"writer": {"score": 85}, "router": {"score": 100}},
        "roles_selectable": {
            "router": True,
            "clarifier": True,
            "shallow": True,
            "planner": True,
            "researcher": True,
            "writer": True,
        },
    },
    {
        "id": "global.anthropic.claude-sonnet-5",
        "lane": "converse",
        "base_model_id": "anthropic.claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "provider": "Anthropic",
        "family": "claude",
        "region": "us-east-1",
        "routing": "global",
        "offered": False,
        "exclusion_reasons": ["probe:plain:AccessDeniedException"],
        "price": {"input_per_1m": 2.0, "output_per_1m": 10.0, "source": "aws-published"},
        "capabilities": {"plain": {"ok": False, "error_code": "AccessDeniedException"}},
        "latency": {},
        "roles": {},
        "roles_selectable": {},
    },
    {
        "id": "anthropic.claude-sonnet-5",
        "lane": "mantle_messages",
        "base_model_id": "anthropic.claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "provider": "Anthropic",
        "family": "claude",
        "region": "us-east-1",
        "routing": "in_region",
        "offered": True,
        "exclusion_reasons": [],
        "price": {"input_per_1m": 2.2, "output_per_1m": 11.0, "source": "aws-published"},
        "capabilities": {"tools": {"ok": True}},
        "latency": {"ttft_ms": 1700},
        "roles": {"writer": {"score": 100}},
        "roles_selectable": {
            "router": True,
            "clarifier": True,
            "shallow": True,
            "planner": True,
            "researcher": True,
            "writer": True,
        },
    },
]


@pytest.fixture(autouse=True)
def patch_identity(monkeypatch, aws_tables):
    monkeypatch.setattr(
        appmod,
        "_principal",
        lambda context: (
            context._principal or (_ for _ in ()).throw(appmod.AuthError(appmod.ErrorCode.UNAUTHENTICATED, "no token"))
        ),
    )
    monkeypatch.setattr(appmod, "_store", None)
    monkeypatch.setattr(appmod, "_engine", None)
    monkeypatch.setenv("AIQ_ENGINE", "mock")
    monkeypatch.setenv("AIQ_MODEL_ROUTER", "nvidia.nemotron-nano-3-30b")
    monkeypatch.setenv("AIQ_MODEL_SHALLOW", "nvidia.nemotron-nano-3-30b")
    monkeypatch.setenv("AIQ_MODEL_PLANNER", "global.amazon.nova-2-lite-v1:0")
    monkeypatch.setenv("AIQ_MODEL_RESEARCHER", "global.amazon.nova-2-lite-v1:0")
    monkeypatch.setenv("AIQ_MODEL_WRITER", "nvidia.nemotron-super-3-120b")
    # a signed matrix in the (moto) bucket
    from aiq_agentcore.store import JobStore
    from modellab_digest import digest  # noqa: F401  (see helper below)

    st = JobStore()
    mx = {
        "schema": "aiq-modellab/v1",
        "generated_at": "2026-09-18T19:00:00Z",
        "probed_as": "arn:test",
        "entries": MATRIX_ENTRIES,
        "summary": {"offered": 2},
        "offer_versions": {},
    }
    mx["digest"] = digest(mx["entries"])
    st.put_json(modellab_runtime.MATRIX_KEY, mx)
    modellab_runtime._CACHE.update(at=0.0, matrix=None, error=None)
    yield
    modellab_runtime._CACHE.update(at=0.0, matrix=None, error=None)


async def collect(payload, principal, session_id="s" * 40):
    out = []
    async for line in appmod.invoke(payload, Ctx(principal, session_id)):
        out.append(json.loads(line))
    return out


def _run_shallow(principal, text="What is S3?", crid="m1"):
    return asyncio.run(
        collect(
            {"op": "chat", "mode": "shallow", "messages": [{"role": "user", "content": text}], "client_request_id": crid},
            principal,
        )
    )


def test_completed_job_becomes_a_listed_package_with_manifest(principal):
    events = _run_shallow(principal)
    assert [e["type"] for e in events][-2:] == ["package", "completed"], [e["type"] for e in events][-3:]
    job_id = events[0]["job_id"]
    summ = events[-2]["data"]
    assert summ["job_id"] == job_id and summ["counts"]["sources"] == 1 and summ["counts"]["citations_verified"] == 1
    lst = asyncio.run(collect({"op": "packages.list"}, principal))
    items = lst[0]["data"]["items"]
    assert [i["job_id"] for i in items] == [job_id] and items[0]["status"] == "completed"
    got = asyncio.run(collect({"op": "packages.get", "job_id": job_id}, principal))
    m = got[0]["data"]["manifest"]
    assert m["schema_version"] == "aiq-agentcore/package/v1" and m["report"]["present"] and m["citations"]["verified"] == 1
    assert (
        m["models"]["roles"]["writer"]["model_id"] == "nvidia.nemotron-super-3-120b"
        and m["models"]["selection"] == "deploy_default"
    )
    assert got[0]["data"]["report_md"].startswith("Mock answer")
    assert m["retention"]["s3_prefix"] == f"packages/{principal.tenant_key}/{job_id}/"


def test_tags_pins_compare_delete_and_tenancy(principal, other_principal):
    a = _run_shallow(principal, "first question about S3", "a")[0]["job_id"]
    b = _run_shallow(principal, "second question about S3 Vectors", "b")[0]["job_id"]
    upd = asyncio.run(
        collect(
            {"op": "packages.update", "job_id": a, "tags": ["Infra", "q3-review"], "pinned": True, "title": "My S3 note"},
            principal,
        )
    )
    s = upd[0]["data"]["summary"]
    assert s["tags"] == ["infra", "q3-review"] and s["pinned"] and s["title"] == "My S3 note"
    lst = asyncio.run(collect({"op": "packages.list", "tag": "infra"}, principal))
    assert [i["job_id"] for i in lst[0]["data"]["items"]] == [a]
    lst = asyncio.run(collect({"op": "packages.list", "q": "vectors"}, principal))
    assert [i["job_id"] for i in lst[0]["data"]["items"]] == [b]
    cmp_ = asyncio.run(collect({"op": "packages.compare", "job_id": a, "other_job_id": b}, principal))
    assert cmp_[0]["type"] == "comparison" and cmp_[0]["data"]["sources"]["shared"]  # same mock source in both
    # other tenant sees nothing
    for payload in (
        {"op": "packages.get", "job_id": a},
        {"op": "packages.update", "job_id": a, "pinned": False},
        {"op": "packages.delete", "job_id": a},
        {"op": "packages.compare", "job_id": a, "other_job_id": b},
        {"op": "export", "job_id": a, "format": "md"},
        {"op": "packages.rerun", "job_id": a},
    ):
        r = asyncio.run(collect(payload, other_principal))
        assert r[0]["type"] == "error" and r[0]["data"]["error"]["code"] == "not_found", payload
    assert asyncio.run(collect({"op": "packages.list"}, other_principal))[0]["data"]["items"] == []
    # delete by the owner: gone from the list, manifest gone, tombstone prevents resurrection
    d = asyncio.run(collect({"op": "packages.delete", "job_id": a}, principal))
    assert d[0]["type"] == "package.deleted"
    assert [i["job_id"] for i in asyncio.run(collect({"op": "packages.list"}, principal))[0]["data"]["items"]] == [b]
    assert asyncio.run(collect({"op": "packages.get", "job_id": a}, principal))[0]["data"]["error"]["code"] == "not_found"
    assert appmod.store().is_tombstoned(principal.tenant_key, a)


def test_rerun_records_lineage(principal):
    parent = _run_shallow(principal, "lineage question", "p1")[0]["job_id"]

    async def flow():
        rr = await collect(
            {
                "op": "packages.rerun",
                "job_id": parent,
                "models": {"writer": {"model_id": "anthropic.claude-sonnet-5", "lane": "mantle_messages"}},
            },
            principal,
        )
        assert (
            rr[0]["type"] == "job.accepted" and rr[0]["data"]["relation"] == "rerun" and rr[0]["data"]["parent_job_id"] == parent
        )
        child = rr[0]["job_id"]
        assert rr[0]["data"]["models"]["writer"]["source"] == "session_override"
        for _ in range(80):
            st = await collect({"op": "status", "job_id": child}, principal)
            if st[0]["data"]["status"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)
        assert st[0]["data"]["status"] == "completed"
        return child

    child = asyncio.run(flow())
    got = asyncio.run(collect({"op": "packages.get", "job_id": child}, principal))
    m = got[0]["data"]["manifest"]
    assert m["lineage"] == {"parent_package_id": parent, "relation": "rerun", "root_package_id": parent, "result_kind": None}
    assert (
        m["models"]["roles"]["writer"]["model_id"] == "anthropic.claude-sonnet-5"
        and m["models"]["roles"]["writer"]["lane"] == "bedrock_mantle"
    )
    assert m["models"]["selection"] == "session_override"


def test_model_selection_is_validated_against_the_matrix(principal):
    # excluded on Converse (AccessDenied in this account) → refused with the reason, nothing runs
    r = asyncio.run(
        collect(
            {
                "op": "submit",
                "mode": "deep",
                "messages": [{"role": "user", "content": "x " * 20}],
                "models": {"writer": {"model_id": "global.anthropic.claude-sonnet-5", "lane": "converse"}},
            },
            principal,
        )
    )
    assert (
        r[0]["type"] == "error"
        and "AccessDenied" in r[0]["data"]["error"]["message"]
        and r[0]["data"]["problems"][0]["role"] == "writer"
    )
    # unknown model → refused
    r = asyncio.run(
        collect({"op": "models.validate", "models": {"planner": {"model_id": "acme.super-model", "lane": "converse"}}}, principal)
    )
    assert r[0]["data"]["ok"] is False and "not in the capability matrix" in r[0]["data"]["problems"][0]["reason"]
    # unknown role → schema error
    r = asyncio.run(
        collect({"op": "models.validate", "models": {"barista": {"model_id": "nvidia.nemotron-nano-3-30b"}}}, principal)
    )
    assert r[0]["type"] == "error" and r[0]["data"]["error"]["code"] == "invalid_request"
    # offered → accepted, human names + estimate present
    r = asyncio.run(
        collect(
            {"op": "models.validate", "models": {"writer": {"model_id": "anthropic.claude-sonnet-5", "lane": "mantle_messages"}}},
            principal,
        )
    )
    assert r[0]["data"]["ok"] and r[0]["data"]["resolved"]["writer"]["human_name"] == "Claude Sonnet 5"
    # prefs persist and apply to the next job
    r = asyncio.run(
        collect(
            {"op": "models.prefs", "models": {"writer": {"model_id": "anthropic.claude-sonnet-5", "lane": "mantle_messages"}}},
            principal,
        )
    )
    assert r[0]["type"] == "models.prefs"
    ev = _run_shallow(principal, "prefs applied?", "pf")
    route = next(e for e in ev if e["type"] == "route")
    assert route["data"]["models"]["writer"]["source"] == "tenant_preference"
    m = asyncio.run(collect({"op": "models"}, principal))[0]["data"]
    assert m["matrix"]["status"]["loaded"] and any(e["id"] == "anthropic.claude-sonnet-5" for e in m["entries"])
    assert m["prefs"]["writer"]["model_id"] == "anthropic.claude-sonnet-5" and m["offered_by_role"]["writer"]


def test_eval_creates_lineage_eval_jobs_and_lists_rows(principal):
    async def flow():
        r = await collect({"op": "eval", "question_ids": ["s1-s3vectors-regions"]}, principal)
        assert r[0]["type"] == "eval.started" and len(r[0]["data"]["job_ids"]) == 1
        job = r[0]["data"]["job_ids"][0]
        for _ in range(80):
            st = await collect({"op": "status", "job_id": job}, principal)
            if st[0]["data"]["status"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)
        assert st[0]["data"]["status"] == "completed" and st[0]["data"]["relation"] == "eval"
        lst = (await collect({"op": "eval.list"}, principal))[0]["data"]["items"]
        assert lst and lst[0]["rows"][0]["status"] == "completed" and lst[0]["judge_note"].startswith("deterministic")
        assert "no LLM judge" in lst[0]["judge_note"] and lst[0]["status"] == "completed"

    asyncio.run(flow())


def test_export_md_and_json_when_renderers_present(principal):
    pytest.importorskip("aiq_agentcore.exports.service")
    job = _run_shallow(principal, "export me", "ex")[0]["job_id"]
    r = asyncio.run(collect({"op": "export", "job_id": job, "format": "md"}, principal))
    assert r[0]["type"] == "export" and r[0]["data"]["url"].startswith("https://") and r[0]["data"]["cached"] is False
    r2 = asyncio.run(collect({"op": "export", "job_id": job, "format": "md"}, principal))
    assert r2[0]["data"]["cached"] is True and r2[0]["data"]["sha256"] == r[0]["data"]["sha256"]
    r = asyncio.run(collect({"op": "export", "job_id": job, "format": "nope"}, principal))
    assert r[0]["type"] == "error"

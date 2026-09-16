# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import asyncio
import sys

import pytest

from aiq_agentcore import aiq_api_shim
from aiq_agentcore.contracts import EventType, JobStatus, ResearchMode
from aiq_agentcore.run_context import RunContext, reset_run_context, set_run_context

REPORT = """# Title

Body with claims [1] and [2].

## Sources
[1] Amazon S3 Vectors: https://aws.amazon.com/s3/features/vectors/
[2] Internal memo, p.3
- [3] https://docs.aws.amazon.com/x.
"""


def test_sources_parsed_and_deduped():
    ctx = aiq_api_shim.report_context_from_markdown(REPORT + "\n[1] https://aws.amazon.com/s3/features/vectors/", "job_" + "a" * 32)
    urls = [s.url for s in ctx.sources]
    assert urls == ["https://aws.amazon.com/s3/features/vectors/", None, "https://docs.aws.amazon.com/x"]
    assert ctx.sources[1].citation_key == "Internal memo, p.3"
    assert "- [1] " in ctx.source_summary_markdown
    files = aiq_api_shim.to_initial_files(ctx, "shorten")
    assert set(files) == {"/shared/original_report.md", "/shared/parent_report_context.json",
                          "/shared/source_summary.md", "/shared/edit_instruction.txt"}
    assert "report_markdown" not in files["/shared/parent_report_context.json"]


def test_install_registers_modules_once():
    assert aiq_api_shim.install() in (True, False)
    assert aiq_api_shim.install() is False
    from aiq_api.jobs.report_context import resolve_authorized_report_context  # noqa: F401
    from aiq_api.jobs.access import require_verified_principal  # noqa: F401
    assert sys.modules["aiq_api"].__aiq_agentcore_shim__ is True
    with pytest.raises(ModuleNotFoundError):
        __import__("aiq_api.auth.middleware")


def test_resolve_is_tenant_scoped(store, principal, other_principal):
    pytest.importorskip("aiq_agent", reason="upstream Principal model not installed in adapter-only env")
    rec, _ = store.create_job(tenant_key=principal.tenant_key, mode=ResearchMode.DEEP, question="q",
                              runtime_session_id=None, client_request_id=None, conversation_id="chat-1")
    key = store.put_text(store.report_key(principal.tenant_key, rec.job_id), REPORT)
    store.update_job(principal.tenant_key, rec.job_id, status=JobStatus.COMPLETED.value, report_key=key)
    store.append_event(principal.tenant_key, rec.job_id, EventType.COMPLETED, {})

    async def resolve_as(p):
        token = set_run_context(RunContext(job_id="job_" + "f" * 32, tenant_key=p.tenant_key))
        try:
            pr = aiq_api_shim.require_verified_principal()
            return await aiq_api_shim.resolve_authorized_report_context(rec.job_id, pr)
        finally:
            reset_run_context(token)

    ctx = asyncio.run(resolve_as(principal))
    assert ctx.parent_job_id == rec.job_id and ctx.sources[0].url.startswith("https://aws.amazon.com")
    with pytest.raises(aiq_api_shim.ReportNotAvailable):
        asyncio.run(resolve_as(other_principal))
    assert aiq_api_shim.get_latest_report_job_for_conversation("chat-1", type("P", (), {"sub": principal.tenant_key})()) == rec.job_id

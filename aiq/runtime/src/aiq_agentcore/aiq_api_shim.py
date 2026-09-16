# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Portions adapted from NVIDIA AI-Q Blueprint `frontends/aiq_api/src/aiq_api/jobs/report_context.py`
# (SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES; SPDX-License-Identifier: Apache-2.0).
"""Compatibility shim for AI-Q **report follow-ups** without the upstream `aiq_api` service.

Upstream's chat workflow (`aiq_agent/agents/chat_researcher/register.py`) imports, at call time,
``aiq_api.jobs.access.require_verified_principal`` and
``aiq_api.jobs.report_context.{resolve_authorized_report_context, report_context_from_markdown, to_initial_files}``
to answer questions about, edit, or extend a previously completed report. `aiq_api` is the Dask/Postgres
job service this adapter replaced, so this module installs lightweight modules under the same names:

* ``require_verified_principal`` → the verified AgentCore tenant (from the run context), never a request field
* ``resolve_authorized_report_context(job_id, principal)`` → loads the report **only if the job belongs to the
  caller's tenant** (DynamoDB job record + S3 report) and reconstructs sources from its ``## Sources`` section using
  the vendored upstream parser, so edits keep verifiable citations.

Installed once by ``AiqEngine`` before the first turn (``install()``); a real `aiq_api` installation takes precedence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import types

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>)\]]+")
_SOURCES_HEADING_RE = re.compile(r"^##\s+(sources|references)\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_CITATION_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?\[\d+\]\s*(?P<text>.+?)\s*$")
_URL_TRIM_CHARS = ".,;:"


class ReportContextSource(BaseModel):
    url: str | None = None
    citation_key: str | None = None
    title: str | None = None
    source_type: str = "parent_report"
    tool_name: str = "parent_report"


class ReportContext(BaseModel):
    parent_job_id: str
    report_markdown: str
    source_summary_markdown: str
    sources: list[ReportContextSource] = Field(default_factory=list)


class ReportNotAvailable(RuntimeError):
    """Raised when the referenced report is missing, incomplete, or belongs to another tenant."""


# ---- vendored pure helpers (behaviour identical to upstream) ------------------------------------------------
def _sources_section(report_markdown: str) -> str:
    match = _SOURCES_HEADING_RE.search(report_markdown)
    if not match:
        return ""
    rest = report_markdown[match.end():]
    next_heading = _NEXT_HEADING_RE.search(rest)
    return rest[: next_heading.start()] if next_heading else rest


def _extract_sources_from_report_markdown(report_markdown: str) -> list[ReportContextSource]:
    section = _sources_section(report_markdown)
    if not section:
        return []
    sources: list[ReportContextSource] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _CITATION_LINE_RE.match(stripped)
        if not m:
            continue
        ref_text = m.group("text").strip()
        url_match = _URL_RE.search(ref_text)
        if url_match:
            sources.append(ReportContextSource(url=url_match.group(0).rstrip(_URL_TRIM_CHARS), title=ref_text))
        else:
            sources.append(ReportContextSource(citation_key=ref_text, title=ref_text))
    return sources


def _dedupe_sources(sources: list[ReportContextSource]) -> list[ReportContextSource]:
    seen: set[str] = set()
    out: list[ReportContextSource] = []
    for s in sources:
        if s.url:
            key = f"url:{s.url.rstrip('/').lower()}"
        elif s.citation_key:
            key = f"citation_key:{s.citation_key.strip().lower()}"
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _source_summary_markdown(sources: list[ReportContextSource]) -> str:
    if not sources:
        return "No durable source metadata was found for the parent report."
    lines = []
    for i, s in enumerate(sources, start=1):
        locator = s.url or s.citation_key or "(unknown source)"
        title = f"{s.title}: " if s.title and s.title != locator else ""
        lines.append(f"- [{i}] {title}{locator}")
    return "\n".join(lines)


def report_context_from_markdown(report_markdown: str, parent_job_id: str = "in-session") -> ReportContext:
    sources = _dedupe_sources(_extract_sources_from_report_markdown(report_markdown))
    return ReportContext(parent_job_id=parent_job_id, report_markdown=report_markdown,
                         source_summary_markdown=_source_summary_markdown(sources), sources=sources)


def to_initial_files(context: ReportContext, instruction: str | None = None) -> dict[str, str]:
    files = {
        "/shared/original_report.md": context.report_markdown,
        "/shared/parent_report_context.json": context.model_dump_json(indent=2, exclude={"report_markdown"}),
        "/shared/source_summary.md": context.source_summary_markdown,
    }
    if instruction is not None:
        files["/shared/edit_instruction.txt"] = instruction
    return files


def report_output_metadata(parent_job_id: str, action: str) -> dict[str, str]:
    return {"parent_job_id": parent_job_id, "interaction_action": action, "result_kind": "report"}


# ---- adapter-backed pieces ---------------------------------------------------------------------------------
def require_verified_principal():
    """The verified tenant of the current job (set by the engine); never derived from request fields."""
    from aiq_agent.auth import Principal  # upstream model

    from .run_context import get_run_context

    ctx = get_run_context()
    if ctx is None:
        raise ReportNotAvailable("report follow-up requires an active research job context")
    return Principal(type="agentcore", sub=ctx.tenant_key)


async def resolve_authorized_report_context(parent_job_id: str, principal) -> ReportContext:
    from .contracts import JobStatus
    from .store import JobStore

    tenant_key = getattr(principal, "sub", None)
    if not tenant_key or not isinstance(parent_job_id, str) or not re.fullmatch(r"job_[a-f0-9]{32}", parent_job_id):
        raise ReportNotAvailable("invalid report reference")
    store = JobStore()
    loop = asyncio.get_running_loop()
    rec = await loop.run_in_executor(None, store.get_job, tenant_key, parent_job_id)  # tenant-partitioned lookup
    if rec is None or rec.status != JobStatus.COMPLETED or not rec.report_key:
        raise ReportNotAvailable("the referenced report is not available to you")
    markdown = await loop.run_in_executor(None, store.get_text, rec.report_key)
    log.info(json.dumps({"event": "report_context.loaded", "parent_job_id": parent_job_id, "chars": len(markdown)}))
    return report_context_from_markdown(markdown, parent_job_id=parent_job_id)


def get_latest_report_job_for_conversation(conversation_id: str, principal, db_url: str | None = None) -> str | None:
    """Optional upstream hook: latest completed report in a conversation (used when no explicit id is given)."""
    from .contracts import JobStatus
    from .store import JobStore

    tenant_key = getattr(principal, "sub", None)
    if not tenant_key or not conversation_id:
        return None
    for rec in JobStore().list_jobs(tenant_key, limit=50):
        if rec.conversation_id == conversation_id and rec.status == JobStatus.COMPLETED and rec.report_key:
            return rec.job_id
    return None


def install() -> bool:
    """Register the shim modules unless a real `aiq_api` is importable. Idempotent."""
    existing = sys.modules.get("aiq_api")
    if existing is not None:
        return False  # either our shim (idempotent) or a real aiq_api already imported
    try:
        import importlib.util

        if importlib.util.find_spec("aiq_api") is not None:
            log.info("aiq_api_shim: real aiq_api present; shim not installed")
            return False
    except (ModuleNotFoundError, ValueError):
        pass
    pkg = types.ModuleType("aiq_api")
    pkg.__path__ = []  # type: ignore[attr-defined]
    pkg.__aiq_agentcore_shim__ = True  # type: ignore[attr-defined]
    jobs = types.ModuleType("aiq_api.jobs")
    jobs.__path__ = []  # type: ignore[attr-defined]
    access = types.ModuleType("aiq_api.jobs.access")
    access.require_verified_principal = require_verified_principal  # type: ignore[attr-defined]
    access.get_latest_report_job_for_conversation = get_latest_report_job_for_conversation  # type: ignore[attr-defined]
    rc = types.ModuleType("aiq_api.jobs.report_context")
    for name, obj in {
        "ReportContext": ReportContext, "ReportContextSource": ReportContextSource,
        "report_context_from_markdown": report_context_from_markdown,
        "resolve_authorized_report_context": resolve_authorized_report_context,
        "to_initial_files": to_initial_files, "report_output_metadata": report_output_metadata,
    }.items():
        setattr(rc, name, obj)
    sys.modules["aiq_api"] = pkg
    sys.modules["aiq_api.jobs"] = jobs
    sys.modules["aiq_api.jobs.access"] = access
    sys.modules["aiq_api.jobs.report_context"] = rc
    pkg.jobs = jobs  # type: ignore[attr-defined]
    jobs.access = access  # type: ignore[attr-defined]
    jobs.report_context = rc  # type: ignore[attr-defined]
    log.info("aiq_api_shim: installed (report follow-ups served from the adapter's S3/DynamoDB state)")
    return True

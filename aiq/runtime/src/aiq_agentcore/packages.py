# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Research Packages — the durable unit of research (11-architecture.md §4, schema aiq-agentcore/package/v1).

A package is built from the job record, the event journal (sources, artifacts, route, citations, usage) and the stored
report/ledger, written as ``tenants/<t>/packages/<job>/manifest.json`` (+ ``sources.json``) and projected into the
``PKG#`` library row. The manifest is the system of record for *results*; the journal remains the record of *execution*.
Everything here is tenant-scoped by construction: every key is derived from the verified tenant key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .contracts import Event, EventType, JobRecord
from .modellab_runtime import ROLES, human_name, price_tokens
from .store import JobStore, now_iso

SCHEMA = "aiq-agentcore/package/v1"
_H_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_MARK_RE = re.compile(r"\[(\d+)\]")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _title_from(report: str, question: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", report or "", re.MULTILINE)
    t = (m.group(1) if m else (question or "")).strip().strip("#").strip()
    return (t[:197] + "…") if len(t) > 200 else t


def headings_of(report: str) -> list[dict[str, Any]]:
    out = []
    for m in _H_RE.finditer(report or ""):
        out.append({"level": len(m.group(1)), "text": m.group(2)[:300]})
        if len(out) >= 60:
            break
    return out


def markers_by_number(report: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    for m in _MARK_RE.finditer(report or ""):
        n = int(m.group(1))
        counts[n] = counts.get(n, 0) + 1
    return counts


def _sources_from_events(events: list[Event]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.type == EventType.SOURCE and ev.data.get("source_id"):
            seen.setdefault(ev.data["source_id"], dict(ev.data))
        if ev.type == EventType.REPORT:
            for s in ev.data.get("sources") or []:
                if s.get("source_id"):
                    seen.setdefault(s["source_id"], dict(s))
    return list(seen.values())


def _sources_section_urls(report: str) -> dict[int, str]:
    """``[n] title — url`` / ``n. url`` lines under ``## Sources`` → {n: url} so markers map to sources."""
    out: dict[int, str] = {}
    m = re.search(r"^##\s+Sources\s*$", report or "", re.MULTILINE | re.IGNORECASE)
    if not m:
        return out
    for line in report[m.end() :].splitlines():
        mm = re.match(r"^\s*(?:\[(\d+)\]|(\d+)\.)\s*(.*)$", line)
        if not mm:
            continue
        n = int(mm.group(1) or mm.group(2))
        url = re.search(r"https?://\S+", mm.group(3))
        if url:
            out[n] = url.group(0).rstrip(").,;")
    return out


def build_manifest(
    store: JobStore,
    rec: JobRecord,
    events: list[Event],
    report: str | None,
    ledger: dict[str, Any] | None,
    *,
    matrix: dict[str, Any] | None,
    runtime_version: str | None,
    artifacts: list[dict[str, Any]] | None = None,
    selection: str | None = None,
) -> dict[str, Any]:
    prefix = store.package_prefix(rec.tenant_key, rec.job_id)
    route = next((e.data for e in events if e.type == EventType.ROUTE), {})
    usage_ev = next((e.data for e in reversed(events) if e.type == EventType.USAGE), {})
    cit = ledger or next((e.data for e in reversed(events) if e.type == EventType.CITATIONS), {}) or {}
    completed_at = next(
        (
            e.ts
            for e in reversed(events)
            if e.type in (EventType.COMPLETED, EventType.CANCELLED) or (e.type == EventType.ERROR and e.data.get("terminal"))
        ),
        None,
    )
    sources = _sources_from_events(events)
    report = report or ""
    numbered = _sources_section_urls(report)
    marks = markers_by_number(report)
    by_url: dict[str, dict[str, Any]] = {s["url"]: s for s in sources if s.get("url")}
    for n, url in numbered.items():
        s = by_url.get(url)
        if s is not None and n in marks:
            s.setdefault("cited_by", []).append(f"[{n}]")
    # models per role (resolved at submit) or deployment defaults at the time
    roles_in = rec.models or {}
    models: dict[str, Any] = {}
    for role in ROLES:
        c = roles_in.get(role) or {}
        mid = c.get("model_id") or os.environ.get(f"AIQ_MODEL_{role.upper()}") or os.environ.get("AIQ_MODEL_SHALLOW", "")
        lane = c.get("lane", "converse")
        models[role] = {
            "model_id": mid,
            "lane": "bedrock_runtime" if lane == "converse" else "bedrock_mantle",
            "human_name": c.get("human_name") or human_name(matrix, mid, lane),
            "provider": mid.split(".")[1].split(".")[0] if mid.count(".") >= 2 else mid.split(".")[0],
            "region": os.environ.get("AIQ_REGION", "us-east-1"),
            "nat_llm_name": f"{role}_llm",
            "max_tokens": None,
            "reasoning_effort": None,
        }
    in_tok = int(usage_ev.get("input_tokens") or rec.usage.get("input_tokens", 0) or 0)
    out_tok = int(usage_ev.get("output_tokens") or rec.usage.get("output_tokens", 0) or 0)
    # cost: attribute all tokens to the writer/planner mix is unknowable per model here; price by the planner rate (bulk)
    # and the writer rate for the output share (honest approximation labelled "computed")
    lines = []
    total = 0.0
    priced = True
    plan = roles_in.get("planner") or {"model_id": models["planner"]["model_id"], "lane": "converse"}
    wri = roles_in.get("writer") or {"model_id": models["writer"]["model_id"], "lane": "converse"}
    p_in = price_tokens(matrix, plan["model_id"], plan.get("lane", "converse"), int(in_tok * 0.8), int(out_tok * 0.6))
    p_out = price_tokens(matrix, wri["model_id"], wri.get("lane", "converse"), int(in_tok * 0.2), int(out_tok * 0.4))
    for label, mid, pr in (("planner+researchers", plan["model_id"], p_in), ("writer", wri["model_id"], p_out)):
        if pr.get("usd") is None:
            priced = False
            continue
        total += pr["usd"]
        lines.append(
            {
                "component": f"bedrock:{mid}:tokens ({label})",
                "unit": "USD",
                "quantity": 1,
                "unit_price_usd": pr["usd"],
                "usd": pr["usd"],
                "model_id": mid,
            }
        )
    searches = int(usage_ev.get("searches") or rec.usage.get("searches", 0) or 0)
    pages = int(usage_ev.get("pages") or rec.usage.get("pages", 0) or 0)
    if searches:
        lines.append(
            {
                "component": "agentcore:web_search:queries",
                "unit": "query",
                "quantity": searches,
                "unit_price_usd": 0.007,
                "usd": round(searches * 0.007, 4),
            }
        )
        total += searches * 0.007
    if pages:
        lines.append(
            {
                "component": "agentcore:browser:sessions",
                "unit": "session",
                "quantity": pages,
                "unit_price_usd": 0.004,
                "usd": round(pages * 0.004, 4),
            }
        )
        total += pages * 0.004
    report_bytes = report.encode("utf-8")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "package_id": rec.job_id,
        "job_id": rec.job_id,
        "tenant_key": rec.tenant_key,
        "title": rec.title or _title_from(report, rec.question) or None,
        "question": rec.question,
        "clarification": [{"q": x.get("q", ""), "a": x.get("a", "")} for x in (rec.plan or {}).get("clarification", [])],
        "mode": rec.mode.value,
        "depth": route.get("depth") if route.get("depth") in ("shallow", "deep", "meta") else None,
        "data_sources": [s for s in (route.get("data_sources") or []) if isinstance(s, str)],
        "status": rec.status.value,
        "error": rec.error,
        "timing": {
            "created_at": rec.created_at,
            "started_at": rec.created_at,
            "completed_at": completed_at,
            "updated_at": now_iso(),
            "duration_seconds": usage_ev.get("seconds"),
            "clarification_turns": len((rec.plan or {}).get("clarification", [])),
        },
        "runtime": {
            "adapter_version": __version__,
            "source_commit": os.environ.get("AIQ_SOURCE_COMMIT"),
            "upstream_ref": os.environ.get("AIQ_UPSTREAM_REF"),
            "runtime_version": runtime_version,
            "runtime_session_id": rec.runtime_session_id,
            "conversation_id": rec.conversation_id,
            "region": os.environ.get("AIQ_REGION", "us-east-1"),
        },
        "models": {
            "selection": selection
            or (
                "session_override"
                if any((roles_in.get(r) or {}).get("source") == "session_override" for r in ROLES)
                else "deploy_default"
            ),
            "roles": models,
        },
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "llm_calls": int(usage_ev.get("llm_calls") or 0),
            "truncated_outputs": int(usage_ev.get("truncated_outputs") or 0),
            "searches": searches,
            "pages": pages,
            "retrievals": int(usage_ev.get("retrievals") or 0),
            "browser_sessions": pages,
        },
        "cost": {
            "currency": "USD",
            "total_usd": round(total, 6),
            "lines": lines,
            "price_source": {
                "kind": "aws_price_list_api" if priced else "estimate",
                "retrieved_at": (matrix or {}).get("generated_at") or now_iso(),
                "offer_codes": list(((matrix or {}).get("offer_versions") or {}).keys()),
                "region": os.environ.get("AIQ_REGION", "us-east-1"),
            },
            "confidence": "computed" if priced else "estimated",
        },
        "report": {
            "present": bool(report),
            "key": prefix + "report.md" if report else None,
            "sha256": _sha(report_bytes) if report else None,
            "size_bytes": len(report_bytes) if report else None,
            "word_count": len(report.split()) if report else None,
            "format": "text/markdown",
            "headings": headings_of(report),
            "summary": None,
            "guardrail_action": None,
        },
        "citations": {
            "ledger_key": prefix + "ledger.json" if cit else None,
            "verified": int(cit.get("verified") or 0),
            "unverified": int(cit.get("unverified") or 0),
            "items": [
                {k: c.get(k) for k in ("marker", "source_id", "url", "verified", "match_level", "reason") if k in c}
                for c in (cit.get("citations") or [])
            ],
        },
        "sources": [
            {
                k: s.get(k)
                for k in (
                    "source_id",
                    "url",
                    "title",
                    "kind",
                    "retrieved_at",
                    "tool",
                    "snippet",
                    "document_key",
                    "content_sha256",
                    "cited_by",
                )
                if s.get(k) is not None
            }
            for s in sources
        ],
        "artifacts": artifacts or [],
        "lineage": {
            "parent_package_id": rec.parent_job_id,
            "relation": rec.relation or "root",
            "root_package_id": rec.parent_job_id or rec.job_id,
            "result_kind": None,
        },
        "organization": {"tags": [], "pinned": False, "project": None, "notes": None},
        "exports": [],
        "evals": [],
        "retention": {"expires_at": None, "s3_prefix": prefix, "deleted_at": None},
    }
    return manifest


def summary_of(manifest: dict[str, Any], index_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """The PackageSummary the library and the chat card show (≤ 4 KB)."""
    ix = index_row or {}
    return {
        "job_id": manifest["job_id"],
        "title": ix.get("title") or manifest.get("title"),
        "question": manifest["question"][:300],
        "mode": manifest["mode"],
        "depth": manifest.get("depth"),
        "status": manifest["status"],
        "created_at": manifest["timing"]["created_at"],
        "completed_at": manifest["timing"].get("completed_at"),
        "models": {
            r: {"model_id": v["model_id"], "human_name": v.get("human_name"), "lane": v.get("lane")}
            for r, v in manifest["models"]["roles"].items()
        },
        "cost_usd": manifest["cost"]["total_usd"],
        "cost_confidence": manifest["cost"]["confidence"],
        "counts": {
            "sources": len(manifest["sources"]),
            "citations_verified": manifest["citations"]["verified"],
            "citations_unverified": manifest["citations"]["unverified"],
            "artifacts": len(manifest["artifacts"]),
            "exports": len(manifest.get("exports") or []),
        },
        "tags": ix.get("tags") or manifest["organization"]["tags"],
        "pinned": bool(ix.get("pinned", manifest["organization"]["pinned"])),
        "lineage": manifest["lineage"],
        "conversation_id": manifest["runtime"].get("conversation_id"),
        "duration_seconds": manifest["timing"].get("duration_seconds"),
        "manifest_key": manifest["retention"]["s3_prefix"] + "manifest.json",
    }


def write_manifest(store: JobStore, manifest: dict[str, Any]) -> str:
    prefix = manifest["retention"]["s3_prefix"]
    key = prefix + "manifest.json"
    store.put_json(key, manifest)
    store.put_json(prefix + "sources.json", manifest["sources"])
    return key


def load_manifest(store: JobStore, tenant_key: str, job_id: str) -> dict[str, Any] | None:
    key = store.package_prefix(tenant_key, job_id) + "manifest.json"
    if store.head(key) is None:
        return None
    return json.loads(store.get_bytes(key))


def finalize(
    store: JobStore,
    rec: JobRecord,
    *,
    matrix: dict[str, Any] | None,
    runtime_version: str | None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build + write the manifest for a job at a terminal (or clarifying/running) state and refresh the PKG# row."""
    events = store.read_events(rec.job_id, after=0, limit=2000)
    report = store.get_text(rec.report_key) if rec.report_key and store.head(rec.report_key) else None
    ledger = json.loads(store.get_text(rec.ledger_key)) if rec.ledger_key and store.head(rec.ledger_key) else None
    existing = load_manifest(store, rec.tenant_key, rec.job_id)
    manifest = build_manifest(
        store,
        rec,
        events,
        report,
        ledger,
        matrix=matrix,
        runtime_version=runtime_version,
        artifacts=artifacts if artifacts is not None else (existing or {}).get("artifacts"),
    )
    if existing:  # user-owned fields and exports persist across refreshes (exports only while the report is unchanged)
        manifest["organization"] = existing.get("organization") or manifest["organization"]
        same_report = (existing.get("report") or {}).get("sha256") == manifest["report"].get("sha256")
        manifest["exports"] = (existing.get("exports") or []) if same_report else []
        manifest["evals"] = existing.get("evals") or []
        if existing.get("title") and not rec.title:
            manifest["title"] = existing["title"]
    # copy report/ledger into the package prefix when they were written elsewhere (phase-2 layout)
    prefix = manifest["retention"]["s3_prefix"]
    if rec.report_key and rec.report_key != prefix + "report.md" and store.head(rec.report_key):
        store.copy(rec.report_key, prefix + "report.md")
    if rec.ledger_key and rec.ledger_key != prefix + "ledger.json" and store.head(rec.ledger_key):
        store.copy(rec.ledger_key, prefix + "ledger.json")
    key = write_manifest(store, manifest)
    summ = summary_of(manifest, store.index_for_job(rec.tenant_key, rec.job_id))
    store.update_job(rec.tenant_key, rec.job_id, cost_usd=manifest["cost"]["total_usd"], title=manifest.get("title"))
    rec2 = store.get_job(rec.tenant_key, rec.job_id) or rec
    store.put_package_index(
        rec2,
        {
            **summ,
            "manifest_key": key,
            "completed_at": manifest["timing"].get("completed_at"),
            "depth": manifest.get("depth"),
            "root_package_id": manifest["lineage"]["root_package_id"],
        },
    )
    return manifest


def compare(a: dict[str, Any], b: dict[str, Any], report_a: str, report_b: str) -> dict[str, Any]:
    """Deterministic comparison: sections by heading text, sources by URL, first sentences of shared sections."""
    ha = [h["text"] for h in a["report"].get("headings", []) if h["level"] == 2]
    hb = [h["text"] for h in b["report"].get("headings", []) if h["level"] == 2]
    norm = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()  # noqa: E731
    na, nb = {norm(h): h for h in ha}, {norm(h): h for h in hb}
    sections = [
        {"title": na.get(k) or nb.get(k), "in_a": k in na, "in_b": k in nb} for k in list(dict.fromkeys(list(na) + list(nb)))
    ]
    ua = {s["url"]: s for s in a["sources"] if s.get("url")}
    ub = {s["url"]: s for s in b["sources"] if s.get("url")}
    shared = [ua[u] for u in ua if u in ub]
    only_a = [ua[u] for u in ua if u not in ub]
    only_b = [ub[u] for u in ub if u not in ua]

    def first_sentences(report: str) -> dict[str, str]:
        out: dict[str, str] = {}
        parts = re.split(r"^##\s+", report or "", flags=re.MULTILINE)
        for p in parts[1:]:
            title, _, body = p.partition("\n")
            sent = re.split(r"(?<=[.!?])\s+", body.strip().replace("\n", " "))
            out[norm(title)] = (sent[0] if sent else "")[:400]
        return out

    fa, fb = first_sentences(report_a), first_sentences(report_b)
    claims = []
    for k in sections:
        key = norm(k["title"] or "")
        if key in fa or key in fb:
            claims.append(
                {
                    "section": k["title"],
                    "a": fa.get(key),
                    "b": fb.get(key),
                    "markers_a": sorted(set(_MARK_RE.findall(fa.get(key) or ""))),
                    "markers_b": sorted(set(_MARK_RE.findall(fb.get(key) or ""))),
                }
            )
    return {
        "a": summary_of(a),
        "b": summary_of(b),
        "sections": sections,
        "sources": {"shared": shared, "only_a": only_a, "only_b": only_b},
        "claims": claims,
        "run": {
            "cost_a": a["cost"]["total_usd"],
            "cost_b": b["cost"]["total_usd"],
            "seconds_a": a["timing"].get("duration_seconds"),
            "seconds_b": b["timing"].get("duration_seconds"),
            "citations_a": a["citations"]["verified"],
            "citations_b": b["citations"]["verified"],
            "words_a": a["report"].get("word_count"),
            "words_b": b["report"].get("word_count"),
        },
    }


def new_artifact_record(
    job_id: str,
    name: str,
    data: bytes,
    kind: str,
    storage_key: str,
    *,
    phase: str,
    sandbox_path: str | None = None,
    caption: str | None = None,
) -> dict[str, Any]:
    import mimetypes

    ext = os.path.splitext(name)[1].lower()
    mime = (
        {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".csv": "text/csv",
            ".json": "application/json",
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".pdf": "application/pdf",
        }.get(ext)
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    sha = _sha(data)
    return {
        "artifact_id": "art_" + hashlib.sha256(f"{job_id}|{name}|{sha}".encode()).hexdigest()[:32],
        "job_id": job_id,
        "kind": kind if kind in ("image", "table", "dataset", "notebook", "document", "text", "archive", "other") else "other",
        "mime_type": mime,
        "filename": name,
        "sandbox_path": sandbox_path or name,
        "storage_key": storage_key,
        "storage_uri": None,
        "sha256": sha,
        "size_bytes": len(data),
        "title": None,
        "caption": caption,
        "inline": mime.startswith("image/") and len(data) <= 1_000_000,
        "workflow": None,
        "source_tool_call_id": None,
        "provenance": {
            "command": None,
            "script_sha256": None,
            "input_file_hashes": {},
            "package_snapshot": [],
            "sandbox_provider": "agentcore_code_interpreter",
        },
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "available",
        "capture_phase": phase,
        "referenced_in_report": None,
    }

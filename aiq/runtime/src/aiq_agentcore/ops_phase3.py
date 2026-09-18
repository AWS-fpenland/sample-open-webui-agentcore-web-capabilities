# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase-3 operations: Research Packages, exports, Model Lab, evaluation (11-architecture.md §10).

Every op derives the tenant from the verified principal, reads only under ``TENANT#<t>`` / ``tenants/<t>/`` and
answers ``not_found`` for anything outside it (indistinguishable from absence, as the phase-1 ops do).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any, Callable

from .contracts import EXPORT_FORMATS, ChatMessage, ErrorCode, EventType, InvokeRequest, Op, Principal, ResearchMode
from .engine_base import EngineRequest
from . import modellab_runtime, packages
from .store import JobStore, now_iso

_store: JobStore | None = None


def _st() -> JobStore:
    from . import app as appmod

    return appmod.store()


def _line(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False) + "\n"


def _err(code: ErrorCode, message: str, **extra: Any) -> str:
    from .contracts import ApiError

    return _line(
        {
            "type": "error",
            "seq": -1,
            "data": {"error": ApiError(code=code, message=message).model_dump(), "terminal": True, **extra},
        }
    )


def _row_to_summary(row: dict[str, Any]) -> dict[str, Any]:
    models = row.get("models") or {}
    return {
        "job_id": row["job_id"],
        "title": row.get("title") or None,
        "question": row.get("question") or "",
        "mode": row.get("mode"),
        "depth": row.get("depth"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
        "updated_at": row.get("updated_at"),
        "models": {
            r: {"model_id": v.get("model_id"), "human_name": v.get("human_name"), "lane": v.get("lane")}
            for r, v in models.items()
            if isinstance(v, dict)
        },
        "cost_usd": row.get("cost_usd"),
        "counts": row.get("counts") or {},
        "tags": row.get("tags") or [],
        "pinned": bool(row.get("pinned")),
        "lineage": row.get("lineage") or {},
        "conversation_id": row.get("conversation_id"),
        "error": row.get("error"),
        "eval_id": row.get("eval_id"),
    }


# ------------------------------------------------------------------------------------------- packages --


async def packages_list(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    limit = body.limit or 50
    items: list[dict[str, Any]] = []
    cursor = body.cursor
    q = (body.q or "").lower().strip()
    for _ in range(6):  # up to 6 pages when filters are narrow
        rows, cursor = await asyncio.get_running_loop().run_in_executor(
            None, lambda c=cursor: st.list_packages(principal.tenant_key, limit=limit, cursor=c)
        )
        for r in rows:
            if body.status and r.get("status") != body.status:
                continue
            if (
                "mode" in body.model_fields_set and r.get("mode") != body.mode.value
            ):  # `mode` defaults to auto; only filter when sent
                continue
            if body.tag and body.tag not in (r.get("tags") or []):
                continue
            if body.pinned is not None and bool(r.get("pinned")) != body.pinned:
                continue
            if q and q not in ((r.get("title") or "") + " " + (r.get("question") or "")).lower():
                continue
            items.append(_row_to_summary(r))
        if len(items) >= limit or cursor is None:
            break
    yield _line({"type": "packages", "seq": 0, "data": {"items": items[:limit], "next_cursor": cursor}})


async def packages_get(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    if not body.job_id:
        yield _err(ErrorCode.INVALID_REQUEST, "job_id is required")
        return
    rec = st.get_job(principal.tenant_key, body.job_id)
    if rec is None:
        yield _err(ErrorCode.NOT_FOUND, "package not found")
        return
    manifest = packages.load_manifest(st, principal.tenant_key, body.job_id)
    if manifest is None:  # not finalised yet (running) → build a transient view
        manifest = packages.build_manifest(
            st,
            rec,
            st.read_events(body.job_id, after=0, limit=2000),
            None,
            None,
            matrix=modellab_runtime.load_matrix(st),
            runtime_version=os.environ.get("AIQ_RUNTIME_VERSION"),
        )
    idx = st.index_for_job(principal.tenant_key, body.job_id) or {}
    manifest["organization"] = {
        **manifest.get("organization", {}),
        "tags": idx.get("tags") or manifest.get("organization", {}).get("tags", []),
        "pinned": bool(idx.get("pinned", False)),
    }
    if idx.get("title"):
        manifest["title"] = idx["title"]
    report_key = manifest.get("report", {}).get("key")
    report_md = st.get_text(report_key) if report_key and st.head(report_key) else ""
    tail = [
        e.model_dump(mode="json")
        for e in st.read_events(body.job_id, after=max(0, rec.last_seq - 40), limit=60)
        if e.type not in (EventType.DELTA,)
    ]
    yield _line(
        {
            "type": "package",
            "seq": rec.last_seq,
            "job_id": rec.job_id,
            "data": {
                "manifest": manifest,
                "report_md": report_md,
                "events_tail": tail,
                "summary": packages.summary_of(manifest, idx),
            },
        }
    )


async def packages_update(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    if not body.job_id:
        yield _err(ErrorCode.INVALID_REQUEST, "job_id is required")
        return
    fields: dict[str, Any] = {}
    if body.tags is not None:
        fields["tags"] = body.tags
    if body.pinned is not None:
        fields["pinned"] = body.pinned
    if body.title is not None:
        fields["title"] = body.title.strip()[:200]
    if not fields and body.notes is None:
        yield _err(ErrorCode.INVALID_REQUEST, "nothing to update (tags, pinned, title, notes)")
        return
    row = (
        st.update_package_index(principal.tenant_key, body.job_id, **fields)
        if fields
        else st.index_for_job(principal.tenant_key, body.job_id)
    )
    if row is None:
        yield _err(ErrorCode.NOT_FOUND, "package not found")
        return
    manifest = packages.load_manifest(st, principal.tenant_key, body.job_id)
    if manifest is not None:
        org = manifest.setdefault("organization", {"tags": [], "pinned": False, "project": None, "notes": None})
        org["tags"], org["pinned"] = row.get("tags") or [], bool(row.get("pinned"))
        if body.notes is not None:
            org["notes"] = body.notes
        if body.title is not None:
            manifest["title"] = fields["title"]
        packages.write_manifest(st, manifest)
    yield _line({"type": "package", "seq": 0, "job_id": body.job_id, "data": {"summary": _row_to_summary(row)}})


async def packages_delete(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    if not body.job_id:
        yield _err(ErrorCode.INVALID_REQUEST, "job_id is required")
        return
    rec = st.get_job(principal.tenant_key, body.job_id)
    if rec is None:
        yield _err(ErrorCode.NOT_FOUND, "package not found")
        return
    if not rec.status.terminal:
        yield _err(ErrorCode.INVALID_REQUEST, f"job is {rec.status.value}; cancel it before deleting")
        return
    loop = asyncio.get_running_loop()
    n1 = await loop.run_in_executor(None, st.delete_prefix, st.package_prefix(principal.tenant_key, body.job_id))
    n2 = await loop.run_in_executor(None, st.delete_prefix, f"tenants/{principal.tenant_key}/jobs/{body.job_id}/")
    await loop.run_in_executor(None, st.delete_package, principal.tenant_key, body.job_id)
    yield _line(
        {"type": "package.deleted", "seq": 0, "job_id": body.job_id, "data": {"job_id": body.job_id, "objects_deleted": n1 + n2}}
    )


async def packages_compare(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    if not body.job_id or not body.other_job_id:
        yield _err(ErrorCode.INVALID_REQUEST, "job_id and other_job_id are required")
        return
    out = []
    for jid in (body.job_id, body.other_job_id):
        if st.get_job(principal.tenant_key, jid) is None:
            yield _err(ErrorCode.NOT_FOUND, "package not found")
            return
        m = packages.load_manifest(st, principal.tenant_key, jid)
        if m is None:
            yield _err(ErrorCode.INVALID_REQUEST, f"package {jid} has no manifest yet (still running?)")
            return
        rk = m.get("report", {}).get("key")
        out.append((m, st.get_text(rk) if rk and st.head(rk) else ""))
    (a, ra), (b, rb) = out
    yield _line({"type": "comparison", "seq": 0, "data": packages.compare(a, b, ra, rb)})


async def packages_rerun(
    principal: Principal,
    body: InvokeRequest,
    session_id: str | None,
    start_background: Callable,
    publisher_factory: Callable,
    resolve_models: Callable,
) -> AsyncIterator[str]:
    st = _st()
    if not body.job_id:
        yield _err(ErrorCode.INVALID_REQUEST, "job_id is required")
        return
    parent = st.get_job(principal.tenant_key, body.job_id)
    if parent is None:
        yield _err(ErrorCode.NOT_FOUND, "package not found")
        return
    roles, err = resolve_models(principal, body)
    if err:
        yield _line(err)
        return
    mode = ResearchMode.SHALLOW if parent.mode == ResearchMode.SHALLOW else ResearchMode.DEEP
    rec, created = st.create_job(
        tenant_key=principal.tenant_key,
        mode=mode,
        question=parent.question,
        runtime_session_id=session_id,
        client_request_id=body.client_request_id or f"rerun-{uuid.uuid4().hex}",
        conversation_id=body.conversation_id or parent.conversation_id,
        models=roles,
        parent_job_id=parent.job_id,
        relation="rerun",
    )
    if created:
        req = EngineRequest(
            principal=principal,
            mode=mode,
            messages=[ChatMessage(role="user", content=parent.question)],
            job_id=rec.job_id,
            collection=None,
            data_sources=body.data_sources,
            models=rec.models,
            publish_artifact=publisher_factory(principal, rec.job_id),
        )
        start_background(principal, rec.job_id, req)
    yield _line(
        {
            "type": EventType.JOB_ACCEPTED.value,
            "seq": 0,
            "job_id": rec.job_id,
            "data": {
                "mode": mode.value,
                "created": created,
                "status": rec.status.value,
                "parent_job_id": parent.job_id,
                "relation": "rerun",
                "models": rec.models,
            },
        }
    )


# --------------------------------------------------------------------------------------------- export --


async def export(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    fmt = (body.format or "").lower()
    if not body.job_id or fmt not in EXPORT_FORMATS:
        yield _err(ErrorCode.INVALID_REQUEST, f"job_id and format ∈ {{{', '.join(EXPORT_FORMATS)}}} are required")
        return
    rec = st.get_job(principal.tenant_key, body.job_id)
    if rec is None:
        yield _err(ErrorCode.NOT_FOUND, "package not found")
        return
    manifest = packages.load_manifest(st, principal.tenant_key, body.job_id)
    if manifest is None or not manifest.get("report", {}).get("present"):
        yield _err(ErrorCode.INVALID_REQUEST, "package has no report to export yet")
        return
    theme = body.theme or "dark"
    prefix = manifest["retention"]["s3_prefix"]
    try:
        from .exports import service as exp
        from .exports.bundle import PackageBundle
    except Exception as e:  # noqa: BLE001
        yield _err(ErrorCode.INTERNAL, f"export renderers unavailable in this image ({e.__class__.__name__})")
        return
    manifest_fmt = exp.MANIFEST_FORMAT.get(fmt, fmt) if hasattr(exp, "MANIFEST_FORMAT") else fmt
    filename = exp.export_filename(body.job_id, fmt, theme)
    content_type = exp.CONTENT_TYPES[fmt]
    # cache: same format + theme rendered from the current report (finalize() clears `exports` when the report changes)
    cached = next(
        (
            x
            for x in manifest.get("exports", [])
            if x.get("format") == manifest_fmt and (x.get("theme") or "dark") == theme and st.head(x["key"])
        ),
        None,
    )
    t0 = time.monotonic()
    was_cached = cached is not None
    if cached is None:
        report_md = st.get_text(manifest["report"]["key"])
        lk = manifest["citations"].get("ledger_key")
        ledger = json.loads(st.get_text(lk)) if lk and st.head(lk) else None
        arts: list[tuple[dict[str, Any], bytes]] = []
        for a in manifest.get("artifacts", [])[:20]:
            if a.get("storage_key") and st.head(a["storage_key"]):
                arts.append((a, st.get_bytes(a["storage_key"])))
        bundle = PackageBundle.from_parts(
            manifest=manifest, report_md=report_md, ledger=ledger, sources=manifest.get("sources", []), artifacts=arts
        )
        pdf_backend = None
        if fmt in ("pdf", "zip") and os.environ.get("AIQ_PDF_BACKEND", "browser") == "browser":
            from .exports.render_pdf_browser import print_html_to_pdf

            region = os.environ.get("AIQ_REGION") or os.environ.get("AWS_REGION", "us-east-1")

            async def pdf_backend(html: str) -> bytes:  # noqa: E306
                return await print_html_to_pdf(html, region=region)

        try:
            rendered = await exp.render(bundle, fmt, theme=theme, pdf_backend=pdf_backend)
        except Exception as e:  # noqa: BLE001
            yield _err(ErrorCode.INTERNAL, f"export failed: {e.__class__.__name__}: {str(e)[:200]}")
            return
        key = prefix + f"exports/{rendered.filename}"
        st.put_bytes(key, rendered.data, rendered.content_type)
        cached = rendered.manifest_record(now_iso(), key=key)  # schema-shaped ExportRecord
        cached.setdefault("theme", theme)
        manifest["exports"] = [
            x
            for x in manifest.get("exports", [])
            if not (x.get("format") == manifest_fmt and (x.get("theme") or "dark") == theme)
        ] + [cached]
        packages.write_manifest(st, manifest)
        idx = st.index_for_job(principal.tenant_key, body.job_id) or {}
        counts = dict(idx.get("counts") or {})
        counts["exports"] = len(manifest["exports"])
        st.update_package_index(principal.tenant_key, body.job_id, counts=counts)
        filename, content_type = rendered.filename, rendered.content_type
    url = st.presign(cached["key"], filename=filename, content_type=content_type, ttl=600)
    yield _line(
        {
            "type": "export",
            "seq": 0,
            "job_id": body.job_id,
            "data": {
                "format": fmt,
                "theme": theme,
                "filename": filename,
                "content_type": content_type,
                "sha256": cached["sha256"],
                "size": cached["size_bytes"],
                "size_bytes": cached["size_bytes"],
                "derived": cached.get("derived", False),
                "generator": cached.get("generator"),
                "key": cached["key"],
                "url": url,
                "expires_in": 600,
                "cached": was_cached,
                "backend": cached.get("generator"),
                "ms": int((time.monotonic() - t0) * 1000),
            },
        }
    )


async def artifact_url(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    if not body.job_id or not body.artifact_id:
        yield _err(ErrorCode.INVALID_REQUEST, "job_id and artifact_id are required")
        return
    if st.get_job(principal.tenant_key, body.job_id) is None:
        yield _err(ErrorCode.NOT_FOUND, "package not found")
        return
    manifest = packages.load_manifest(st, principal.tenant_key, body.job_id) or {}
    art = next((a for a in manifest.get("artifacts", []) if a.get("artifact_id") == body.artifact_id), None)
    if art is None:  # running job: artifacts live in the journal until finalize
        for ev in st.read_events(body.job_id, after=0, limit=2000):
            if ev.type == EventType.ARTIFACT and (ev.data.get("record") or {}).get("artifact_id") == body.artifact_id:
                art = ev.data["record"]
                break
    if art is None or not art.get("storage_key", "").startswith(
        (f"packages/{principal.tenant_key}/", f"tenants/{principal.tenant_key}/")
    ):
        yield _err(ErrorCode.NOT_FOUND, "artifact not found")
        return
    url = st.presign(art["storage_key"], filename=art["filename"], content_type=art["mime_type"], ttl=600)
    yield _line(
        {"type": "artifact.url", "seq": 0, "job_id": body.job_id, "data": {"url": url, "expires_in": 600, "artifact": art}}
    )


# --------------------------------------------------------------------------------------------- models --


def _trim_entry(e: dict[str, Any]) -> dict[str, Any]:
    caps = {
        k: {"ok": v.get("ok"), "error_code": v.get("error_code"), "ms": v.get("ms")}
        for k, v in (e.get("capabilities") or {}).items()
    }
    return {
        k: e.get(k)
        for k in (
            "id",
            "lane",
            "base_model_id",
            "name",
            "provider",
            "family",
            "region",
            "routing",
            "offered",
            "exclusion_reasons",
            "latency",
            "roles",
            "roles_selectable",
            "attempts",
            "mantle_status_reason",
        )
    } | {
        "price": {k: (e.get("price") or {}).get(k) for k in ("input_per_1m", "output_per_1m", "source", "routing_priced")},
        "capabilities": caps,
        "capability_errors": {
            k: v.get("error_message") for k, v in (e.get("capabilities") or {}).items() if v.get("error_message")
        },
    }


async def models(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    mx = modellab_runtime.load_matrix(st)
    prefs = st.get_prefs(principal.tenant_key)
    defaults = modellab_runtime.deployment_defaults()
    resolved, _ = modellab_runtime.resolve(mx, None, prefs)
    entries = [_trim_entry(e) for e in (mx or {}).get("entries", [])]
    if body.role:
        entries = [e for e in entries if (e.get("roles_selectable") or {}).get(body.role)]
    yield _line(
        {
            "type": "models",
            "seq": 0,
            "data": {
                "matrix": {
                    k: (mx or {}).get(k) for k in ("generated_at", "probed_as", "digest", "summary", "offer_versions", "region")
                }
                | {"status": modellab_runtime.matrix_status()},
                "entries": entries,
                "defaults": {r: v["model_id"] for r, v in defaults.items()},
                "defaults_named": {
                    r: {**v, "human_name": modellab_runtime.human_name(mx, v["model_id"], v["lane"])} for r, v in defaults.items()
                },
                "prefs": prefs,
                "resolved": resolved,
                "offered_by_role": {r: modellab_runtime.offered_for_role(mx, r) for r in modellab_runtime.ROLES},
                "estimate_deep_run_usd": modellab_runtime.estimate_deep_run(mx, resolved),
            },
        }
    )


async def models_prefs(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    mx = modellab_runtime.load_matrix(st)
    if body.models is None:
        yield _err(ErrorCode.INVALID_REQUEST, "models is required (use {} to clear)")
        return
    chosen = {r: c.model_dump() for r, c in body.models.items()}
    problems = modellab_runtime.validate(mx, chosen)
    if problems:
        yield _err(
            ErrorCode.INVALID_REQUEST,
            "preferences refused: " + "; ".join(f"{p['role']}: {p['model_id']} — {p['reason']}" for p in problems)[:800],
            problems=problems,
        )
        return
    for r, c in chosen.items():
        c["human_name"] = modellab_runtime.human_name(mx, c["model_id"], c.get("lane", "converse"))
    st.put_prefs(principal.tenant_key, chosen)
    yield _line({"type": "models.prefs", "seq": 0, "data": {"prefs": chosen}})


async def models_validate(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    mx = modellab_runtime.load_matrix(st)
    chosen = {r: c.model_dump() for r, c in (body.models or {}).items()}
    problems = modellab_runtime.validate(mx, chosen)
    resolved, _ = modellab_runtime.resolve(mx, chosen or None, st.get_prefs(principal.tenant_key))
    yield _line(
        {
            "type": "models.validated",
            "seq": 0,
            "data": {
                "ok": not problems,
                "problems": problems,
                "resolved": resolved,
                "estimate_usd": modellab_runtime.estimate_deep_run(mx, resolved),
            },
        }
    )


# ----------------------------------------------------------------------------------------------- eval --


def _questions() -> list[dict[str, Any]]:
    with resources.files("aiq_agentcore").joinpath("data/eval_questions.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("questions", [])


async def eval_start(
    principal: Principal,
    body: InvokeRequest,
    session_id: str | None,
    start_background: Callable,
    publisher_factory: Callable,
    resolve_models: Callable,
) -> AsyncIterator[str]:
    st = _st()
    roles, err = resolve_models(principal, body)
    if err:
        yield _line(err)
        return
    qs = _questions()
    if body.question_ids:
        qs = [q for q in qs if q.get("id") in body.question_ids]
    if not qs:
        yield _err(ErrorCode.INVALID_REQUEST, "no questions selected")
        return
    eval_id = "eval_" + uuid.uuid4().hex[:16]
    job_ids = []
    for q in qs:
        mode = ResearchMode.DEEP if (q.get("mode") or q.get("depth")) == "deep" else ResearchMode.SHALLOW
        rec, created = st.create_job(
            tenant_key=principal.tenant_key,
            mode=mode,
            question=q["question"],
            runtime_session_id=session_id,
            client_request_id=f"{eval_id}-{q.get('id')}",
            conversation_id=body.conversation_id,
            models=roles,
            relation="eval",
            eval_id=eval_id,
        )
        if created:
            req = EngineRequest(
                principal=principal,
                mode=mode,
                messages=[ChatMessage(role="user", content=q["question"])],
                job_id=rec.job_id,
                models=rec.models,
                publish_artifact=publisher_factory(principal, rec.job_id),
            )
            start_background(principal, rec.job_id, req)
        job_ids.append(
            {
                "question_id": q.get("id"),
                "job_id": rec.job_id,
                "mode": mode.value,
                "expected_keywords": q.get("expected_keywords"),
            }
        )
    st.put_eval(
        principal.tenant_key,
        eval_id,
        {
            "created_at": now_iso(),
            "models": roles,
            "jobs": job_ids,
            "judge": body.judge.model_dump() if body.judge else None,
            "judge_note": "deterministic metrics only (citations verified, sources, cost, latency, keyword hits); "
            "no LLM judge in this build"
            if not body.judge
            else "LLM judge requested; not executed in this build",
        },
    )
    yield _line(
        {
            "type": "eval.started",
            "seq": 0,
            "data": {"eval_id": eval_id, "job_ids": [j["job_id"] for j in job_ids], "jobs": job_ids, "models": roles},
        }
    )


async def eval_list(principal: Principal, body: InvokeRequest) -> AsyncIterator[str]:
    st = _st()
    items = []
    for ev in st.list_evals(principal.tenant_key):
        rows = []
        done = 0
        for j in ev.get("jobs") or []:
            rec = st.get_job(principal.tenant_key, j["job_id"])
            idx = st.index_for_job(principal.tenant_key, j["job_id"]) or {}
            counts = idx.get("counts") or {}
            report_hit = None
            if rec and rec.report_key and j.get("expected_keywords") and st.head(rec.report_key):
                txt = st.get_text(rec.report_key).lower()
                kws = [k for k in j["expected_keywords"] if isinstance(k, str)]
                report_hit = round(sum(1 for k in kws if k.lower() in txt) / len(kws), 2) if kws else None
            if rec and rec.status.terminal:
                done += 1
            rows.append(
                {
                    "question_id": j.get("question_id"),
                    "job_id": j["job_id"],
                    "mode": j.get("mode"),
                    "status": rec.status.value if rec else "missing",
                    "cost_usd": rec.cost_usd if rec else None,
                    "seconds": (rec.usage or {}).get("seconds") if rec else None,
                    "citations_verified": counts.get("citations_verified"),
                    "citations_unverified": counts.get("citations_unverified"),
                    "sources": counts.get("sources"),
                    "keyword_hit_rate": report_hit,
                    "error": rec.error if rec else None,
                }
            )
        items.append(
            {
                "eval_id": ev.get("eval_id"),
                "created_at": ev.get("created_at"),
                "models": ev.get("models"),
                "judge": ev.get("judge"),
                "judge_note": ev.get("judge_note"),
                "status": "completed" if done == len(rows) and rows else "running",
                "rows": rows,
                "totals": {
                    "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
                    "citations_verified": sum(r["citations_verified"] or 0 for r in rows),
                    "citations_unverified": sum(r["citations_unverified"] or 0 for r in rows),
                    "completed": sum(1 for r in rows if r["status"] == "completed"),
                    "failed": sum(1 for r in rows if r["status"] == "failed"),
                },
            }
        )
    yield _line({"type": "evals", "seq": 0, "data": {"items": items}})


# -------------------------------------------------------------------------------------------- dispatch --


async def dispatch(
    principal: Principal,
    body: InvokeRequest,
    session_id: str | None,
    start_background: Callable,
    publisher_factory: Callable,
    resolve_models: Callable,
) -> AsyncIterator[str]:
    op = body.op
    if op == Op.PACKAGES_LIST:
        gen = packages_list(principal, body)
    elif op == Op.PACKAGES_GET:
        gen = packages_get(principal, body)
    elif op == Op.PACKAGES_UPDATE:
        gen = packages_update(principal, body)
    elif op == Op.PACKAGES_DELETE:
        gen = packages_delete(principal, body)
    elif op == Op.PACKAGES_COMPARE:
        gen = packages_compare(principal, body)
    elif op == Op.PACKAGES_RERUN:
        gen = packages_rerun(principal, body, session_id, start_background, publisher_factory, resolve_models)
    elif op == Op.EXPORT:
        gen = export(principal, body)
    elif op == Op.ARTIFACT_URL:
        gen = artifact_url(principal, body)
    elif op == Op.MODELS:
        gen = models(principal, body)
    elif op == Op.MODELS_PREFS:
        gen = models_prefs(principal, body)
    elif op == Op.MODELS_VALIDATE:
        gen = models_validate(principal, body)
    elif op == Op.EVAL:
        gen = eval_start(principal, body, session_id, start_background, publisher_factory, resolve_models)
    elif op == Op.EVAL_LIST:
        gen = eval_list(principal, body)
    else:
        yield _err(ErrorCode.INVALID_REQUEST, f"unsupported op {op.value}")
        return
    async for line in gen:
        yield line

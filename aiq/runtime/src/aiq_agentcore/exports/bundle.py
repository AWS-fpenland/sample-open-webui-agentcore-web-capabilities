# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The input to every renderer: a Research Package's stored parts, and the loader that merges them.

ADR-21 makes the Research Package (``manifest.json`` + ``report.md`` + ``ledger.json`` + ``sources.json`` +
``artifacts/``) the results system of record. The ``export`` op reads those objects from S3 and hands them to this
module as a ``PackageBundle``; nothing here touches S3 or the filesystem, so the renderers are testable offline and the
same code serves the runtime, a backfill and the tests.

``load_bundle`` ports the prototype loader (research-tracks/phase3-export/proto/mdmodel.py::load_package):
the report's ``## Sources`` block (``[N] Title: URL``) is merged with the citation ledger (source_id, verified,
match_level) and the source rows (kind, tool, retrieved_at, snippet, ``cited_by``) into one numbered source model that
every renderer re-emits. Markers are the union of the three inputs, so a citation is never silently dropped; a marker
that resolves to no retrieved source is kept and rendered as unverified.
"""

from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass, field
from typing import Any

from .mdmodel import ArtifactRec, Package, SourceRec, make_source_id, parse_markdown, pop_title, split_sources_section

_MARKER_RE = re.compile(r"^\[(\d+)\]$")


def _as_dict(obj: Any) -> dict:
    """Accept dicts, JSON text/bytes and pydantic models (contracts.Source, ...)."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return json.loads(bytes(obj).decode("utf-8"))
    if isinstance(obj, str):
        return json.loads(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"cannot interpret {type(obj).__name__} as a JSON object")


@dataclass
class PackageBundle:
    """A Research Package as stored: manifest (schema aiq-agentcore/package/v1) + report + ledger + sources + artifacts.

    ``sources`` are Source rows (contracts.Source fields, optional ``cited_by`` markers); ``artifacts`` pair each
    artifact record (schema ``Artifact``) with its bytes — only image bytes are ever embedded, other artifacts are
    listed and shipped in the ZIP.
    """

    manifest: dict
    report_md: str
    ledger: dict | None = None
    sources: list[dict] = field(default_factory=list)
    artifacts: list[tuple[dict, bytes]] = field(default_factory=list)

    @property
    def package_id(self) -> str:
        return str(self.manifest.get("package_id") or self.manifest.get("job_id") or "job_unknown")

    @classmethod
    def from_parts(cls, manifest: Any, report_md: str | bytes, ledger: Any = None, sources: list[Any] | None = None,
                   artifacts: list[tuple[Any, bytes]] | None = None) -> PackageBundle:
        """Build a bundle from raw stored parts (S3 object bytes, JSON text, dicts or pydantic models).

        - ``sources`` defaults to ``manifest["sources"]``
        - ``ledger`` defaults to the manifest's ``citations`` block (same shape as ledger.json)
        """
        man = _as_dict(manifest)
        report = report_md.decode("utf-8") if isinstance(report_md, (bytes, bytearray)) else str(report_md)
        led = _as_dict(ledger) if ledger is not None else None
        if led is None and isinstance(man.get("citations"), dict):
            cit = man["citations"]
            led = {"citations": list(cit.get("items") or []), "verified": cit.get("verified", 0),
                   "unverified": cit.get("unverified", 0), "sources_retrieved": len(man.get("sources") or [])}
        rows = [_as_dict(s) for s in (sources if sources is not None else (man.get("sources") or []))]
        arts = [(_as_dict(rec), bytes(data)) for rec, data in (artifacts or [])]
        return cls(manifest=man, report_md=report, ledger=led, sources=rows, artifacts=arts)


def _artifact_rec(rec: dict, data: bytes) -> ArtifactRec | None:
    if rec.get("status") in ("rejected", "deleted"):
        return None
    storage_key = rec.get("storage_key")
    filename = rec.get("filename") or (storage_key.rsplit("/", 1)[-1] if storage_key else "") or "artifact"
    artifact_id = rec.get("artifact_id") or "art_" + make_source_id(filename)[4:]
    mime = rec.get("mime_type") or rec.get("mime") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return ArtifactRec(artifact_id=str(artifact_id), filename=str(filename), mime=str(mime), data=data,
                       caption=rec.get("caption") or rec.get("title") or "", title=rec.get("title"),
                       storage_key=storage_key)


def load_bundle(bundle: PackageBundle) -> Package:
    """Parse the report and merge Sources block + ledger + source rows into the renderer model."""
    manifest = bundle.manifest or {}
    job_id = bundle.package_id
    ledger = bundle.ledger or {}
    timing = manifest.get("timing") or {}
    created_at = str(timing.get("created_at") or "")
    completed_at = str(timing.get("completed_at") or "")

    body_md, entries = split_sources_section(bundle.report_md)
    blocks = parse_markdown(body_md)
    title = pop_title(blocks) or str(manifest.get("title") or "") or "Research report"

    # 1. every retrieved source, from the rows
    retrieved: dict[str, SourceRec] = {}
    marker_sid: dict[int, str] = {}
    for row in bundle.sources:
        sid = str(row.get("source_id") or make_source_id(row.get("url") or row.get("document_key") or ""))
        retrieved[sid] = SourceRec(source_id=sid, url=row.get("url"), title=row.get("title"),
                                   kind=row.get("kind") or "web_search",
                                   retrieved_at=str(row.get("retrieved_at") or created_at),
                                   tool=row.get("tool") or "agentcore_web_search", snippet=row.get("snippet"),
                                   document_key=row.get("document_key"))
        for marker in row.get("cited_by") or []:
            m = _MARKER_RE.match(str(marker))
            if m:
                marker_sid.setdefault(int(m.group(1)), sid)
    by_url = {r.url: r for r in retrieved.values() if r.url}

    # 2. the ledger (deterministic verification), keyed by marker number
    by_marker: dict[int, dict] = {}
    for c in ledger.get("citations") or []:
        m = _MARKER_RE.match(str(c.get("marker") or ""))
        if m:
            by_marker.setdefault(int(m.group(1)), c)

    # 3. cited sources = union of the report's Sources block, the ledger and the rows' cited_by markers
    block_by_n = {n: (ttl, url) for n, ttl, url in entries}
    cited: list[SourceRec] = []
    for n in sorted(set(block_by_n) | set(by_marker) | set(marker_sid)):
        ttl, block_url = block_by_n.get(n, ("", None))
        c = by_marker.get(n, {})
        url = c.get("url") or block_url
        base_by_url = by_url.get(url) if url else None
        sid = (c.get("source_id") or marker_sid.get(n) or (base_by_url.source_id if base_by_url else None)
               or make_source_id(url or f"{job_id}#{n}"))
        base = retrieved.get(sid) or base_by_url
        rec = SourceRec(source_id=sid, url=url or (base.url if base else None),
                        title=ttl or (base.title if base else None), kind=base.kind if base else "web_search",
                        retrieved_at=(base.retrieved_at if base else created_at),
                        tool=base.tool if base else "agentcore_web_search", snippet=base.snippet if base else None,
                        document_key=base.document_key if base else None, n=n, verified=c.get("verified"),
                        match_level=c.get("match_level"))
        cited.append(rec)
        retrieved[sid] = rec

    artifacts = [a for a in (_artifact_rec(rec, data) for rec, data in bundle.artifacts) if a is not None]
    return Package(job_id=job_id, title=title, created_at=created_at, body=blocks, sources=cited,
                   retrieved=list(retrieved.values()), ledger=ledger, raw_markdown=bundle.report_md,
                   manifest=manifest, question=str(manifest.get("question") or ""), completed_at=completed_at,
                   artifacts=artifacts)


def as_package(obj: PackageBundle | Package) -> Package:
    return obj if isinstance(obj, Package) else load_bundle(obj)

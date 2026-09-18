# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Export service: one entry point per format over a ``PackageBundle`` (ADR-26; runtime ``export`` op, §10).

``render(bundle, fmt, theme=..., pdf_backend=...)`` returns a ``RenderedExport`` (bytes + sha256 + content type +
timing + which backend produced it). ``render_all`` renders every format; ``zip`` bundles every other format with
``manifest.json`` (the package manifest whose ``exports`` list describes the files in the archive, schema
``ExportRecord``) and ``artifacts/<filename>``.

Filenames are ``<package_id>.<ext>`` — the slide deck is ``<package_id>-summary.pptx`` and a light HTML export is
``<package_id>-light.html`` — so downloads from the Workbench, chat attachments and the ZIP agree.

Heavy renderers (docx / pptx / reportlab) run in a worker thread so the runtime's event loop keeps serving
NDJSON streams; the renderer modules are imported lazily so bibliographic/JSON exports work even if an Office
library is missing from an image.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .bundle import PackageBundle, load_bundle
from .citations import to_bibtex, to_csl_json, to_ris
from .mdmodel import Package
from .render_pdf import PdfBackend, render_pdf

EXPORTS_VERSION = "0.1.0"
GENERATOR = f"aiq-agentcore-exports/{EXPORTS_VERSION}"
PACKAGE_SCHEMA = "aiq-agentcore/package/v1"

FORMATS = ("md", "html", "pdf", "docx", "pptx", "json", "csv", "bibtex", "ris", "csl", "zip")

CONTENT_TYPES = {
    "md": "text/markdown",
    "html": "text/html",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "json": "application/json",
    "csv": "text/csv",
    "bibtex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
    "csl": "application/vnd.citationstyles.csl+json",
    "zip": "application/zip",
}

EXTENSIONS = {"md": ".md", "html": ".html", "pdf": ".pdf", "docx": ".docx", "pptx": "-summary.pptx", "json": ".json",
              "csv": ".csv", "bibtex": ".bib", "ris": ".ris", "csl": ".csl.json", "zip": ".zip"}

# ExportRecord.format spelling in the package schema differs for CSL-JSON.
MANIFEST_FORMAT = {"csl": "csl-json"}

CSV_HEADER = ["source_id", "n", "title", "url", "kind", "tool", "retrieved_at", "verified", "match_level", "cited_by",
              "snippet"]

DERIVED_FORMATS = frozenset({"pptx"})


def export_filename(package_id: str, fmt: str, theme: str = "dark") -> str:
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; expected one of {FORMATS}")
    if fmt == "html" and theme == "light":
        return f"{package_id}-light.html"
    return f"{package_id}{EXTENSIONS[fmt]}"


def _now_iso() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass
class RenderedExport:
    format: str
    filename: str
    content_type: str
    data: bytes
    sha256: str
    size: int
    derived: bool
    backend: str | None
    ms: int
    theme: str | None = None
    meta: dict = field(default_factory=dict)

    @classmethod
    def build(cls, fmt: str, filename: str, data: bytes, *, ms: float, backend: str | None = None,
              theme: str | None = None, meta: dict | None = None) -> RenderedExport:
        return cls(format=fmt, filename=filename, content_type=CONTENT_TYPES[fmt], data=data,
                   sha256=hashlib.sha256(data).hexdigest(), size=len(data), derived=fmt in DERIVED_FORMATS,
                   backend=backend, ms=round(ms), theme=theme, meta=dict(meta or {}))

    def manifest_record(self, created_at: str | None = None, key: str | None = None) -> dict:
        """An ``ExportRecord`` (package schema) for this file; ``key`` defaults to the filename."""
        generator = GENERATOR + (f" ({self.backend})" if self.backend else "")
        return {"format": MANIFEST_FORMAT.get(self.format, self.format), "key": key or self.filename,
                "sha256": self.sha256, "size_bytes": self.size, "created_at": created_at or _now_iso(),
                "derived": self.derived, "generator": generator, "theme": self.theme}


# ----------------------------------------------------------------------------- per-format renderers


def markdown_export(bundle: PackageBundle, pkg: Package, *, front_matter: bool = True) -> str:
    """The report of record, preceded by a YAML front-matter excerpt of the manifest (ADR-26 Markdown row)."""
    if not front_matter:
        return bundle.report_md
    m = bundle.manifest or {}
    roles = (m.get("models") or {}).get("roles") or {}
    fm: dict[str, Any] = {
        "package_id": pkg.job_id,
        "title": pkg.title,
        "question": m.get("question") or None,
        "mode": m.get("mode"),
        "status": m.get("status"),
        "created_at": pkg.created_at or None,
        "completed_at": pkg.completed_at or None,
        "sources_cited": len(pkg.sources),
        "sources_retrieved": len(pkg.retrieved),
        "citations_verified": pkg.verified_count,
        "citations_unverified": sum(1 for s in pkg.sources if s.verified is False),
        "models": {role: r.get("model_id") for role, r in roles.items() if isinstance(r, dict)} or None,
        "schema": PACKAGE_SCHEMA,
        "exported_at": _now_iso(),
        "generator": GENERATOR,
    }
    # JSON scalars / flow collections are valid YAML, so json.dumps gives correct quoting without a YAML dependency.
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items() if v is not None] + ["---", ""]
    return "\n".join(lines) + bundle.report_md


def json_export(bundle: PackageBundle) -> bytes:
    doc = {"schema": PACKAGE_SCHEMA, "manifest": bundle.manifest, "report_md": bundle.report_md,
           "ledger": bundle.ledger, "sources": bundle.sources}
    return json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")


def csv_export(pkg: Package) -> bytes:
    """Sources table: cited sources first (by marker), then retrieved-but-uncited; ``cited_by`` lists every marker."""
    markers: dict[str, list[str]] = {}
    for s in pkg.sources:
        markers.setdefault(s.source_id, []).append(f"[{s.n}]")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    uncited = [s for s in pkg.retrieved if s.n is None]
    for s in [*pkg.sources, *uncited]:
        verified = "" if s.verified is None else ("true" if s.verified else "false")
        snippet = " ".join((s.snippet or "").split())[:500]
        w.writerow([s.source_id, s.n if s.n is not None else "", s.title or "", s.url or "", s.kind, s.tool,
                    s.retrieved_at, verified, s.match_level or "", ";".join(markers.get(s.source_id, [])), snippet])
    return buf.getvalue().encode("utf-8")


async def _render_one(bundle: PackageBundle, pkg: Package, fmt: str, *, theme: str,
                      pdf_backend: PdfBackend | None) -> RenderedExport:
    t0 = time.perf_counter()
    filename = export_filename(pkg.job_id, fmt, theme)
    backend: str | None = None
    meta: dict = {}
    out_theme: str | None = None
    if fmt == "md":
        data = markdown_export(bundle, pkg).encode("utf-8")
    elif fmt == "html":
        from .render_html import render_html

        data = render_html(pkg, theme=theme).encode("utf-8")
        backend, out_theme = "markdown-it-py", theme
    elif fmt == "pdf":
        data, meta = await render_pdf(pkg, theme=theme, pdf_backend=pdf_backend)
        backend, out_theme = meta.get("backend"), "print"
    elif fmt == "docx":
        from .render_docx import render_docx

        data, meta = await asyncio.to_thread(render_docx, pkg)
        backend = "python-docx"
    elif fmt == "pptx":
        from .render_pptx import render_pptx

        data, meta = await asyncio.to_thread(render_pptx, pkg)
        backend = "python-pptx"
    elif fmt == "json":
        data = json_export(bundle)
    elif fmt == "csv":
        data = csv_export(pkg)
    elif fmt == "bibtex":
        data = to_bibtex(pkg.sources, package_id=pkg.job_id).encode("utf-8")
    elif fmt == "ris":
        data = to_ris(pkg.sources, package_id=pkg.job_id).encode("utf-8")
    elif fmt == "csl":
        data = json.dumps(to_csl_json(pkg.sources, package_id=pkg.job_id), indent=1, ensure_ascii=False).encode("utf-8")
    else:
        raise ValueError(f"unknown export format {fmt!r}")
    return RenderedExport.build(fmt, filename, data, ms=(time.perf_counter() - t0) * 1000, backend=backend,
                                theme=out_theme, meta=meta)


def _zip(bundle: PackageBundle, pkg: Package, parts: list[RenderedExport], *, theme: str) -> RenderedExport:
    """ZIP = every non-zip export + artifacts/<filename> + manifest.json whose ``exports`` describe the archive."""
    t0 = time.perf_counter()
    created_at = _now_iso()
    manifest = dict(bundle.manifest or {})
    manifest["exports"] = [p.manifest_record(created_at) for p in parts]
    manifest_bytes = json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in parts:
            z.writestr(p.filename, p.data)
        for a in pkg.artifacts:
            z.writestr(f"artifacts/{a.filename}", a.data)
        z.writestr("manifest.json", manifest_bytes)
    entries = len(parts) + len(pkg.artifacts) + 1
    return RenderedExport.build("zip", export_filename(pkg.job_id, "zip", theme), buf.getvalue(),
                                ms=(time.perf_counter() - t0) * 1000, backend=None,
                                meta={"entries": entries, "formats": [p.format for p in parts]})


# ----------------------------------------------------------------------------- public API


async def render_all(bundle: PackageBundle, *, theme: str = "dark", pdf_backend: PdfBackend | None = None,
                     formats: tuple[str, ...] | list[str] | None = None) -> list[RenderedExport]:
    """Render ``formats`` (default: all) in FORMATS order. A requested ``zip`` always contains every other format."""
    wanted = tuple(formats) if formats is not None else FORMATS
    unknown = [f for f in wanted if f not in FORMATS]
    if unknown:
        raise ValueError(f"unknown export format(s) {unknown}; expected a subset of {FORMATS}")
    pkg = load_bundle(bundle)
    need = [f for f in FORMATS if f != "zip" and (f in wanted or "zip" in wanted)]
    parts = [await _render_one(bundle, pkg, f, theme=theme, pdf_backend=pdf_backend) for f in need]
    out = [p for p in parts if p.format in wanted]
    if "zip" in wanted:
        out.append(_zip(bundle, pkg, parts, theme=theme))
    return out


async def render(bundle: PackageBundle, fmt: str, *, theme: str = "dark",
                 pdf_backend: PdfBackend | None = None) -> RenderedExport:
    """Render one format. ``pdf_backend`` (async html -> pdf bytes) selects the Browser PDF path; None = reportlab."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; expected one of {FORMATS}")
    if fmt == "zip":
        return (await render_all(bundle, theme=theme, pdf_backend=pdf_backend, formats=("zip",)))[0]
    return await _render_one(bundle, load_bundle(bundle), fmt, theme=theme, pdf_backend=pdf_backend)

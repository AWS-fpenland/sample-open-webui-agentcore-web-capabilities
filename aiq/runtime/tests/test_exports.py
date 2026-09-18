# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for ``aiq_agentcore.exports`` against the real deep-job fixture (no AWS, no network).

The fixture (``evidence/fixtures/deep-job-*/``), the package schema and the CSL schema live in the phase-3 research
package that sits next to this worktree; ``AIQ_PHASE3_PACKAGE_DIR`` overrides the location. When they are absent
(a bare checkout of the sample repo) the module is skipped with a clear reason instead of failing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path

import pytest

from aiq_agentcore.exports import (
    FORMATS,
    PackageBundle,
    export_filename,
    load_bundle,
    print_html_to_pdf,
    render,
    render_all,
)
from aiq_agentcore.exports.mdmodel import Block, split_sources_section
from aiq_agentcore.exports.render_html import load_tokens_css
from aiq_agentcore.exports.render_pdf import render_pdf
from aiq_agentcore.exports.service import CSV_HEADER
from aiq_agentcore.exports.tokens import TOKENS_CSS

EXPECTED_CONTENT_TYPES = {
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
PNG_NAME = "fig-demo-costs.png"


def _research_package_dir() -> Path | None:
    cands: list[Path] = []
    if os.environ.get("AIQ_PHASE3_PACKAGE_DIR"):
        cands.append(Path(os.environ["AIQ_PHASE3_PACKAGE_DIR"]))
    try:
        cands.append(Path(__file__).resolve().parents[5])
    except IndexError:
        pass
    for c in cands:
        if ((c / "evidence" / "fixtures").is_dir()
                and (c / "research-tracks" / "phase3-architecture" / "package-schema.json").is_file()
                and (c / "research-tracks" / "phase3-export" / "csl-data.schema.json").is_file()):
            return c
    return None


RESEARCH = _research_package_dir()
pytestmark = pytest.mark.skipif(RESEARCH is None, reason="phase-3 research package (fixture + schemas) not available")


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def fixture_dir() -> Path:
    dirs = sorted((RESEARCH / "evidence" / "fixtures").glob("deep-job-job_*"))
    if not dirs:
        pytest.skip("no deep-job fixture directory")
    return dirs[0]


@pytest.fixture(scope="module")
def package_schema() -> dict:
    return json.loads((RESEARCH / "research-tracks" / "phase3-architecture" / "package-schema.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def csl_schema() -> dict:
    return json.loads((RESEARCH / "research-tracks" / "phase3-export" / "csl-data.schema.json").read_text("utf-8"))


def _png_bytes() -> bytes:
    """Synthetic bar chart (Pillow) so the image path is exercised; the fixture itself has no artifacts."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (900, 420), (255, 255, 255))
    d = ImageDraw.Draw(im)
    bars = [("S3 Vectors 250K", 11.38), ("OSS dev 250K", 175.2), ("OSS HA 250K", 350.4)]
    mx = max(v for _, v in bars)
    for k, (label, v) in enumerate(bars):
        x0 = 80 + k * 260
        h = int(300 * v / mx)
        d.rectangle([x0, 360 - h, x0 + 180, 360], fill=(20, 184, 166))
        d.text((x0, 370), f"{label}: ${v}/mo", fill=(0, 0, 0))
    d.text((80, 20), "SYNTHETIC demo artifact - monthly cost, USD", fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _manifest(job_id: str, tenant: str, report_md: str, ledger: dict, sources: list[dict], artifacts: list[dict]) -> dict:
    """A schema-complete manifest for the fixture (timing/usage values come from its events journal)."""
    created, completed = "2026-09-15T22:14:08.037Z", "2026-09-15T22:16:53.462Z"
    headings = [{"level": len(m.group(1)), "text": m.group(2).strip()[:300]}
                for m in re.finditer(r"^(#{1,6})\s+(.+)$", report_md, re.MULTILINE)]
    role = {"model_id": "nvidia.nemotron-super-3-120b", "lane": "bedrock_runtime", "region": "us-east-1"}
    prefix = f"tenants/{tenant}/packages/{job_id}/"
    return {
        "schema_version": "aiq-agentcore/package/v1",
        "package_id": job_id, "job_id": job_id, "tenant_key": tenant, "title": None,
        "question": "Compare Amazon S3 Vectors and Amazon OpenSearch Serverless for Bedrock Knowledge Bases",
        "clarification": [], "mode": "deep", "depth": "deep", "data_sources": ["web_search"],
        "status": "completed", "error": None,
        "timing": {"created_at": created, "started_at": created, "completed_at": completed, "updated_at": completed,
                   "duration_seconds": 161.2, "clarification_turns": 0},
        "runtime": {"adapter_version": "0.1.0", "source_commit": None, "upstream_ref": None, "runtime_version": "v17",
                    "runtime_session_id": None, "conversation_id": None, "region": "us-east-1"},
        "models": {"selection": "backfill_inferred",
                   "roles": {r: dict(role) for r in ("router", "clarifier", "shallow", "planner", "researcher", "writer")}},
        "usage": {"input_tokens": 700246, "output_tokens": 26514, "llm_calls": 58, "searches": 12, "pages": 0,
                  "retrievals": 0},
        "cost": {"currency": "USD", "total_usd": 0.0, "lines": [], "confidence": "estimated",
                 "price_source": {"kind": "estimate", "retrieved_at": completed}},
        "report": {"present": True, "key": prefix + "report.md", "sha256": hashlib.sha256(report_md.encode()).hexdigest(),
                   "size_bytes": len(report_md.encode()), "word_count": len(report_md.split()),
                   "format": "text/markdown", "headings": headings, "summary": None, "guardrail_action": None},
        "citations": {"ledger_key": prefix + "ledger.json", "verified": ledger["verified"],
                      "unverified": ledger["unverified"],
                      "items": [{"marker": c["marker"], "source_id": c["source_id"], "url": c["url"],
                                 "verified": c["verified"], "match_level": c["match_level"], "reason": None}
                                for c in ledger["citations"]]},
        "sources": sources, "artifacts": artifacts,
        "lineage": {"parent_package_id": None, "relation": "root", "root_package_id": job_id, "result_kind": None,
                    "changes": {"models_changed": [], "data_sources_changed": False, "question_changed": False}},
        "organization": {"tags": ["vector-stores"], "pinned": False, "project": None, "notes": None},
        "exports": [], "evals": [],
        "retention": {"expires_at": None, "s3_prefix": prefix, "deleted_at": None},
    }


@pytest.fixture(scope="module")
def parts(fixture_dir: Path) -> dict:
    report_md = (fixture_dir / "report.md").read_text(encoding="utf-8")
    ledger = json.loads((fixture_dir / "ledger.json").read_text(encoding="utf-8"))
    job_id = re.search(r"(job_[a-f0-9]{32})", fixture_dir.name).group(1)
    tenant = "u_" + hashlib.sha256(b"test-tenant").hexdigest()[:32]
    _, entries = split_sources_section(report_md)
    titles = {n: t for n, t, _ in entries}
    sources = []
    for c in ledger["citations"]:
        n = int(c["marker"].strip("[]"))
        sources.append({"source_id": c["source_id"], "url": c["url"], "title": titles.get(n), "kind": "web_search",
                        "retrieved_at": "2026-09-15T22:14:48Z", "tool": "agentcore_web_search", "snippet": None,
                        "document_key": None, "content_sha256": None, "cited_by": [c["marker"]]})
    png = _png_bytes()
    artifact = {
        "artifact_id": "art_" + hashlib.sha256(png).hexdigest()[:32], "job_id": job_id, "kind": "image",
        "mime_type": "image/png", "filename": PNG_NAME,
        "sandbox_path": f"/workspace/{job_id}/aiq-artifacts/{PNG_NAME}",
        "storage_key": f"tenants/{tenant}/packages/{job_id}/artifacts/{PNG_NAME}", "storage_uri": None,
        "sha256": hashlib.sha256(png).hexdigest(), "size_bytes": len(png), "title": "Monthly cost comparison",
        "caption": "Figure A1 - monthly cost comparison (synthetic demo artifact)", "inline": True, "workflow": "skill",
        "source_tool_call_id": None, "created_at": "2026-09-15T22:16:50.000Z", "capture_phase": "final",
        "status": "available", "referenced_in_report": False,
    }
    manifest = _manifest(job_id, tenant, report_md, ledger, sources, [artifact])
    return {"job_id": job_id, "report_md": report_md, "ledger": ledger, "sources": sources, "artifact": artifact,
            "png": png, "manifest": manifest}


@pytest.fixture(scope="module")
def bundle(parts: dict) -> PackageBundle:
    return PackageBundle.from_parts(parts["manifest"], parts["report_md"], parts["ledger"], parts["sources"],
                                    [(parts["artifact"], parts["png"])])


# ----------------------------------------------------------------------------- loader / model


def _walk(blocks: list[Block]):
    for b in blocks:
        yield b
        for item in b.items:
            yield from _walk(item)
        yield from _walk(b.children)


def test_loader_merges_sources_block_ledger_and_rows(bundle: PackageBundle, parts: dict):
    pkg = load_bundle(bundle)
    assert pkg.job_id == parts["job_id"]
    assert pkg.title.startswith("Amazon S3 Vectors vs. Amazon OpenSearch Serverless")
    assert len(pkg.sources) == 17 and [s.n for s in pkg.sources] == list(range(1, 18))
    assert all(s.verified for s in pkg.sources) and pkg.verified_count == 17
    s1 = pkg.source_by_n(1)
    assert s1.source_id == "src_2d70f209503bc808"  # from the ledger
    assert s1.title == "S3 Vectors vs OpenSearch: Decision Tree from 30+ Projects"  # from the Sources block
    assert s1.tool == "agentcore_web_search" and s1.retrieved_at == "2026-09-15T22:14:48Z"  # from the source rows
    assert not any(b.kind == "heading" and b.level == 1 for b in pkg.body)
    assert not any(b.kind == "heading" and "Sources" in "".join(i.text for i in b.inlines) for b in pkg.body)
    assert len(pkg.artifacts) == 1 and pkg.artifacts[0].is_image and pkg.trailing_artifacts() == pkg.artifacts


def test_adjacent_duplicate_citations_collapse(bundle: PackageBundle):
    pkg = load_bundle(bundle)
    # the fixture's "Cost optimization ... [1][1][4][3][6][8][6][16]" list item
    para = next(b for b in _walk(pkg.body) if b.kind == "paragraph"
                and any(i.kind == "text" and "Cost optimization" in i.text for i in b.inlines))
    assert [i.n for i in para.inlines if i.kind == "cite"] == [1, 4, 3, 6, 8, 16]


def test_from_parts_accepts_bytes_and_derives_ledger_from_manifest(parts: dict):
    b = PackageBundle.from_parts(json.dumps(parts["manifest"]).encode(), parts["report_md"].encode())
    assert b.ledger is not None and len(b.ledger["citations"]) == 17 and b.ledger["verified"] == 17
    assert len(b.sources) == 17 and b.package_id == parts["job_id"]
    assert len(load_bundle(b).sources) == 17


def test_tokens_css_packaged_file_matches_constant():
    assert load_tokens_css() == TOKENS_CSS and "--aiq-teal" in TOKENS_CSS


# ----------------------------------------------------------------------------- formats


async def test_markdown_contains_report(bundle: PackageBundle, parts: dict):
    r = await render(bundle, "md")
    text = r.data.decode("utf-8")
    assert r.filename == f"{parts['job_id']}.md" and r.content_type == "text/markdown"
    assert text.startswith("---\n") and f'package_id: "{parts["job_id"]}"' in text
    assert parts["report_md"] in text and text.endswith(parts["report_md"])
    assert r.sha256 == hashlib.sha256(r.data).hexdigest() and r.size == len(r.data) and not r.derived


@pytest.mark.parametrize("theme", ["dark", "light"])
async def test_html_themes_cites_sources_tokens_print(bundle: PackageBundle, parts: dict, theme: str):
    r = await render(bundle, "html", theme=theme)
    html = r.data.decode("utf-8")
    expected_name = f"{parts['job_id']}-light.html" if theme == "light" else f"{parts['job_id']}.html"
    assert r.filename == expected_name and r.content_type == "text/html" and r.theme == theme
    assert f'<html lang="en" data-theme="{theme}">' in html
    assert html.count('<sup class="cite">') > 0
    assert len(re.findall(r'<li id="src-\d+"', html)) == len(bundle.sources) == 17
    assert "unresolved citation" not in html
    assert "@media print" in html and "size:A4" in html
    assert "--aiq-teal" in html and "--bg:" in html and '[data-theme="light"]' in html  # design tokens embedded
    assert 'src="data:image/png;base64,' in html  # artifact embedded, self-contained
    assert "<script" not in html


async def test_pdf_reportlab_fallback_without_browser(bundle: PackageBundle, parts: dict):
    from pypdf import PdfReader

    r = await render(bundle, "pdf")
    assert r.backend == "reportlab" and r.theme == "print" and r.data.startswith(b"%PDF")
    assert r.filename == f"{parts['job_id']}.pdf" and r.content_type == "application/pdf"
    rd = PdfReader(io.BytesIO(r.data))
    text = "\n".join((pg.extract_text() or "") for pg in rd.pages)
    assert len(rd.pages) >= 2 and "Sources" in text and "[1]" in text
    uri = sum(1 for pg in rd.pages for a in (pg.get("/Annots") or [])
              if (a.get_object().get("/A") or {}).get("/URI"))
    assert uri > 0  # clickable Sources URLs preserved


async def test_render_pdf_uses_injected_backend(bundle: PackageBundle):
    seen: list[str] = []

    async def fake_backend(html: str) -> bytes:
        seen.append(html)
        return b"%PDF-fake"

    data, meta = await render_pdf(bundle, theme="light", pdf_backend=fake_backend)
    assert data == b"%PDF-fake" and meta["backend"] == "browser" and meta["theme"] == "print"
    assert len(seen) == 1 and 'data-theme="light"' in seen[0] and "@media print" in seen[0]
    r = await render(bundle, "pdf", pdf_backend=fake_backend)
    assert r.backend == "browser" and r.data == b"%PDF-fake" and r.theme == "print"


async def test_render_pdf_falls_back_when_backend_fails(bundle: PackageBundle):
    async def broken(_html: str) -> bytes:
        raise RuntimeError("browser session unavailable")

    data, meta = await render_pdf(bundle, pdf_backend=broken)
    assert data.startswith(b"%PDF") and meta["backend"] == "reportlab"
    assert meta["fallback_from"] == "browser" and "RuntimeError" in meta["error"]

    async def garbage(_html: str) -> bytes:
        return b"<html>not a pdf</html>"

    data, meta = await render_pdf(bundle, pdf_backend=garbage)
    assert data.startswith(b"%PDF") and meta["backend"] == "reportlab" and meta["fallback_from"] == "browser"


async def test_print_html_to_pdf_rejects_fragments_before_any_aws_call():
    with pytest.raises(ValueError):
        await print_html_to_pdf("<p>not a document</p>", region="us-east-1")


async def test_docx_reopens(bundle: PackageBundle, parts: dict):
    from docx import Document
    from docx.oxml.ns import qn

    r = await render(bundle, "docx")
    assert r.filename == f"{parts['job_id']}.docx" and r.content_type == EXPECTED_CONTENT_TYPES["docx"]
    doc = Document(io.BytesIO(r.data))
    styles = [p.style.name for p in doc.paragraphs]
    assert sum(1 for s in styles if s.startswith("Heading") or s == "Title") >= 3
    links = doc.element.body.findall(".//" + qn("w:hyperlink"))
    assert sum(1 for h in links if h.get(qn("w:anchor"))) > 0  # [N] -> bookmarked Sources entries
    assert sum(1 for h in links if h.get(qn("r:id"))) >= 17  # external URLs
    assert len(doc.element.body.findall(".//" + qn("w:bookmarkStart"))) == 17
    assert any(p.text.strip() == "Sources" and p.style.name.startswith("Heading") for p in doc.paragraphs)
    assert parts["job_id"] in doc.sections[0].footer.paragraphs[0].text
    assert len(doc.inline_shapes) >= 1  # the artifact picture


async def test_pptx_reopens_and_is_labelled_derived(bundle: PackageBundle, parts: dict):
    from pptx import Presentation

    r = await render(bundle, "pptx")
    assert r.filename == f"{parts['job_id']}-summary.pptx" and r.derived is True
    prs = Presentation(io.BytesIO(r.data))
    assert len(prs.slides) >= 5
    for slide in prs.slides:
        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
        assert any("Derived" in t and parts["job_id"] in t for t in texts), "every slide carries the derived footer"
    titles = [s.shapes.title.text for s in prs.slides if s.shapes.title is not None]
    assert any(t.startswith("Sources") for t in titles)
    assert sum(1 for s in prs.slides for sh in s.shapes if sh.shape_type == 13) >= 1  # picture slide
    assert "Derived" in prs.core_properties.subject


async def test_json_manifest_validates_against_schema(bundle: PackageBundle, parts: dict, package_schema: dict):
    import jsonschema

    r = await render(bundle, "json")
    doc = json.loads(r.data.decode("utf-8"))
    assert set(doc) == {"schema", "manifest", "report_md", "ledger", "sources"}
    assert doc["schema"] == "aiq-agentcore/package/v1"
    jsonschema.Draft202012Validator(package_schema).validate(doc["manifest"])
    assert doc["report_md"] == parts["report_md"] and len(doc["sources"]) == 17 and doc["ledger"]["verified"] == 17


async def test_csv_header_exact(bundle: PackageBundle):
    r = await render(bundle, "csv")
    rows = list(csv.reader(io.StringIO(r.data.decode("utf-8"))))
    assert rows[0] == ["source_id", "n", "title", "url", "kind", "tool", "retrieved_at", "verified", "match_level",
                       "cited_by", "snippet"]
    assert rows[0] == CSV_HEADER and len(rows) - 1 == 17
    first = dict(zip(rows[0], rows[1]))
    assert first["n"] == "1" and first["cited_by"] == "[1]" and first["verified"] == "true"
    assert first["match_level"] == "exact" and first["url"].startswith("https://")


async def test_bibtex_parses(bundle: PackageBundle):
    import bibtexparser

    r = await render(bundle, "bibtex")
    lib = bibtexparser.parse_string(r.data.decode("utf-8"))
    with_url = [s for s in bundle.sources if s.get("url")]
    assert len(lib.entries) == len(with_url) == 17 and not lib.failed_blocks
    keys = [e.key for e in lib.entries]
    assert len(set(keys)) == len(keys)
    assert all(e.entry_type == "online" and e.fields_dict["url"] and e.fields_dict["urldate"] for e in lib.entries)
    assert r.filename.endswith(".bib") and r.content_type == "application/x-bibtex"


async def test_ris_parses(bundle: PackageBundle):
    import rispy

    r = await render(bundle, "ris")
    entries = rispy.loads(r.data.decode("utf-8"))
    assert len(entries) == 17
    assert all(e["type_of_reference"] == "ELEC" and (e.get("url") or e.get("urls")) and e.get("access_date")
               for e in entries)


async def test_csl_json_validates(bundle: PackageBundle, csl_schema: dict):
    import jsonschema

    r = await render(bundle, "csl")
    items = json.loads(r.data.decode("utf-8"))
    errors = list(jsonschema.Draft7Validator(csl_schema).iter_errors(items))
    assert errors == [] and len(items) == 17
    assert all(i["type"] == "webpage" and i["URL"] and i["accessed"]["date-parts"] for i in items)
    assert r.filename.endswith(".csl.json") and r.content_type == "application/vnd.citationstyles.csl+json"


async def test_zip_listing_and_manifest_digests(bundle: PackageBundle, parts: dict, package_schema: dict):
    import jsonschema

    jid = parts["job_id"]
    r = await render(bundle, "zip")
    assert r.filename == f"{jid}.zip" and r.content_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        names = set(z.namelist())
        expected = {f"{jid}.md", f"{jid}.html", f"{jid}.pdf", f"{jid}.docx", f"{jid}-summary.pptx", f"{jid}.json",
                    f"{jid}.csv", f"{jid}.bib", f"{jid}.ris", f"{jid}.csl.json", "manifest.json", f"artifacts/{PNG_NAME}"}
        assert names == expected
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        jsonschema.Draft202012Validator(package_schema).validate(manifest)
        recs = manifest["exports"]
        assert {rec["format"] for rec in recs} == {"md", "html", "pdf", "docx", "pptx", "json", "csv", "bibtex", "ris",
                                                    "csl-json"}
        for rec in recs:
            blob = z.read(rec["key"])
            assert hashlib.sha256(blob).hexdigest() == rec["sha256"] and len(blob) == rec["size_bytes"]
        assert {rec["key"] for rec in recs} == expected - {"manifest.json", f"artifacts/{PNG_NAME}"}
        assert [rec["derived"] for rec in recs if rec["format"] == "pptx"] == [True]
        assert [rec["theme"] for rec in recs if rec["format"] == "pdf"] == ["print"]
        assert z.read(f"artifacts/{PNG_NAME}") == parts["png"]
        assert manifest["package_id"] == jid and manifest["artifacts"][0]["filename"] == PNG_NAME


async def test_render_all_formats_order_and_content_types(bundle: PackageBundle):
    outs = await render_all(bundle)
    assert [o.format for o in outs] == list(FORMATS)
    for o in outs:
        assert o.content_type == EXPECTED_CONTENT_TYPES[o.format]
        assert o.sha256 == hashlib.sha256(o.data).hexdigest() and o.size == len(o.data) > 0 and o.ms >= 0
        assert o.derived is (o.format == "pptx")
    assert outs[-1].meta["entries"] == 12


async def test_unknown_format_rejected(bundle: PackageBundle):
    with pytest.raises(ValueError):
        await render(bundle, "xlsx")
    with pytest.raises(ValueError):
        export_filename("job_x", "xlsx")
    assert export_filename("job_x", "pptx") == "job_x-summary.pptx"
    assert export_filename("job_x", "html", theme="light") == "job_x-light.html"

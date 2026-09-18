# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Derived summary deck via python-pptx (ADR-26): 16:9 title, key findings, one slide per H2 (≤6 bullets), table
slides, artifact slides for images, Sources slides with hyperlinks, full section text in speaker notes.

The deck ADDS content (a heuristic summary: first sentence per section, ≤6 bullets, ≤8 table rows, ≤4 images —
deterministic, no LLM; decision item 6, export-rendering.md §6), so it is labelled derived everywhere a reader can
look: the footer of EVERY slide ("Derived from research package <id> · summary deck, not the report of record"), the
subtitle, ``core_properties.subject`` and ``derived: true`` in the export record. Port of proto/render_pptx.py.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from .mdmodel import Block, Inline, Package, plain, sections

W, H = Inches(13.333), Inches(7.5)
MAX_BULLETS = 6
MUTED = RGBColor(0x47, 0x55, 0x69)
ACCENT = RGBColor(0x0F, 0x76, 0x6E)


def clip(s: str, n: int = 190) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def first_sentence(inlines: list[Inline]) -> str:
    txt = plain(inlines)
    m = re.match(r"(.+?[.!?])(\s|$)", txt)
    return (m.group(1) if m else txt).strip()


def _first_sentence_inlines(inlines: list[Inline]) -> list[Inline]:
    out: list[Inline] = []
    for idx, i in enumerate(inlines):
        if i.kind == "text":
            m = re.search(r"[.!?](\s|$)", i.text)
            if m:
                out.append(Inline("text", i.text[: m.end()].rstrip(), bold=i.bold, italic=i.italic))
                # keep immediately following citation markers
                j = idx + 1
                while j < len(inlines) and inlines[j].kind == "cite":
                    out.append(inlines[j])
                    j += 1
                return out
        out.append(i)
    return out


def bullets_for(blocks: list[Block]) -> list[list[Inline]]:
    """Prefer list items; else the first sentence of each paragraph (as inlines so citation runs keep hyperlinks)."""
    out: list[list[Inline]] = []
    for b in blocks:
        if b.kind == "list":
            for item in b.items:
                if item and item[0].kind == "paragraph":
                    out.append(item[0].inlines)
    if not out:
        for b in blocks:
            if b.kind == "paragraph":
                out.append(_first_sentence_inlines(b.inlines))
    return out[:MAX_BULLETS]


def _set_title(slide, text: str, size: int) -> None:
    slide.shapes.title.text = text
    runs = slide.shapes.title.text_frame.paragraphs[0].runs
    if runs:
        runs[0].font.size = Pt(size)


def add_footer(slide, text: str) -> None:
    tb = slide.shapes.add_textbox(Inches(0.4), H - Inches(0.45), W - Inches(0.8), Inches(0.35))
    p = tb.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = text
    r.font.size = Pt(10)
    r.font.color.rgb = MUTED


def add_runs(paragraph, inlines: list[Inline], pkg: Package, size: int = 18, max_chars: int = 190) -> None:
    used = 0
    for i in inlines:
        if used >= max_chars:
            break
        if i.kind in ("text", "code", "link"):
            t = re.sub(r"\s+", " ", i.text)
            if used + len(t) > max_chars:
                t = t[: max_chars - used - 1].rstrip() + "…"
            r = paragraph.add_run()
            r.text = t
            r.font.size = Pt(size)
            r.font.bold = i.bold or None
            r.font.italic = i.italic or None
            if i.kind == "link" and i.href:
                r.hyperlink.address = i.href
            used += len(t)
        elif i.kind == "cite":
            r = paragraph.add_run()
            r.text = f" [{i.n}]"
            r.font.size = Pt(max(size - 6, 9))
            r.font.color.rgb = ACCENT
            s = pkg.source_by_n(i.n)
            if s and s.url:
                r.hyperlink.address = s.url


def bullet_slide(prs, pkg: Package, title: str, bullets: list[list[Inline]], notes: str, footer: str):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    _set_title(slide, clip(title, 80), 30)
    body = slide.placeholders[1]
    body.left, body.top, body.width, body.height = Inches(0.6), Inches(1.5), W - Inches(1.2), H - Inches(2.3)
    tf = body.text_frame
    tf.word_wrap = True
    for k, inl in enumerate(bullets):
        p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
        add_runs(p, inl, pkg)
        p.space_after = Pt(8)
    if notes:
        slide.notes_slide.notes_text_frame.text = notes[:4000]
    add_footer(slide, footer)
    return slide


def table_slide(prs, pkg: Package, title: str, tbl: Block, footer: str, max_rows: int = 8, max_cols: int = 6) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    _set_title(slide, clip(title, 80), 28)
    hdr = tbl.header[0] if tbl.header else []
    rows = tbl.rows[:max_rows]
    ncols = min(max([len(hdr)] + [len(r) for r in rows]) or 1, max_cols)
    nrows = len(rows) + (1 if hdr else 0)
    shape = slide.shapes.add_table(nrows, ncols, Inches(0.6), Inches(1.6), W - Inches(1.2), Inches(0.45) * nrows)
    t = shape.table
    ri = 0
    if hdr:
        for ci in range(ncols):
            cell = t.cell(0, ci)
            cell.text = clip(plain(hdr[ci], cites=False), 60) if ci < len(hdr) else ""
            if cell.text:
                cell.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
                cell.text_frame.paragraphs[0].runs[0].font.bold = True
        ri = 1
    for r in rows:
        for ci in range(ncols):
            cell = t.cell(ri, ci)
            cell.text = clip(plain(r[ci], cites=False), 60) if ci < len(r) else ""
            if cell.text:
                cell.text_frame.paragraphs[0].runs[0].font.size = Pt(13)
        ri += 1
    if len(tbl.rows) > max_rows:
        tb = slide.shapes.add_textbox(Inches(0.6), Inches(1.6) + Inches(0.45) * nrows + Inches(0.1), Inches(8),
                                      Inches(0.4))
        tb.text_frame.text = f"… {len(tbl.rows) - max_rows} more rows in the report"
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
        tb.text_frame.paragraphs[0].runs[0].font.color.rgb = MUTED
    add_footer(slide, footer)


def image_slide(prs, title: str, data: bytes, caption: str, footer: str) -> bool:
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as im:
            iw, ih = im.size
    except Exception:  # noqa: BLE001 — Pillow absent or undecodable: fall back to a 4:3 box
        iw, ih = 4, 3
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    _set_title(slide, clip(title, 80), 28)
    max_w, max_h = W - Inches(1.2), H - Inches(2.6)
    scale = min(max_w / iw, max_h / ih)
    pw, ph = int(iw * scale), int(ih * scale)
    left = int((W - pw) / 2)
    try:
        slide.shapes.add_picture(io.BytesIO(data), left, Inches(1.5), width=pw, height=ph)
    except Exception:  # noqa: BLE001 — python-pptx could not read the image: drop the slide content, keep the deck
        return False
    if caption:
        tb = slide.shapes.add_textbox(Inches(0.6), Inches(1.5) + ph + Inches(0.1), W - Inches(1.2), Inches(0.5))
        tb.text_frame.text = caption
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
        tb.text_frame.paragraphs[0].runs[0].font.color.rgb = MUTED
    add_footer(slide, footer)
    return True


def render_pptx(pkg: Package) -> tuple[bytes, dict]:
    """Render the derived summary deck. Returns (pptx_bytes, meta with slide counts)."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    footer = f"Derived from research package {pkg.job_id} · summary deck, not the report of record"
    secs = sections(pkg.body)
    # 1. title
    s = prs.slides.add_slide(prs.slide_layouts[0])
    _set_title(s, clip(pkg.title, 110), 34)
    sub = s.placeholders[1]
    researched = pkg.created_at[:10] if pkg.created_at else "n/a"
    sub.text = (f"Derived summary of research package {pkg.job_id}\n"
                f"{len(pkg.sources)} sources ({pkg.verified_count} verified) · researched {researched}"
                f" · generated {datetime.now(UTC).strftime('%Y-%m-%d')} by AI-Q on Amazon Bedrock AgentCore")
    for p in sub.text_frame.paragraphs:
        for r in p.runs:
            r.font.size = Pt(16)
    add_footer(s, footer)
    # 2. key findings: first sentence of each H2 section
    findings = []
    for title, blocks in secs:
        for b in blocks:
            if b.kind == "paragraph":
                findings.append([Inline("text", f"{title}: ", bold=True)] + _first_sentence_inlines(b.inlines))
                break
        if len(findings) >= MAX_BULLETS:
            break
    if findings:
        bullet_slide(prs, pkg, "Key findings", findings,
                     "One finding per section; see the report for evidence and citations.", footer)
    # 3. one slide per H2 (+ table slides)
    n_tables = 0
    for title, blocks in secs:
        notes = "\n\n".join(plain(b.inlines) for b in blocks if b.kind == "paragraph")
        bl = bullets_for(blocks)
        if bl:
            bullet_slide(prs, pkg, title, bl, notes, footer)
        for b in blocks:
            if b.kind == "table" and n_tables < 4:
                table_slide(prs, pkg, f"{title} — table", b, footer)
                n_tables += 1
    # 4. images: artifacts not already inline, then figures in the body
    n_img = 0
    for a in pkg.trailing_artifacts():
        if a.is_image and n_img < 4 and image_slide(prs, a.caption or "Artifact", a.data, a.caption, footer):
            n_img += 1
    for b in pkg.body:
        if b.kind == "image" and n_img < 4:
            got = pkg.image(b.src)
            if got and image_slide(prs, b.alt or "Figure", got[0], b.alt, footer):
                n_img += 1
    # 5. sources, 8 per slide
    per = 8
    total = (len(pkg.sources) + per - 1) // per
    for k in range(0, len(pkg.sources), per):
        chunk = pkg.sources[k:k + per]
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        _set_title(slide, "Sources" + (f" ({k // per + 1}/{total})" if len(pkg.sources) > per else ""), 28)
        tb = slide.shapes.add_textbox(Inches(0.6), Inches(1.5), W - Inches(1.2), H - Inches(2.3))
        tf = tb.text_frame
        tf.word_wrap = True
        for j, src in enumerate(chunk):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            r = p.add_run()
            r.text = f"[{src.n}] {clip(src.display_title(), 90)}"
            r.font.size = Pt(13)
            if src.url:
                r2 = p.add_run()
                r2.text = f"  {clip(src.url, 70)}"
                r2.font.size = Pt(11)
                r2.font.color.rgb = ACCENT
                r2.hyperlink.address = src.url
            p.space_after = Pt(6)
        add_footer(slide, footer)
    prs.core_properties.title = f"{pkg.title} — derived summary"
    prs.core_properties.subject = f"Derived from research package {pkg.job_id}"
    prs.core_properties.author = "AI-Q on Amazon Bedrock AgentCore"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), {"backend": "python-pptx", "slides": len(prs.slides), "sections": len(secs),
                            "table_slides": n_tables, "image_slides": n_img, "derived": True}

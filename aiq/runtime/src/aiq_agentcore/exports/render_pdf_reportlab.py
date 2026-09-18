# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF export via reportlab — the always-available FALLBACK behind the AgentCore Browser print (ADR-26).

Pure python, runs inside the Runtime image with no system libraries (WeasyPrint/wkhtmltopdf were rejected for
that reason, Track C §3). Same block model and content as the HTML: citations are superscript internal links to
anchored Sources entries, Sources URLs are clickable, the footer carries the package id and page number.

Fonts: reportlab's built-in Helvetica covers Latin-1 only. When a DejaVu Sans TTF set is available
(``AIQ_EXPORT_FONT_DIR``, an explicit ``font_dir`` or the Debian path) it is registered once and used for full
Unicode coverage (no CJK — decision item 2 in export-rendering.md §6). Registration is process-global state, so it is
guarded by a lock and cached.
"""

from __future__ import annotations

import io
import os
import threading
from datetime import UTC, datetime
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)

from .mdmodel import Block, Inline, Package

ACCENT = "#0f766e"  # tokens.css --accent (light theme); print is always light
_FONT_FILES = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf",
               "DejaVuSansMono.ttf")
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF")
_FONT_LOCK = threading.Lock()
_FONTS: dict[str, tuple[str, str, str]] = {}


def register_fonts(font_dir: str | None = None) -> tuple[str, str, str]:
    """Return (body_font, mono_font, note). Registers the DejaVu family once per directory; Helvetica otherwise."""
    dirs = [d for d in (font_dir, os.environ.get("AIQ_EXPORT_FONT_DIR"), *_FONT_DIRS) if d]
    key = "|".join(dirs)
    with _FONT_LOCK:
        if key in _FONTS:
            return _FONTS[key]
        for d in dirs:
            paths = [os.path.join(d, n) for n in _FONT_FILES]
            if all(os.path.exists(p) for p in paths):
                reg, bold, ital, boldital, mono = paths
                pdfmetrics.registerFont(TTFont("AIQBody", reg))
                pdfmetrics.registerFont(TTFont("AIQBody-Bold", bold))
                pdfmetrics.registerFont(TTFont("AIQBody-Italic", ital))
                pdfmetrics.registerFont(TTFont("AIQBody-BoldItalic", boldital))
                pdfmetrics.registerFont(TTFont("AIQMono", mono))
                pdfmetrics.registerFontFamily("AIQBody", normal="AIQBody", bold="AIQBody-Bold", italic="AIQBody-Italic",
                                              boldItalic="AIQBody-BoldItalic")
                _FONTS[key] = ("AIQBody", "AIQMono", "ttf:" + os.path.basename(reg))
                return _FONTS[key]
        _FONTS[key] = ("Helvetica", "Courier", "builtin:Helvetica (Latin-1 only)")
        return _FONTS[key]


def _styles(body_font: str, mono_font: str) -> dict[str, ParagraphStyle]:
    ss = getSampleStyleSheet()
    ink = colors.HexColor("#0f172a")
    muted = colors.HexColor("#475569")
    return {
        "title": ParagraphStyle("T", parent=ss["Title"], fontName=body_font, fontSize=20, leading=25, alignment=TA_LEFT,
                                spaceAfter=4, textColor=ink),
        "meta": ParagraphStyle("M", parent=ss["Normal"], fontName=body_font, fontSize=8.5, leading=11, textColor=muted,
                               spaceAfter=10),
        "h2": ParagraphStyle("H2", parent=ss["Heading2"], fontName=body_font, fontSize=14, leading=18, spaceBefore=12,
                             spaceAfter=5, textColor=ink),
        "h3": ParagraphStyle("H3", parent=ss["Heading3"], fontName=body_font, fontSize=11.5, leading=15, spaceBefore=8,
                             spaceAfter=3),
        "body": ParagraphStyle("B", parent=ss["Normal"], fontName=body_font, fontSize=10, leading=14.5, spaceAfter=5),
        "cell": ParagraphStyle("C", parent=ss["Normal"], fontName=body_font, fontSize=8.8, leading=11.5),
        "code": ParagraphStyle("K", parent=ss["Code"], fontName=mono_font, fontSize=8, leading=10,
                               backColor=colors.HexColor("#f3f3f3"), borderPadding=(4, 4, 4, 4), leftIndent=4,
                               spaceAfter=8),
        "quote": ParagraphStyle("Q", parent=ss["Normal"], fontName=body_font, fontSize=10, leading=14, leftIndent=12,
                                textColor=colors.HexColor("#444444")),
        "src": ParagraphStyle("S", parent=ss["Normal"], fontName=body_font, fontSize=8.8, leading=12, spaceAfter=3),
        "cap": ParagraphStyle("Cap", parent=ss["Normal"], fontName=body_font, fontSize=8.5, leading=11, textColor=muted,
                              spaceAfter=8),
    }


def inlines_markup(inlines: list[Inline], mono: str) -> str:
    out = []
    for i in inlines:
        if i.kind == "text":
            t = xml_escape(i.text)
            if i.bold:
                t = f"<b>{t}</b>"
            if i.italic:
                t = f"<i>{t}</i>"
            if i.strike:
                t = f"<strike>{t}</strike>"
            out.append(t)
        elif i.kind == "code":
            out.append(f'<font face="{mono}" size="8.5">{xml_escape(i.text)}</font>')
        elif i.kind == "link":
            out.append(f'<a href="{xml_escape(i.href or "")}" color="{ACCENT}">{xml_escape(i.text)}</a>')
        elif i.kind == "cite":
            out.append(f'<super><a href="#src-{i.n}" color="{ACCENT}">[{i.n}]</a></super>')
        elif i.kind == "br":
            out.append("<br/>")
    return "".join(out)


def _image_flowable(data: bytes, max_w: float, max_h: float) -> Image | None:
    try:
        img = Image(io.BytesIO(data))
        iw, ih = img.imageWidth, img.imageHeight
    except Exception:  # noqa: BLE001 — undecodable image: skip it rather than fail the export
        return None
    scale = min(max_w / iw, max_h / ih, 1.0)
    img.drawWidth, img.drawHeight = iw * scale, ih * scale
    img.hAlign = "LEFT"
    return img


def _image_for(src: str | None, pkg: Package, max_w: float, max_h: float) -> Image | None:
    got = pkg.image(src)
    return _image_flowable(got[0], max_w, max_h) if got else None


def blocks_flowables(blocks: list[Block], pkg: Package, st: dict, mono: str, avail_w: float, depth: int = 0) -> list:
    fl: list = []
    for b in blocks:
        if b.kind == "heading":
            fl.append(Paragraph(inlines_markup(b.inlines, mono), st["h2"] if b.level <= 2 else st["h3"]))
        elif b.kind == "paragraph":
            fl.append(Paragraph(inlines_markup(b.inlines, mono), st["body"]))
        elif b.kind == "list":
            items = []
            for item in b.items:
                sub = blocks_flowables(item, pkg, st, mono, avail_w - 14, depth + 1) or [Spacer(0, 0)]
                items.append(ListItem(sub, leftIndent=14))
            fl.append(ListFlowable(items, bulletType="1" if b.ordered else "bullet", start=b.start if b.ordered else None,
                                   leftIndent=14, bulletFontName=st["body"].fontName, bulletFontSize=9, spaceAfter=4))
        elif b.kind == "table":
            hdr = b.header[0] if b.header else []
            ncols = max([len(hdr)] + [len(r) for r in b.rows]) or 1
            data = []
            if hdr:
                data.append([Paragraph(f"<b>{inlines_markup(c, mono)}</b>", st["cell"]) for c in hdr]
                            + [""] * (ncols - len(hdr)))
            for r in b.rows:
                data.append([Paragraph(inlines_markup(c, mono), st["cell"]) for c in r] + [""] * (ncols - len(r)))
            t = Table(data, colWidths=[avail_w / ncols] * ncols, repeatRows=1 if hdr else 0, hAlign="LEFT")
            style = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
                     ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                     ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
            if hdr:
                style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")))
            t.setStyle(TableStyle(style))
            fl.append(t)
            fl.append(Spacer(0, 6))
        elif b.kind == "code":
            wrapped = "\n".join(line[i:i + 100] for line in b.text.splitlines() or [""]
                                for i in range(0, max(len(line), 1), 100))
            fl.append(XPreformatted(xml_escape(wrapped), st["code"]))
        elif b.kind == "image":
            img = _image_for(b.src, pkg, avail_w, 120 * mm)
            if img is not None:
                fl.append(KeepTogether([img, Paragraph(xml_escape(b.alt), st["cap"])] if b.alt else [img]))
        elif b.kind == "hr":
            fl.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#999999"), spaceBefore=6,
                                 spaceAfter=6))
        elif b.kind == "blockquote":
            for f in blocks_flowables(b.children, pkg, st, mono, avail_w - 12, depth + 1):
                if isinstance(f, Paragraph):
                    f.style = st["quote"]
                fl.append(f)
    return fl


def render_pdf_reportlab(pkg: Package, font_dir: str | None = None) -> tuple[bytes, dict]:
    """Render the package to PDF bytes with reportlab. Returns (pdf_bytes, {"backend", "font", "pages"})."""
    body_font, mono, font_note = register_fonts(font_dir)
    st = _styles(body_font, mono)
    page_w, _page_h = A4
    margin = 16 * mm
    avail_w = page_w - 2 * margin
    footer_text = f"Research package {pkg.job_id} · AI-Q on Amazon Bedrock AgentCore"

    def on_page(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(body_font, 7.5)
        canvas.setFillColor(colors.HexColor("#475569"))
        canvas.drawString(margin, 10 * mm, footer_text)
        canvas.drawRightString(page_w - margin, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=pkg.title, author="AI-Q on Amazon Bedrock AgentCore",
                            subject=f"Research package {pkg.job_id}", creator="aiq-agentcore exports (reportlab)")
    story: list = [Paragraph(xml_escape(pkg.title), st["title"])]
    meta = f"Research package {pkg.job_id}"
    if pkg.created_at:
        meta += f" · researched {pkg.created_at[:10]}"
    meta += (f" · {len(pkg.sources)} sources ({pkg.verified_count} verified)"
             f" · rendered {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    story.append(Paragraph(xml_escape(meta), st["meta"]))
    story += blocks_flowables(pkg.body, pkg, st, mono, avail_w)
    trailing = pkg.trailing_artifacts()
    if trailing:
        story.append(Paragraph("Artifacts", st["h2"]))
        for a in trailing:
            if a.is_image:
                img = _image_flowable(a.data, avail_w, 110 * mm)
                if img is not None:
                    story.append(KeepTogether([img, Paragraph(xml_escape(a.caption), st["cap"])]))
            else:
                story.append(Paragraph(xml_escape(f"{a.caption or a.filename} ({a.mime}, {len(a.data):,} bytes)"),
                                       st["cap"]))
    story.append(Paragraph("Sources", st["h2"]))
    for s in pkg.sources:
        if s.url:
            link = f'<a href="{xml_escape(s.url)}" color="{ACCENT}">{xml_escape(s.url)}</a>'
        else:
            link = xml_escape(s.document_key or "")
        if s.verified:
            mark = " ✓" if body_font != "Helvetica" else " (verified)"
        else:
            mark = ""
        story.append(Paragraph(f'<a name="src-{s.n}"/>[{s.n}] {xml_escape(s.display_title())} — {link}{mark}', st["src"]))
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue(), {"backend": "reportlab", "font": font_note, "pages": doc.page}

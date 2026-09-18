# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Markdown → block model shared by every export renderer (ADR-26).

Port of the measured Track C prototype (research-tracks/phase3-export/proto/mdmodel.py). The parsing rules settled
there and kept here:

- headings h1..h6 (h1 = document title, removed from the body)
- paragraphs, ordered/unordered lists (nested), GFM pipe tables, fenced/indented code, images, blockquotes, hr
- inline: bold / italic / strike / code / links / hard+soft breaks
- ``[N]`` citation markers become ``cite`` inlines; duplicate markers in one contiguous run collapse
  (``[1][1][4][3][6][8][6][16]`` → ``[1][4][3][6][8][16]`` — the real fixture has these)
- the trailing ``## Sources`` section (``[N] Title: URL`` lines) is parsed into source records and removed from the
  body; renderers emit their own Sources list from the merged source model (Sources block + ledger + source rows,
  see ``bundle.load_bundle``)

Safety: markdown is turned into tokens and re-emitted by each renderer — raw HTML in a report is reduced to its text,
never passed through (threat-model row "Renderers", 11-architecture.md §11).

No filesystem access: images are resolved from inline ``data:`` URIs or from the bundle's artifact bytes
(``artifact://<id>``, ``artifacts/<filename>`` or a bare filename) via ``Package.image``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes, urlparse

from markdown_it import MarkdownIt

CITE_RE = re.compile(r"\[(\d{1,3})\]")
SOURCE_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]\s*(.*?)\s*:\s*(https?://\S+)\s*$")
SOURCES_HEADING_RE = re.compile(r"^#{1,3}\s+Sources\s*$", re.IGNORECASE | re.MULTILINE)
ARTIFACT_URI_RE = re.compile(r"^artifact://([A-Za-z0-9_.-]+)$")
_DATA_URI_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(;[^,]*)?,(.*)$", re.DOTALL)


@dataclass
class Inline:
    kind: str  # text | code | link | cite | img | br
    text: str = ""
    href: str | None = None
    n: int | None = None
    src: str | None = None
    alt: str | None = None
    bold: bool = False
    italic: bool = False
    strike: bool = False


@dataclass
class Block:
    kind: str  # heading | paragraph | list | table | code | image | hr | blockquote
    level: int = 0
    inlines: list[Inline] = field(default_factory=list)
    ordered: bool = False
    start: int = 1
    items: list[list[Block]] = field(default_factory=list)
    header: list[list[Inline]] = field(default_factory=list)
    rows: list[list[list[Inline]]] = field(default_factory=list)
    lang: str = ""
    text: str = ""
    src: str | None = None
    alt: str = ""
    children: list[Block] = field(default_factory=list)


@dataclass
class SourceRec:
    """Mirror of aiq_agentcore.contracts.Source + citation-ledger fields + the report's marker number."""

    source_id: str
    url: str | None = None
    title: str | None = None
    kind: str = "web_search"  # web_search | web_page | document | news | prediction_market | package
    retrieved_at: str = ""  # ISO-8601 UTC
    tool: str = "agentcore_web_search"
    snippet: str | None = None
    document_key: str | None = None
    n: int | None = None  # marker number in the report ([N]); None = retrieved but uncited
    verified: bool | None = None
    match_level: str | None = None

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "") if self.url else ""

    def display_title(self) -> str:
        return self.title or self.host or self.document_key or self.source_id


@dataclass
class ArtifactRec:
    """An artifact's bytes plus the manifest fields the renderers need (schema ``Artifact``)."""

    artifact_id: str
    filename: str
    mime: str
    data: bytes
    caption: str = ""
    title: str | None = None
    storage_key: str | None = None

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


@dataclass
class Package:
    """The parsed, merged view of one Research Package that every renderer consumes."""

    job_id: str
    title: str
    created_at: str
    body: list[Block]
    sources: list[SourceRec]  # cited, ordered by n
    retrieved: list[SourceRec]  # every retrieved source (cited + uncited)
    ledger: dict
    raw_markdown: str
    manifest: dict = field(default_factory=dict)
    question: str = ""
    completed_at: str = ""
    artifacts: list[ArtifactRec] = field(default_factory=list)

    def source_by_n(self, n: int | None) -> SourceRec | None:
        for s in self.sources:
            if s.n == n:
                return s
        return None

    @property
    def verified_count(self) -> int:
        return sum(1 for s in self.sources if s.verified)

    def artifact_for(self, src: str | None) -> ArtifactRec | None:
        """Resolve ``artifact://<id>``, ``artifacts/<filename>``, a storage key or a bare filename to an artifact."""
        if not src or src.startswith(("data:", "http://", "https://")):
            return None
        m = ARTIFACT_URI_RE.match(src)
        if m:
            return next((a for a in self.artifacts if a.artifact_id == m.group(1)), None)
        name = src.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        return next((a for a in self.artifacts if a.storage_key == src or a.filename == name), None)

    def image(self, src: str | None) -> tuple[bytes, str] | None:
        """Bytes + mime for an inline data URI or an artifact reference; None when it cannot be resolved offline."""
        if not src:
            return None
        if src.startswith("data:"):
            return data_uri_bytes(src)
        art = self.artifact_for(src)
        return (art.data, art.mime) if art else None

    def referenced_artifact_ids(self) -> set[str]:
        ids: set[str] = set()

        def walk(blocks: list[Block]) -> None:
            for b in blocks:
                if b.kind == "image":
                    art = self.artifact_for(b.src)
                    if art:
                        ids.add(art.artifact_id)
                for item in b.items:
                    walk(item)
                walk(b.children)

        walk(self.body)
        return ids

    def trailing_artifacts(self) -> list[ArtifactRec]:
        """Artifacts for the closing "Artifacts" section: everything not already placed inline by the report."""
        ref = self.referenced_artifact_ids()
        return [a for a in self.artifacts if a.artifact_id not in ref]


# ----------------------------------------------------------------------------- helpers


def data_uri_bytes(src: str) -> tuple[bytes, str] | None:
    m = _DATA_URI_RE.match(src or "")
    if not m:
        return None
    mime = m.group(1) or "application/octet-stream"
    params = m.group(2) or ""
    try:
        data = base64.b64decode(m.group(3)) if "base64" in params else unquote_to_bytes(m.group(3))
    except (ValueError, binascii.Error):
        return None
    return data, mime


def make_source_id(url_or_key: str) -> str:
    return "src_" + hashlib.sha256(url_or_key.encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- inline / block parsing


def plain(inlines: list[Inline], cites: bool = True) -> str:
    out = []
    for i in inlines:
        if i.kind == "cite":
            if cites:
                out.append(f"[{i.n}]")
        elif i.kind == "br":
            out.append(" ")
        elif i.kind != "img":
            out.append(i.text)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def dedupe_cites(inlines: list[Inline]) -> list[Inline]:
    out: list[Inline] = []
    run: set[int] = set()
    for i in inlines:
        if i.kind == "cite":
            if i.n in run:
                continue
            run.add(i.n)  # type: ignore[arg-type]
        elif not (i.kind == "text" and not i.text.strip()):
            run = set()
        out.append(i)
    return out


def parse_inlines(children) -> list[Inline]:
    out: list[Inline] = []
    bold = italic = strike = 0
    link: dict | None = None

    def emit_text(s: str) -> None:
        if link is not None:
            link["text"] += s
            return
        pos = 0
        for m in CITE_RE.finditer(s):
            if m.start() > pos:
                out.append(Inline("text", s[pos:m.start()], bold=bold > 0, italic=italic > 0, strike=strike > 0))
            out.append(Inline("cite", m.group(0), n=int(m.group(1))))
            pos = m.end()
        if pos < len(s):
            out.append(Inline("text", s[pos:], bold=bold > 0, italic=italic > 0, strike=strike > 0))

    for t in children or []:
        ty = t.type
        if ty == "text":
            emit_text(t.content)
        elif ty == "strong_open":
            bold += 1
        elif ty == "strong_close":
            bold -= 1
        elif ty == "em_open":
            italic += 1
        elif ty == "em_close":
            italic -= 1
        elif ty == "s_open":
            strike += 1
        elif ty == "s_close":
            strike -= 1
        elif ty == "code_inline":
            if link is not None:
                link["text"] += t.content
            else:
                out.append(Inline("code", t.content))
        elif ty == "link_open":
            link = {"href": t.attrs.get("href", ""), "text": ""}
        elif ty == "link_close":
            if link is not None:
                out.append(Inline("link", link["text"] or link["href"], href=link["href"], bold=bold > 0,
                                  italic=italic > 0))
                link = None
        elif ty == "image":
            out.append(Inline("img", src=t.attrs.get("src", ""), alt=t.content or str(t.attrs.get("alt", ""))))
        elif ty == "softbreak":
            emit_text(" ")
        elif ty == "hardbreak":
            out.append(Inline("br"))
        elif ty == "html_inline":
            emit_text(re.sub(r"<[^>]+>", "", t.content))
    return dedupe_cites(out)


def parse_blocks(tokens, i: int = 0, stop: str | None = None) -> tuple[list[Block], int]:
    blocks: list[Block] = []
    while i < len(tokens):
        t = tokens[i]
        if stop and t.type == stop:
            return blocks, i + 1
        ty = t.type
        if ty == "heading_open":
            blocks.append(Block("heading", level=int(t.tag[1]), inlines=parse_inlines(tokens[i + 1].children)))
            i += 3
        elif ty == "paragraph_open":
            inl = parse_inlines(tokens[i + 1].children)
            i += 3
            imgs = [x for x in inl if x.kind == "img"]
            rest = [x for x in inl if x.kind != "img" and not (x.kind == "text" and not x.text.strip())]
            if imgs and not rest:
                for im in imgs:
                    blocks.append(Block("image", src=im.src, alt=im.alt or ""))
            else:
                blocks.append(Block("paragraph", inlines=inl))
        elif ty in ("bullet_list_open", "ordered_list_open"):
            b = Block("list", ordered=(ty == "ordered_list_open"), start=int(t.attrs.get("start", 1) or 1))
            close = "ordered_list_close" if b.ordered else "bullet_list_close"
            i += 1
            while i < len(tokens) and tokens[i].type == "list_item_open":
                children, i = parse_blocks(tokens, i + 1, "list_item_close")
                b.items.append(children)
            if i < len(tokens) and tokens[i].type == close:
                i += 1
            blocks.append(b)
        elif ty == "table_open":
            b = Block("table")
            i += 1
            in_head = False
            row: list[list[Inline]] = []
            while i < len(tokens) and tokens[i].type != "table_close":
                tt = tokens[i]
                if tt.type == "thead_open":
                    in_head = True
                elif tt.type == "thead_close":
                    in_head = False
                elif tt.type == "tr_open":
                    row = []
                elif tt.type in ("th_open", "td_open"):
                    row.append(parse_inlines(tokens[i + 1].children))
                    i += 2
                elif tt.type == "tr_close":
                    (b.header if in_head else b.rows).append(row)
                i += 1
            i += 1
            blocks.append(b)
        elif ty in ("fence", "code_block"):
            blocks.append(Block("code", lang=(t.info or "").strip(), text=t.content.rstrip("\n")))
            i += 1
        elif ty == "hr":
            blocks.append(Block("hr"))
            i += 1
        elif ty == "blockquote_open":
            children, i = parse_blocks(tokens, i + 1, "blockquote_close")
            blocks.append(Block("blockquote", children=children))
        elif ty == "html_block":
            txt = re.sub(r"<[^>]+>", "", t.content).strip()
            if txt:
                blocks.append(Block("paragraph", inlines=[Inline("text", txt)]))
            i += 1
        else:
            i += 1
    return blocks, i


def parse_markdown(md_text: str) -> list[Block]:
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    blocks, _ = parse_blocks(md.parse(md_text))
    return blocks


def split_sources_section(md_text: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Return (markdown without the Sources section, [(n, title, url), ...])."""
    m = SOURCES_HEADING_RE.search(md_text)
    if not m:
        return md_text, []
    after = md_text[m.end():]
    nxt = re.search(r"^#{1,2}\s+\S", after, re.MULTILINE)
    section = after[: nxt.start()] if nxt else after
    entries = []
    for line in section.splitlines():
        mm = SOURCE_LINE_RE.match(line)
        if mm:
            entries.append((int(mm.group(1)), mm.group(2).strip(), mm.group(3).strip()))
    body = md_text[: m.start()] + (after[nxt.start():] if nxt else "")
    return body.rstrip() + "\n", entries


def pop_title(blocks: list[Block]) -> str:
    """Remove the first H1 from the body and return its text ('' when the report has no H1)."""
    for b in blocks:
        if b.kind == "heading" and b.level == 1:
            blocks.remove(b)
            return plain(b.inlines, cites=False)
    return ""


def sections(body: list[Block]) -> list[tuple[str, list[Block]]]:
    """Group body blocks by H2. Blocks before the first H2 become 'Overview'."""
    out: list[tuple[str, list[Block]]] = []
    cur_title: str | None = None
    cur: list[Block] = []
    for b in body:
        if b.kind == "heading" and b.level == 2:
            if cur or cur_title is not None:
                out.append((cur_title or "Overview", cur))
            cur_title, cur = plain(b.inlines, cites=False), []
        else:
            cur.append(b)
    if cur or cur_title is not None:
        out.append((cur_title or "Overview", cur))
    return out


def heading_id(inlines: list[Inline]) -> str:
    return "h-" + "".join(ch if ch.isalnum() else "-" for ch in plain(inlines, cites=False).lower())[:60].strip("-")

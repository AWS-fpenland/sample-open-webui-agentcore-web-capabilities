# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501  (embedded CSS / template strings)
"""Styled HTML export — the canonical styled rendering (ADR-26; Track C §3).

Decisions implemented here:
- the Workbench design tokens (``design/tokens.css``, embedded verbatim) style the report so an export looks like the
  product that produced it; ``theme="dark"`` (default) or ``"light"`` is forced with ``data-theme`` on ``<html>``
- ``@media print`` switches to light colours and an A4 ``@page`` — this is the HTML the AgentCore Browser prints to
  PDF, so print fidelity is decided in this file
- ``[N]`` citations render as ``<sup class="cite"><a href="#src-N">``; the Sources list is ``<ol class="sources">``
  with ``id="src-N"`` items, clickable URLs and a verified badge
- the file is self-contained: images become data URIs (``page.set_content`` has no base URL and the browser session
  blocks the network); all text is escaped, no raw HTML from the report survives (mdmodel)
"""

from __future__ import annotations

import base64
import html
from datetime import UTC, datetime
from importlib import resources

from .mdmodel import Block, Inline, Package, heading_id, plain
from .tokens import TOKENS_CSS

THEMES = ("dark", "light")

# Report layer on top of the token set: maps the prototype's variables onto the Workbench tokens, adds print rules.
REPORT_CSS = """
:root{--surface:var(--bg-2);--muted:var(--fg-muted);--border:var(--glass-border);--code-bg:var(--glass);
 --font:var(--font-sans);--mono:var(--font-mono);--radius:var(--r-sm);--measure:78ch;color-scheme:dark}
:root[data-theme=light]{color-scheme:light}
@media print{:root,:root[data-theme]{--bg:#fff;--bg-2:#fff;--fg:#000;--fg-muted:#444;--fg-faint:#555;--accent:#0f766e;
  --accent-2:#6d28d9;--accent-3:#b45309;--ok:#15803d;--glass:#f3f3f3;--glass-strong:#eee;--glass-border:#999;
  --shadow:none;--surface:#fff;--muted:#444;--border:#999;--code-bg:#f3f3f3;color-scheme:light}
 html,body{background:#fff;background-image:none;color:#000}
 body{margin:0;max-width:none;font-size:10.5pt}nav,.no-print{display:none}h2{break-after:avoid}
 table,figure,pre,li{break-inside:avoid}a{text-decoration:none}ol.sources a[href]::after{content:""}
 footer.doc{margin-top:1.2rem;break-before:avoid;break-inside:avoid}
 @page{size:A4;margin:18mm 16mm}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:var(--font);line-height:1.55;max-width:var(--measure);
 margin:0 auto;padding:2rem 1.25rem}
header.doc h1{font-size:1.9rem;line-height:1.2;margin:.2rem 0 .5rem}
header.doc .gradient-bar{margin:0 0 .6rem}
.meta{color:var(--muted);font-size:.85rem}h2{font-size:1.35rem;margin:1.8rem 0 .5rem}h3{font-size:1.1rem;margin:1.3rem 0 .4rem}
p{margin:.6rem 0}a{color:var(--accent)}
table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.95rem}
th,td{border:1px solid var(--border);padding:.4rem .6rem;text-align:left;vertical-align:top}
th{background:var(--surface)}tr:nth-child(even) td{background:var(--glass)}
sup.cite{line-height:0}sup.cite a{font-weight:600;text-decoration:none;padding:0 .1em}sup.cite a:hover{text-decoration:underline}
code{font-family:var(--mono);font-size:.9em;background:var(--code-bg);padding:.1rem .3rem;border-radius:4px}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);padding:.8rem 1rem;overflow:auto}
pre code{background:none;padding:0}
figure{margin:1.2rem 0}figure img{max-width:100%;height:auto;border:1px solid var(--border);border-radius:var(--radius);background:#fff}  # noqa: E501
figcaption{color:var(--muted);font-size:.85rem;margin-top:.3rem}
blockquote{border-left:3px solid var(--accent);margin:1rem 0;padding:.2rem 1rem;color:var(--muted)}
ol.sources{padding-left:2.2rem}ol.sources li{margin:.35rem 0;word-break:break-word}
ol.sources li:target{background:var(--surface);outline:2px solid var(--accent)}
.badge{display:inline-block;font-size:.7rem;padding:0 .4rem;border-radius:var(--r-pill);border:1px solid var(--border);
 color:var(--muted);vertical-align:middle;margin-left:.3rem}
.badge.ok{color:var(--ok);border-color:var(--ok)}
footer.doc{margin-top:2.5rem;border-top:1px solid var(--border);padding-top:.8rem;color:var(--muted);font-size:.8rem}
hr{border:0;border-top:1px solid var(--border);margin:1.5rem 0}.toc{font-size:.9rem;color:var(--muted)}.toc a{margin-right:.8rem}
ul.artifacts{padding-left:1.2rem}
@media (max-width:600px){body{padding:1rem .8rem}header.doc h1{font-size:1.5rem}table{font-size:.85rem;display:block;overflow-x:auto}}  # noqa: E501
"""


def load_tokens_css() -> str:
    """The packaged ``tokens.css`` (importlib.resources); falls back to the generated constant."""
    try:
        return (resources.files(__package__) / "tokens.css").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return TOKENS_CSS


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def image_src(src: str | None, pkg: Package) -> str:
    """Self-contained image reference: data URIs pass through, artifacts are embedded, external URLs are kept as-is."""
    if not src:
        return ""
    if src.startswith("data:"):
        return src
    got = pkg.image(src)
    if got:
        data, mime = got
        return f"data:{mime};base64," + base64.b64encode(data).decode()
    return src


def inlines_html(inlines: list[Inline], pkg: Package) -> str:
    out = []
    for i in inlines:
        if i.kind == "text":
            t = esc(i.text)
            if i.bold:
                t = f"<strong>{t}</strong>"
            if i.italic:
                t = f"<em>{t}</em>"
            if i.strike:
                t = f"<del>{t}</del>"
            out.append(t)
        elif i.kind == "code":
            out.append(f"<code>{esc(i.text)}</code>")
        elif i.kind == "link":
            out.append(f'<a href="{esc(i.href)}" rel="noopener">{esc(i.text)}</a>')
        elif i.kind == "cite":
            s = pkg.source_by_n(i.n)
            title = esc(s.display_title()) if s else "unresolved citation"
            out.append(f'<sup class="cite"><a href="#src-{i.n}" title="{title}">[{i.n}]</a></sup>')
        elif i.kind == "br":
            out.append("<br>")
        elif i.kind == "img":
            out.append(f'<img src="{esc(image_src(i.src, pkg))}" alt="{esc(i.alt)}">')
    return "".join(out)


def blocks_html(blocks: list[Block], pkg: Package) -> str:
    out = []
    for b in blocks:
        if b.kind == "heading":
            lvl = min(max(b.level, 2), 6)
            out.append(f'<h{lvl} id="{heading_id(b.inlines)}">{inlines_html(b.inlines, pkg)}</h{lvl}>')
        elif b.kind == "paragraph":
            out.append(f"<p>{inlines_html(b.inlines, pkg)}</p>")
        elif b.kind == "list":
            tag = "ol" if b.ordered else "ul"
            start = f' start="{b.start}"' if b.ordered and b.start != 1 else ""
            items = []
            for item in b.items:
                # tight list items are single hidden paragraphs; render children inline when it is one paragraph
                if len(item) == 1 and item[0].kind == "paragraph":
                    items.append(f"<li>{inlines_html(item[0].inlines, pkg)}</li>")
                else:
                    items.append(f"<li>{blocks_html(item, pkg)}</li>")
            out.append(f"<{tag}{start}>{''.join(items)}</{tag}>")
        elif b.kind == "table":
            head = "".join(f'<th scope="col">{inlines_html(c, pkg)}</th>' for c in (b.header[0] if b.header else []))
            rows = "".join("<tr>" + "".join(f"<td>{inlines_html(c, pkg)}</td>" for c in r) + "</tr>" for r in b.rows)
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        elif b.kind == "code":
            cls = f' class="language-{esc(b.lang)}"' if b.lang else ""
            out.append(f"<pre><code{cls}>{esc(b.text)}</code></pre>")
        elif b.kind == "image":
            cap = f"<figcaption>{esc(b.alt)}</figcaption>" if b.alt else ""
            out.append(f'<figure><img src="{esc(image_src(b.src, pkg))}" alt="{esc(b.alt)}" loading="lazy">{cap}</figure>')
        elif b.kind == "hr":
            out.append("<hr>")
        elif b.kind == "blockquote":
            out.append(f"<blockquote>{blocks_html(b.children, pkg)}</blockquote>")
    return "\n".join(out)


def sources_html(pkg: Package) -> str:
    items = []
    for s in pkg.sources:
        if s.verified:
            badge = '<span class="badge ok">verified</span>'
        elif s.verified is False:
            badge = '<span class="badge">unverified</span>'
        else:
            badge = ""
        link = f'<a href="{esc(s.url)}" rel="noopener">{esc(s.url)}</a>' if s.url else esc(s.document_key or "")
        items.append(f'<li id="src-{s.n}" value="{s.n}">{esc(s.display_title())} &mdash; {link}{badge}</li>')
    return f'<section id="sources"><h2>Sources</h2><ol class="sources">{"".join(items)}</ol></section>'


def artifacts_html(pkg: Package) -> str:
    arts = pkg.trailing_artifacts()
    if not arts:
        return ""
    figs = []
    others = []
    for a in arts:
        if a.is_image:
            data_uri = "data:" + a.mime + ";base64," + base64.b64encode(a.data).decode()
            figs.append(f'<figure><img src="{data_uri}" alt="{esc(a.caption)}"><figcaption>{esc(a.caption)}</figcaption></figure>')  # noqa: E501
        else:
            label = esc(a.caption or a.filename)
            others.append(f'<li><a href="artifacts/{esc(a.filename)}">{label}</a> <span class="meta">({esc(a.mime)}, '
                          f"{len(a.data):,} bytes)</span></li>")
    body = "".join(figs) + (f'<ul class="artifacts">{"".join(others)}</ul>' if others else "")
    return f'<section id="artifacts"><h2>Artifacts</h2>{body}</section>'


def render_html(pkg: Package, theme: str = "dark") -> str:
    """Complete, self-contained HTML document for the package. ``theme`` is 'dark' or 'light'."""
    if theme not in THEMES:
        raise ValueError(f"theme must be one of {THEMES}, got {theme!r}")
    rendered = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    toc = " ".join(f'<a href="#{heading_id(b.inlines)}">{esc(plain(b.inlines, cites=False))}</a>'
                   for b in pkg.body if b.kind == "heading" and b.level == 2)
    meta = f"Research package <code>{esc(pkg.job_id)}</code>"
    if pkg.created_at:
        meta += f" · researched {esc(pkg.created_at[:10])}"
    meta += f" · {len(pkg.sources)} sources ({pkg.verified_count} verified)"
    css = load_tokens_css() + REPORT_CSS
    return f"""<!doctype html>
<html lang="en" data-theme="{theme}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="{theme}">
<title>{esc(pkg.title)}</title><meta name="generator" content="aiq-agentcore exports">
<meta name="description" content="Research report {esc(pkg.job_id)}">
<style>{css}</style></head>
<body><header class="doc"><h1>{esc(pkg.title)}</h1><div class="gradient-bar" aria-hidden="true"></div>
<p class="meta">{meta}</p><nav class="toc no-print" aria-label="Sections">{toc}</nav></header>
<main>
{blocks_html(pkg.body, pkg)}
{artifacts_html(pkg)}
{sources_html(pkg)}
</main>
<footer class="doc">AI-Q on Amazon Bedrock AgentCore · research package {esc(pkg.job_id)} · rendered {rendered} · \
citations link to the Sources list; every source was retrieved during the job.</footer>
</body></html>
"""

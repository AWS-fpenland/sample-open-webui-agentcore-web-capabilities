// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Report rendering: marked → DOMPurify (threat model: no raw HTML from packages reaches the DOM unsanitized).
// `[n]` markers become links to #src-n; `artifact://<id>` references become #art-<id> placeholders that the
// Report tab resolves to presigned URLs after mount.
import DOMPurify from 'dompurify';
import { marked } from 'marked';

marked.setOptions({ gfm: true, breaks: false });

// (guarded so the module can be imported in Node for the render smoke test; browsers always have addHook)
if (typeof DOMPurify.addHook === 'function') DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    const href = node.getAttribute('href') ?? '';
    if (/^https?:/i.test(href)) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  }
});

const SOURCES_HEADING = /^#{1,3}\s*(Sources|References|Bibliography)\s*$/im;

/** Cut a trailing "Sources" section (the workbench renders a structured, verified list instead). */
export function splitSourcesSection(md: string): { body: string; tail: string | null } {
  const m = SOURCES_HEADING.exec(md);
  if (!m) return { body: md, tail: null };
  const rest = md.slice(m.index + m[0].length);
  const level = m[0].match(/^#+/)?.[0].length ?? 2;
  if (new RegExp(`^#{1,${level}}\\s`, 'm').test(rest)) return { body: md, tail: null };
  return { body: md.slice(0, m.index).trimEnd(), tail: rest.trim() };
}

export function renderMarkdown(md: string): string {
  const pre = md.replace(/artifact:\/\/(art_[a-f0-9]+)/g, '#art-$1');
  let html = marked.parse(pre, { async: false }) as string;
  html = html.replace(/\[(\d{1,3})\](?![^<]*>)/g, (_m, n: string) => `<a class="cite" href="#src-${n}" aria-label="citation ${n}">[${n}]</a>`);
  return String(DOMPurify.sanitize(html, { USE_PROFILES: { html: true }, ADD_ATTR: ['target'] }));
}

export function renderInline(md: string): string {
  return String(DOMPurify.sanitize(marked.parseInline(md, { async: false }) as string, { USE_PROFILES: { html: true } }));
}

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501  (embedded CSS / template strings)
"""Design tokens embedded as a string constant (fallback for the packaged ``tokens.css``).

Decision (11-architecture.md §1, §8 / ADR-26): every surface of the product shares one token set, ``design/tokens.css``;
the styled HTML export embeds it so an exported report looks like the Workbench that produced it. ``render_html`` loads
``tokens.css`` from package data via ``importlib.resources`` and falls back to ``TOKENS_CSS`` when the data file is not
installed. This module is GENERATED from ``design/tokens.css`` — regenerate both files together, never edit by hand.
"""

TOKENS_CSS = r"""/* AI-Q Research Workbench — design tokens (dark default, light via [data-theme="light"])
   House style: glassmorphism surfaces with a teal → violet → amber gradient accent.
   Contrast targets: WCAG 2.1 AA (≥ 4.5:1 body text, ≥ 3:1 large text / UI components) in both themes. */
:root {
  /* brand gradient stops */
  --aiq-teal: #14b8a6;      /* teal-500 */
  --aiq-violet: #8b5cf6;    /* violet-500 */
  --aiq-amber: #f59e0b;     /* amber-500 */
  --aiq-gradient: linear-gradient(120deg, var(--aiq-teal) 0%, var(--aiq-violet) 55%, var(--aiq-amber) 100%);
  --aiq-gradient-soft: linear-gradient(120deg, rgba(20,184,166,.18), rgba(139,92,246,.18) 55%, rgba(245,158,11,.18));

  /* dark theme (default) */
  --bg: #0b0f17;            /* app background */
  --bg-2: #0f1522;          /* raised panels */
  --glass: rgba(255,255,255,.055);
  --glass-strong: rgba(255,255,255,.09);
  --glass-border: rgba(255,255,255,.12);
  --glass-blur: 14px;
  --fg: #e8ecf3;            /* body text  — 14.9:1 on --bg */
  --fg-muted: #a5adbd;      /* secondary  — 7.6:1 on --bg */
  --fg-faint: #7c8596;      /* tertiary   — 4.6:1 on --bg (AA body) */
  --accent: #5eead4;        /* teal-300 for links/focus on dark — 10.4:1 */
  --accent-2: #c4b5fd;      /* violet-300 */
  --accent-3: #fcd34d;      /* amber-300 */
  --ok: #4ade80;  --warn: #fbbf24;  --err: #f87171;  --info: #7dd3fc;
  --ok-bg: rgba(74,222,128,.14); --warn-bg: rgba(251,191,36,.14); --err-bg: rgba(248,113,113,.14); --info-bg: rgba(125,211,252,.14);
  --focus: 0 0 0 3px rgba(94,234,212,.55);
  --shadow: 0 10px 30px rgba(0,0,0,.45);

  /* type */
  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Inter, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --fs-xs: .75rem; --fs-sm: .875rem; --fs-md: 1rem; --fs-lg: 1.125rem; --fs-xl: 1.375rem; --fs-2xl: 1.75rem; --fs-3xl: 2.25rem;
  --lh: 1.5;

  /* space / radius / motion / density */
  --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-5: 24px; --s-6: 32px; --s-7: 48px;
  --r-sm: 8px; --r-md: 12px; --r-lg: 18px; --r-pill: 999px;
  --ease: cubic-bezier(.2,.8,.2,1); --t-fast: 120ms; --t-med: 220ms; --t-slow: 400ms;
  --row-h: 44px;            /* comfortable density */
}
[data-density="compact"] { --row-h: 34px; --s-4: 12px; --s-5: 16px; --fs-md: .9375rem; }
[data-theme="light"] {
  --bg: #f6f7fb; --bg-2: #ffffff;
  --glass: rgba(255,255,255,.65); --glass-strong: rgba(255,255,255,.85); --glass-border: rgba(15,23,42,.10);
  --fg: #0f172a;            /* 15.4:1 */
  --fg-muted: #475569;      /* 7.4:1 */
  --fg-faint: #64748b;      /* 4.9:1 */
  --accent: #0f766e;        /* teal-700 — 5.9:1 on --bg */
  --accent-2: #6d28d9;      /* violet-700 */
  --accent-3: #b45309;      /* amber-700 */
  --ok: #15803d; --warn: #b45309; --err: #b91c1c; --info: #0369a1;
  --ok-bg: rgba(21,128,61,.10); --warn-bg: rgba(180,83,9,.10); --err-bg: rgba(185,28,28,.10); --info-bg: rgba(3,105,161,.10);
  --focus: 0 0 0 3px rgba(15,118,110,.45);
  --shadow: 0 10px 30px rgba(15,23,42,.10);
}
@media (prefers-reduced-motion: reduce) { :root { --t-fast: 0ms; --t-med: 0ms; --t-slow: 0ms; } }

/* base */
html, body { background: var(--bg); color: var(--fg); font-family: var(--font-sans); font-size: var(--fs-md); line-height: var(--lh); }
body { background-image: radial-gradient(1200px 600px at 10% -10%, rgba(20,184,166,.12), transparent 60%),
                         radial-gradient(900px 500px at 100% 0%, rgba(139,92,246,.14), transparent 60%),
                         radial-gradient(700px 400px at 60% 110%, rgba(245,158,11,.10), transparent 60%); background-attachment: fixed; }
a { color: var(--accent); text-underline-offset: 2px; }
:focus-visible { outline: none; box-shadow: var(--focus); border-radius: var(--r-sm); }
.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }

/* components */
.glass { background: var(--glass); border: 1px solid var(--glass-border); border-radius: var(--r-lg); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur)); box-shadow: var(--shadow); }
.gradient-text { background: var(--aiq-gradient); -webkit-background-clip: text; background-clip: text; color: transparent; }
.gradient-bar { height: 3px; background: var(--aiq-gradient); border-radius: var(--r-pill); }
.btn { display:inline-flex; align-items:center; gap: var(--s-2); height: 36px; padding: 0 var(--s-4); border-radius: var(--r-md); border: 1px solid var(--glass-border); background: var(--glass-strong); color: var(--fg); font-size: var(--fs-sm); font-weight: 600; cursor: pointer; transition: transform var(--t-fast) var(--ease), background var(--t-fast); }
.btn:hover { transform: translateY(-1px); background: var(--glass); }
.btn-primary { background: var(--aiq-gradient); color: #0b0f17; border: none; }
[data-theme="light"] .btn-primary { color: #fff; }
.btn-ghost { background: transparent; }
.chip { display:inline-flex; align-items:center; gap: 6px; height: 24px; padding: 0 10px; border-radius: var(--r-pill); font-size: var(--fs-xs); font-weight: 600; border: 1px solid var(--glass-border); background: var(--glass); color: var(--fg-muted); }
.chip-ok { color: var(--ok); background: var(--ok-bg); } .chip-warn { color: var(--warn); background: var(--warn-bg); }
.chip-err { color: var(--err); background: var(--err-bg); } .chip-info { color: var(--info); background: var(--info-bg); }
.card { padding: var(--s-5); }
.kpi { display:flex; flex-direction:column; gap: 2px; } .kpi b { font-size: var(--fs-xl); font-weight: 700; } .kpi span { color: var(--fg-muted); font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: .06em; }
.table { width:100%; border-collapse: collapse; font-size: var(--fs-sm); } .table th { text-align:left; color: var(--fg-muted); font-weight:600; padding: 0 var(--s-3); height: var(--row-h); border-bottom: 1px solid var(--glass-border); }
.table td { padding: 0 var(--s-3); height: var(--row-h); border-bottom: 1px solid var(--glass-border); } .table tr:hover td { background: var(--glass); }
.skeleton { background: linear-gradient(90deg, var(--glass), var(--glass-strong), var(--glass)); background-size: 200% 100%; animation: shimmer 1.2s infinite; border-radius: var(--r-sm); }
@keyframes shimmer { from { background-position: 200% 0 } to { background-position: -200% 0 } }
.progress { height: 6px; background: var(--glass); border-radius: var(--r-pill); overflow:hidden; } .progress > i { display:block; height:100%; background: var(--aiq-gradient); border-radius: var(--r-pill); transition: width var(--t-slow) var(--ease); }
.empty { text-align:center; color: var(--fg-muted); padding: var(--s-7) var(--s-5); } .empty h3 { color: var(--fg); margin: var(--s-3) 0 var(--s-2); }
.model-name { font-weight: 600; } .model-id { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--fg-faint); }
/* layout */
.app { display:grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
.nav { padding: var(--s-5) var(--s-4); position: sticky; top: 0; height: 100vh; }
.nav a { display:flex; align-items:center; gap: var(--s-3); height: 40px; padding: 0 var(--s-3); border-radius: var(--r-md); color: var(--fg-muted); text-decoration:none; font-weight: 600; font-size: var(--fs-sm); }
.nav a[aria-current="page"], .nav a:hover { background: var(--glass-strong); color: var(--fg); }
.main { padding: var(--s-6); max-width: 1400px; }
.topbar { display:flex; align-items:center; justify-content: space-between; gap: var(--s-4); margin-bottom: var(--s-5); }
.grid { display:grid; gap: var(--s-4); } .grid-3 { grid-template-columns: repeat(3, 1fr); } .grid-2 { grid-template-columns: repeat(2, 1fr); } .grid-4 { grid-template-columns: repeat(4, 1fr); }
@media (max-width: 1024px) { .app { grid-template-columns: 72px 1fr; } .nav a span { display:none; } .grid-3, .grid-4 { grid-template-columns: repeat(2, 1fr); } .main { padding: var(--s-5); } }
@media (max-width: 640px) { .app { grid-template-columns: 1fr; } .nav { position: static; height:auto; display:flex; gap: var(--s-2); overflow-x:auto; padding: var(--s-3); } .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; } .main { padding: var(--s-4); } .table th:nth-child(n+4), .table td:nth-child(n+4) { display:none; } }
"""

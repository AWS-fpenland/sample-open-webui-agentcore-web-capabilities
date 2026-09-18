#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501  (browser harness: long selectors and JS snippets)
"""Accessibility + keyboard + reload checks of the Research Workbench (AgentCore Browser, Playwright over signed CDP).

  python aiq/scripts/workbench_axe.py --url https://aiq.example --username user@x --password-env PW --axe /path/axe.min.js \
      --routes /,/lab,/p/<job> --out-dir shots --result-out result.json

For each route (desktop, both themes): inject axe-core (context created with bypass_csp so the injected script is allowed;
the deployed CSP itself stays untouched) and run the WCAG 2.0/2.1 A+AA rule set; record violations by rule, impact, node count.
Keyboard walk on the first route: Tab through the page, record focus order and whether :focus-visible applies, then Enter on
the first focused link. Reload check: reload a package route and confirm the session survives (no Cognito redirect, same h1).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(__file__))
from workbench_e2e import cognito_login, start_browser, summarize  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-env", default="WB_PASSWORD")
    ap.add_argument("--axe", required=True, help="path to axe.min.js")
    ap.add_argument("--routes", default="/,/lab")
    ap.add_argument("--reload-route", default=None, help="route to reload mid-session (defaults to the last route)")
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--tabs", type=int, default=24)
    ap.add_argument("--settle", type=int, default=6000)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--result-out", default=None)
    ap.add_argument("--session-timeout", type=int, default=900)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    password = os.environ[a.password_env]
    axe_src = open(a.axe, encoding="utf-8").read()
    routes = [r for r in a.routes.split(",") if r]
    client, session_id, ws_url, headers = start_browser(a.region, a.session_timeout)
    print(f"# browser session {session_id}", file=sys.stderr)
    out: dict = {"browser_session": session_id, "axe_version": None, "scans": [], "keyboard": None, "reload": None, "console": []}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60_000)
            context = browser.new_context(bypass_csp=True, viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on("pageerror", lambda e: out["console"].append({"type": "pageerror", "text": str(e)[:300]}))
            page.goto(a.url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            for sel in ["button:has-text('Sign in')", "a:has-text('Sign in')"]:
                if page.locator(sel).count():
                    page.locator(sel).first.click()
                    break
            cognito_login(page, a.username, password, a.out_dir, "axe-login")
            page.wait_for_url(re.compile(re.escape(a.url.rstrip("/")) + r".*"), timeout=60_000)
            page.wait_for_timeout(a.settle)
            for route in routes:
                for theme in a.themes.split(","):
                    page.goto(a.url.rstrip("/") + route, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(a.settle)
                    page.evaluate("t => { document.documentElement.setAttribute('data-theme', t); localStorage.setItem('aiq.theme', t); }", theme)  # noqa: E501
                    page.wait_for_timeout(500)
                    page.add_script_tag(content=axe_src)
                    res = page.evaluate("""async () => {
                        const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}});
                        return {version: axe.version,
                          violations: r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length,
                              targets: v.nodes.slice(0, 3).map(n => n.target.join(' ')), html: v.nodes.slice(0, 2).map(n => n.html.slice(0, 160))})),
                          passes: r.passes.length, incomplete: r.incomplete.map(i => ({id: i.id, nodes: i.nodes.length}))};
                    }""")
                    out["axe_version"] = res["version"]
                    summ = summarize(page)
                    out["scans"].append({"route": route, "theme": theme, "h1": summ.get("h1"), "rows": summ.get("rows"),
                                         "violations": res["violations"], "passes": res["passes"], "incomplete": res["incomplete"]})
                    print(f"# axe {route} {theme}: {len(res['violations'])} violations "
                          f"({', '.join(v['id'] + ':' + str(v['nodes']) for v in res['violations'])}) passes={res['passes']}", file=sys.stderr)
            # keyboard walkthrough on the first route (dark)
            page.goto(a.url.rstrip("/") + routes[0], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(a.settle)
            page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
            page.mouse.click(2, 2)  # reset focus to the document
            order = []
            for _ in range(a.tabs):
                page.keyboard.press("Tab")
                page.wait_for_timeout(60)
                info = page.evaluate("""() => { const e = document.activeElement; if (!e) return null;
                    const cs = getComputedStyle(e); return {tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
                    text: (e.innerText || e.getAttribute('aria-label') || e.value || '').trim().slice(0, 50), href: e.getAttribute('href'),
                    focusVisible: e.matches(':focus-visible'), outline: cs.outlineStyle !== 'none' && cs.outlineWidth !== '0px',
                    boxShadow: cs.boxShadow !== 'none'}; }""")
                order.append(info)
            page.screenshot(path=os.path.join(a.out_dir, "keyboard-focus.png"))
            # Enter on the first focused link that points into the app
            link_idx = next((i for i, o in enumerate(order) if o and o.get("tag") == "a" and (o.get("href") or "").startswith("/")), None)
            entered = None
            if link_idx is not None:
                page.mouse.click(2, 2)
                for _ in range(link_idx + 1):
                    page.keyboard.press("Tab")
                    page.wait_for_timeout(40)
                before = page.url
                page.keyboard.press("Enter")
                page.wait_for_timeout(2500)
                entered = {"pressed_on": order[link_idx], "url_before": before, "url_after": page.url, "h1": summarize(page).get("h1")}
            reachable = [o for o in order if o]
            out["keyboard"] = {"tabs": a.tabs, "order": order, "focus_visible_count": sum(1 for o in reachable if o.get("focusVisible")),
                               "with_visible_indicator": sum(1 for o in reachable if o.get("focusVisible") and (o.get("outline") or o.get("boxShadow"))),
                               "distinct_targets": len({(o.get('tag'), o.get('text')) for o in reachable}), "enter_on_first_link": entered}
            print(f"# keyboard: {len(reachable)} focus stops, focus-visible {out['keyboard']['focus_visible_count']}, "
                  f"visible indicator {out['keyboard']['with_visible_indicator']}, enter -> {entered and entered['url_after']}", file=sys.stderr)
            # reload mid-session
            rr = a.reload_route or routes[-1]
            page.goto(a.url.rstrip("/") + rr, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(a.settle)
            h1_before = summarize(page).get("h1")
            t0 = time.time()
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(a.settle)
            summ = summarize(page)
            out["reload"] = {"route": rr, "h1_before": h1_before, "h1_after": summ.get("h1"), "url_after": page.url,
                             "redirected_to_cognito": "amazoncognito.com" in page.url, "seconds": round(time.time() - t0, 1),
                             "signed_in_text": bool(re.search(r"Sign out", summ.get("text") or ""))}
            page.screenshot(path=os.path.join(a.out_dir, "after-reload.png"))
            print(f"# reload {rr}: h1 {h1_before} -> {summ.get('h1')} cognito_redirect={out['reload']['redirected_to_cognito']}", file=sys.stderr)
            context.close()
            browser.close()
    finally:
        try:
            client.stop()
            out["browser_session_stopped"] = True
        except Exception as e:  # noqa: BLE001
            out["browser_session_stopped"] = f"failed: {e}"
    if a.result_out:
        with open(a.result_out, "w") as f:
            json.dump(out, f, indent=2)
    print(json.dumps({"scans": [{k: s[k] for k in ("route", "theme", "h1", "passes")} | {"violations": [(v["id"], v["impact"], v["nodes"]) for v in s["violations"]]} for s in out["scans"]],
                      "keyboard": {k: v for k, v in (out["keyboard"] or {}).items() if k != "order"}, "reload": out["reload"], "console": out["console"]}, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

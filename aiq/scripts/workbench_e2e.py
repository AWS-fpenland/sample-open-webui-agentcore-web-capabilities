#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Browser-driven checks of the Research Workbench (AgentCore Browser + Playwright over signed CDP).

  python aiq/scripts/workbench_e2e.py --url https://aiq.example --username user@x --password-env PW --out-dir shots \
      [--routes /,/lab,/p/<job>,/compare/<a>/<b>,/exports/<job>] [--breakpoints desktop,tablet,mobile] [--themes dark,light]
      [--owui-url https://oui.example]   # when given, signs in to Open WebUI first so the Cognito session already exists
      [--result-out result.json]

Signs in through Cognito Managed Login (the Workbench's own PKCE client), then visits each route at each breakpoint and theme,
saving screenshots + a DOM summary (headings, table rows, error banners). Never prints tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BREAKPOINTS = {"desktop": (1440, 900), "tablet": (900, 1100), "mobile": (390, 844)}


def start_browser(region: str, timeout_s: int):
    from bedrock_agentcore.tools.browser_client import BrowserClient

    client = BrowserClient(region)
    session_id = client.start(identifier="aws.browser.v1", session_timeout_seconds=timeout_s, name="aiq-workbench-e2e")
    ws_url, headers = client.generate_ws_headers()
    return client, session_id, ws_url, headers


def cognito_login(page, username: str, password: str, out_dir: str, tag: str) -> None:
    """Fill the Cognito Managed Login form if it is showing (skipped when the hosted session already exists)."""
    page.wait_for_timeout(2500)
    if "amazoncognito.com" not in page.url:
        return
    page.screenshot(path=os.path.join(out_dir, f"{tag}-cognito.png"))
    page.wait_for_selector("input:not([type='hidden'])", state="attached", timeout=45_000)
    pw = page.locator("input[type='password']")
    if pw.count() == 0:
        first = page.locator("input:not([type='hidden']):not([type='password'])").first
        first.click()
        first.fill(username)
        page.keyboard.press("Enter")
        page.wait_for_selector("input[type='password']", timeout=45_000)
        pw = page.locator("input[type='password']")
    else:
        inputs = page.locator("input:not([type='hidden']):not([type='password']):not([type='checkbox'])")
        for i in range(inputs.count()):
            cand = inputs.nth(i)
            try:
                if cand.is_visible():
                    cand.click()
                    cand.fill(username)
                    break
            except Exception:  # noqa: BLE001
                continue
    pw.first.click()
    pw.first.fill(password)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4000)


def summarize(page) -> dict:
    return page.evaluate("""() => ({
        url: location.href, title: document.title,
        h1: [...document.querySelectorAll('h1')].map(e => e.innerText.trim()).slice(0, 5),
        h2: [...document.querySelectorAll('h2')].map(e => e.innerText.trim()).slice(0, 12),
        rows: document.querySelectorAll('table tbody tr').length,
        buttons: [...document.querySelectorAll('button, a.btn')].map(e => e.innerText.trim()).filter(Boolean).slice(0, 40),
        errors: [...document.querySelectorAll('[role=alert], .error, .banner-error')].map(e => e.innerText.trim()).slice(0, 5),
        text: (document.body ? document.body.innerText : '').slice(0, 1500),
        theme: document.documentElement.getAttribute('data-theme'),
    })""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--owui-url", default=None)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-env", default="WB_PASSWORD")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--routes", default="/,/lab")
    ap.add_argument("--breakpoints", default="desktop,tablet,mobile")
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--settle", type=int, default=6000, help="ms to wait after navigation for data to load")
    ap.add_argument("--result-out", default=None)
    ap.add_argument("--session-timeout", type=int, default=1200)
    ap.add_argument("--click", action="append", default=[], help="after the first route: click a button by text and screenshot")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    password = os.environ[a.password_env]
    client, session_id, ws_url, headers = start_browser(a.region, a.session_timeout)
    print(f"# browser session {session_id}", file=sys.stderr)
    out: dict = {"browser_session": session_id, "pages": [], "clicks": []}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})
            if a.owui_url:  # one identity: sign in to Open WebUI first, the Workbench then reuses the Cognito session
                page.goto(a.owui_url, wait_until="domcontentloaded", timeout=60_000)
                cognito_login(page, a.username, password, a.out_dir, "00-owui")
                page.wait_for_timeout(3000)
                page.screenshot(path=os.path.join(a.out_dir, "00-owui-signed-in.png"))
                out["owui_signed_in_url"] = page.url
            page.goto(a.url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            page.screenshot(path=os.path.join(a.out_dir, "01-workbench-landing.png"))
            # sign-in button if the SPA gates on auth
            for sel in ["button:has-text('Sign in')", "a:has-text('Sign in')", "button:has-text('Sign In')"]:
                if page.locator(sel).count():
                    page.locator(sel).first.click()
                    break
            t0 = time.time()
            cognito_login(page, a.username, password, a.out_dir, "02-workbench")
            page.wait_for_url(re.compile(re.escape(a.url.rstrip("/")) + r".*"), timeout=60_000)
            page.wait_for_timeout(a.settle)
            out["signin_seconds"] = round(time.time() - t0, 1)
            out["signin_via_hosted_form"] = os.path.exists(os.path.join(a.out_dir, "02-workbench-cognito.png"))
            page.screenshot(path=os.path.join(a.out_dir, "03-workbench-signed-in.png"))
            for route in [r for r in a.routes.split(",") if r]:
                for theme in a.themes.split(","):
                    for bp in a.breakpoints.split(","):
                        w, h = BREAKPOINTS[bp]
                        page.set_viewport_size({"width": w, "height": h})
                        page.goto(a.url.rstrip("/") + route, wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(a.settle if bp == "desktop" else 2500)
                        page.evaluate("t => { document.documentElement.setAttribute('data-theme', t); localStorage.setItem('aiq.theme', t); }", theme)
                        page.wait_for_timeout(400)
                        name = route.strip("/").replace("/", "_") or "library"
                        path = os.path.join(a.out_dir, f"{name}-{theme}-{bp}.png")
                        page.screenshot(path=path, full_page=(bp != "desktop"))
                        summ = summarize(page) if bp == "desktop" else {"url": page.url}
                        out["pages"].append({"route": route, "theme": theme, "breakpoint": bp, "shot": os.path.basename(path), **summ})
                        print(f"# {route} {theme} {bp} rows={summ.get('rows')} h1={summ.get('h1')}", file=sys.stderr)
            page.set_viewport_size({"width": 1440, "height": 900})
            for i, label in enumerate(a.click):
                page.goto(a.url.rstrip("/") + a.routes.split(",")[0], wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(a.settle)
                btn = page.locator(f"button:has-text('{label}'), a:has-text('{label}')")
                ok = btn.count() > 0
                if ok:
                    btn.first.click()
                    page.wait_for_timeout(3500)
                page.screenshot(path=os.path.join(a.out_dir, f"click-{i}-{re.sub(r'[^a-z0-9]+', '-', label.lower())}.png"))
                out["clicks"].append({"label": label, "found": ok, "url": page.url, **({"summary": summarize(page)} if ok else {})})
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
    print(json.dumps({k: v for k, v in out.items() if k != "pages"}, indent=2)[:3000])
    print(json.dumps([{k: p.get(k) for k in ("route", "theme", "breakpoint", "rows", "h1", "errors")} for p in out["pages"]], indent=1)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

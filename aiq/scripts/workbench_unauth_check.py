#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501
"""Prove that no Workbench route renders without a session: a fresh AgentCore Browser (no profile, no cookies) visits each route
and must end up on Cognito Managed Login without ever showing application content or fixtures.

  python aiq/scripts/workbench_unauth_check.py --url https://aiq.example --out-dir shots --result-out result.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(__file__))
from workbench_e2e import start_browser  # noqa: E402

CONTENT_MARKERS = re.compile(r"Research Library|Model Lab|mock mode|mock identity|preview with fixtures|Compare two runs|Export center", re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--routes", default="/,/?mock=1,/lab,/p/job_00000000000000000000000000000000,/compare/a/b,/exports/x,/signout")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--result-out", default=None)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    client, session_id, ws_url, headers = start_browser(a.region, 600)
    out: dict = {"browser_session": session_id, "routes": []}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60_000)
            for i, route in enumerate([r for r in a.routes.split(",") if r]):
                context = browser.new_context(viewport={"width": 1440, "height": 900})  # fresh context: no cookies, no storage
                page = context.new_page()
                page.goto(a.url.rstrip("/") + route, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(700)
                early = page.evaluate("() => ({url: location.href, text: (document.body ? document.body.innerText : '').slice(0, 400)})")
                page.wait_for_timeout(6000)
                final_url = page.url
                text = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 400)") if "amazoncognito.com" not in final_url else ""
                shot = os.path.join(a.out_dir, f"{i:02d}-{re.sub(r'[^a-z0-9]+', '-', route.lower()).strip('-') or 'root'}.png")
                page.screenshot(path=shot)
                rec = {
                    "route": route,
                    "early_url": early["url"], "early_text": early["text"][:160],
                    "final_url": final_url[:200],
                    "redirected_to_cognito": "amazoncognito.com" in final_url,
                    "content_rendered": bool(CONTENT_MARKERS.search(early["text"] or "")) or bool(CONTENT_MARKERS.search(text or "")),
                    "shot": os.path.basename(shot),
                }
                out["routes"].append(rec)
                print(f"# {route}: early='{rec['early_text'][:60]}' → cognito={rec['redirected_to_cognito']} content={rec['content_rendered']}", file=sys.stderr)
                context.close()
            browser.close()
    finally:
        try:
            client.stop()
            out["browser_session_stopped"] = True
        except Exception as e:  # noqa: BLE001
            out["browser_session_stopped"] = f"failed: {e}"
    out["all_redirected"] = all(r["redirected_to_cognito"] for r in out["routes"])
    out["any_content_without_session"] = any(r["content_rendered"] for r in out["routes"])
    if a.result_out:
        with open(a.result_out, "w") as f:
            json.dump(out, f, indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "routes"}, indent=1))
    return 0 if out["all_redirected"] and not out["any_content_without_session"] else 1


if __name__ == "__main__":
    sys.exit(main())

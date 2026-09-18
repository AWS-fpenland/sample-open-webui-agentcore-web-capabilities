#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: E501
"""Verify the Workbench → Open WebUI deep link: /?models=<id>&q=<text>&submit=false must pre-select the model and prefill the composer
without sending. Uses the same AgentCore Browser + Cognito login as owui_e2e.py."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(__file__))
from owui_e2e import login, shot, start_browser  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-env", default="OWUI_PASSWORD")
    ap.add_argument("--model", default="aiq_agentcore.deep")
    ap.add_argument("--q", default="Deep-link check: this text must appear in the composer and must not be sent.")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--result-out", default=None)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    client, session_id, ws_url, headers = start_browser(a.region, 600)
    out: dict = {"browser_session": session_id}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})
            out["login"] = login(page, a.url, a.username, os.environ[a.password_env], a.out_dir)
            link = a.url.rstrip("/") + "/?" + urllib.parse.urlencode({"models": a.model, "q": a.q, "submit": "false"})
            out["link"] = link
            page.goto(link, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6000)
            shot(page, a.out_dir, "deeplink.png")
            out["composer"] = page.evaluate("""() => { const b = document.querySelector('#chat-input'); return b ? (b.value ?? b.innerText ?? '').trim() : null; }""")
            out["selected_models"] = page.evaluate("""() => [...document.querySelectorAll('button')].map(b => b.innerText.trim()).filter(t => /AI-Q/i.test(t)).slice(0, 4)""")
            out["url_after"] = page.url
            out["sent"] = "/c/" in page.url  # a chat id in the URL would mean the message was submitted
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
    print(json.dumps({k: v for k, v in out.items() if k != "login"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

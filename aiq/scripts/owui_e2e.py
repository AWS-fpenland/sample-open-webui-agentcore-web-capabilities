#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Browser-driven Open WebUI checks for AI-Q on AgentCore, using an Amazon Bedrock AgentCore
Browser session (AWS-managed browser) driven with Playwright over signed CDP.

No local browser is needed. Screenshots are written to --out-dir; the Open WebUI session token
(for the pipe installer) is written to --token-out with mode 0600 and never printed.

Actions
  login                       Cognito Managed Login as --username (password from --password-env), save token + screenshot
  chat --model <id> --prompt  Sign in, start a new chat with the model, send the prompt, wait for the answer, screenshot,
                              dump message text/sources/status lines as JSON (--result-out)
  reload                      Reload the current chat mid-stream (used by the reconnect test), screenshot
Usage (AWS credentials for the account that hosts the browser session in the environment):
  python aiq/scripts/owui_e2e.py --url https://oui.example --username user@x --password-env PW \
      --out-dir ./shots --token-out /private/token.txt login
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright


def start_browser(region: str, timeout_s: int):
    from bedrock_agentcore.tools.browser_client import BrowserClient

    client = BrowserClient(region)
    session_id = client.start(identifier="aws.browser.v1", session_timeout_seconds=timeout_s, name="aiq-owui-e2e")
    ws_url, headers = client.generate_ws_headers()
    return client, session_id, ws_url, headers


def shot(page, out_dir: str, name: str) -> str:
    path = os.path.join(out_dir, name)
    page.screenshot(path=path, full_page=False)
    print(f"# screenshot {path}", file=sys.stderr)
    return path


def login(page, url: str, username: str, password: str, out_dir: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2500)
    # Open WebUI auto-redirects to Cognito Managed Login; if a "Continue with" button shows instead, click it.
    if "amazoncognito.com" not in page.url and "cognito" not in page.url:
        for sel in ["button:has-text('Continue with')", "button:has-text('Amazon Cognito')", "button:has-text('Sign in')"]:
            if page.locator(sel).count():
                page.locator(sel).first.click()
                page.wait_for_timeout(2500)
                break
    shot(page, out_dir, "01-login-page.png")
    user_sel = "input[name='username'], input[type='email'], input#signInFormUsername, input[autocomplete='username']"
    pw_sel = "input[name='password'], input[type='password'], input#signInFormPassword"
    page.wait_for_selector(user_sel, timeout=30_000)
    page.locator(user_sel).first.fill(username)
    # Managed Login may ask for the username first, then password on the next screen.
    if page.locator(pw_sel).count() == 0:
        page.keyboard.press("Enter")
        page.wait_for_selector(pw_sel, timeout=30_000)
    page.locator(pw_sel).first.fill(password)
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(re.escape(url.rstrip("/")) + r".*"), timeout=60_000)
    page.wait_for_timeout(4000)
    # Dismiss release notes / first-run dialogs if present.
    for sel in ["button:has-text('Okay, Let')", "button:has-text('Okay')", "button:has-text('Got it')", "button[aria-label='Close']"]:
        try:
            if page.locator(sel).count():
                page.locator(sel).first.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass
    token = page.evaluate("() => localStorage.getItem('token')")
    me = None
    if token:
        me = page.evaluate("""async (t) => { const r = await fetch('/api/v1/auths/', {headers: {Authorization: 'Bearer ' + t}}); return r.ok ? await r.json() : {status: r.status}; }""", token)
    shot(page, out_dir, "02-signed-in.png")
    return {"token": token, "me": {k: me.get(k) for k in ("id", "role", "name") if isinstance(me, dict)} if me else None}


def pick_model(page, model_id: str):
    """Open the model selector and pick the model whose id/name matches."""
    sel_btn = page.locator("button[aria-label='Select a model'], #model-selector-0-button, button:has-text('Select a model')")
    if sel_btn.count() == 0:
        sel_btn = page.locator("[id^='model-selector'] button").first
    sel_btn.first.click()
    page.wait_for_timeout(800)
    search = page.locator("input[placeholder*='Search a model'], input[placeholder*='Search']")
    if search.count():
        search.first.fill(model_id)
        page.wait_for_timeout(800)
    opt = page.locator(f"[data-value='{model_id}'], button:has-text('{model_id}'), div[role='option']:has-text('{model_id}')")
    if opt.count() == 0:
        opt = page.locator("div[role='option'], [role='menuitem']").first
    opt.first.click()
    page.wait_for_timeout(600)


def chat(page, url: str, model_id: str, prompt: str, out_dir: str, wait_s: int, reload_after_s: int | None) -> dict:
    page.goto(url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    pick_model(page, model_id)
    shot(page, out_dir, "10-model-selected.png")
    box = page.locator("#chat-input, textarea#chat-input, div#chat-input[contenteditable='true'], textarea").first
    box.click()
    box.fill(prompt) if box.evaluate("e => e.tagName") == "TEXTAREA" else box.type(prompt)
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    t0 = time.time()
    page.wait_for_timeout(2500)
    shot(page, out_dir, "11-sent.png")
    reloaded = False
    while time.time() - t0 < wait_s:
        if reload_after_s and not reloaded and time.time() - t0 > reload_after_s:
            shot(page, out_dir, "12-before-reload.png")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            reloaded = True
            shot(page, out_dir, "13-after-reload.png")
        # generating state ends when the stop button disappears and the last message has content
        stop_btn = page.locator("button[aria-label='Stop'], button:has-text('Stop')").count()
        done = page.evaluate("""() => { const m = document.querySelectorAll('[id^="message-"]'); if (!m.length) return null;
            const last = m[m.length-1]; return {text: last.innerText.slice(0, 20000)}; }""")
        if stop_btn == 0 and done and done.get("text") and time.time() - t0 > 6:
            break
        page.wait_for_timeout(2000)
    page.wait_for_timeout(1500)
    shot(page, out_dir, "19-answer.png")
    result = page.evaluate("""() => {
        const msgs = [...document.querySelectorAll('[id^="message-"]')];
        const last = msgs[msgs.length-1];
        const text = last ? last.innerText : '';
        const links = last ? [...last.querySelectorAll('a[href^="http"]')].map(a => a.href) : [];
        const statuses = last ? [...last.querySelectorAll('[class*="status"], .text-gray-500')].map(e => e.innerText).filter(Boolean).slice(0, 30) : [];
        return {url: location.href, text, links, statuses, message_count: msgs.length};
    }""")
    result["seconds"] = round(time.time() - t0, 1)
    result["reloaded"] = reloaded
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-env", default="OWUI_PASSWORD")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--token-out", default=None)
    ap.add_argument("--result-out", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--wait", type=int, default=300)
    ap.add_argument("--reload-after", type=int, default=None)
    ap.add_argument("--session-timeout", type=int, default=1500)
    ap.add_argument("action", choices=["login", "chat"])
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    password = os.environ[a.password_env]
    client, session_id, ws_url, headers = start_browser(a.region, a.session_timeout)
    print(f"# browser session {session_id}", file=sys.stderr)
    out: dict = {"browser_session": session_id, "action": a.action}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_viewport_size({"width": 1400, "height": 900})
            info = login(page, a.url, a.username, password, a.out_dir)
            out["me"] = info["me"]
            if a.token_out and info["token"]:
                with open(os.open(a.token_out, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as f:
                    f.write(info["token"])
                out["token_saved"] = True
            if a.action == "chat":
                out["chat"] = chat(page, a.url, a.model, a.prompt, a.out_dir, a.wait, a.reload_after)
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
    print(json.dumps({k: v for k, v in out.items() if k != "token"}, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

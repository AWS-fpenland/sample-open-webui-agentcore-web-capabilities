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
    # Cognito Managed Login renders a React form; attribute names vary, so locate by role/placeholder with fallbacks.
    try:
        page.wait_for_selector("input:not([type='hidden'])", state="attached", timeout=45_000)
    except Exception:
        # Diagnostics for the harness: where are we and what does the DOM contain?
        info = page.evaluate("() => ({url: location.href, inputs: document.querySelectorAll('input').length, "
                             "frames: window.frames.length, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 300)})")  # noqa: E501
        print(f"# login page diagnostics: {info}", file=sys.stderr)
        shot(page, out_dir, "01b-login-timeout.png")
        for fr in page.frames[1:]:
            try:
                if fr.locator("input").count():
                    print(f"# login inputs found in frame {fr.url}", file=sys.stderr)
            except Exception:
                pass
        raise
    page.wait_for_timeout(1000)
    pw = page.locator("input[type='password']")
    if pw.count() == 0:
        # username-first flow
        first = page.locator("input:not([type='hidden']):not([type='password'])").first
        first.click()
        first.fill(username)
        page.keyboard.press("Enter")
        page.wait_for_selector("input[type='password']", timeout=45_000)
        pw = page.locator("input[type='password']")
    else:
        user_inputs = page.locator("input:not([type='hidden']):not([type='password']):not([type='checkbox'])")
        target = None
        for i in range(user_inputs.count()):
            cand = user_inputs.nth(i)
            try:
                if cand.is_visible():
                    target = cand
                    break
            except Exception:
                continue
        if target is None:
            raise RuntimeError("no visible username input on the login page")
        target.click()
        target.fill(username)
    pw.first.click()
    pw.first.fill(password)
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(re.escape(url.rstrip("/")) + r".*"), timeout=60_000)
    page.wait_for_timeout(4000)
    # Dismiss release notes / first-run dialogs if present.
    for sel in ["button:has-text('Okay, Let')", "button:has-text('Okay')", "button:has-text('Got it')", "button[aria-label='Close']"]:  # noqa: E501
        try:
            if page.locator(sel).count():
                page.locator(sel).first.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass
    token = page.evaluate("() => localStorage.getItem('token')")
    me = None
    if token:
        me = page.evaluate("""async (t) => { const r = await fetch('/api/v1/auths/', {headers: {Authorization: 'Bearer ' + t}}); return r.ok ? await r.json() : {status: r.status}; }""", token)  # noqa: E501
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


def chat(page, url: str, model_id: str, prompt: str, out_dir: str, wait_s: int, reload_after_s: int | None,
         stop_after_s: int | None = None, follow_ups: list[str] | None = None, follow_up_wait: int = 600) -> dict:
    # Open WebUI pre-selects models from the `models` query parameter on a new chat (no DOM fiddling needed).
    page.goto(url.rstrip("/") + f"/?models={model_id}", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(4000)
    for sel in ["button:has-text('Okay, Let')", "button:has-text('Okay')", "button:has-text('Got it')"]:
        try:
            if page.locator(sel).count():
                page.locator(sel).first.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass
    shot(page, out_dir, "10-model-selected.png")
    selected = page.evaluate("() => [...document.querySelectorAll('button')].map(b => b.innerText.trim()).filter(t => /AI-Q/i.test(t)).slice(0,3)")  # noqa: E501
    print(f"# selected-model buttons: {selected}", file=sys.stderr)
    box = page.locator("#chat-input").first
    if box.count() == 0:
        box = page.locator("textarea, div[contenteditable='true']").first
    box.click()
    tag = box.evaluate("e => e.tagName")
    if tag == "TEXTAREA":
        box.fill(prompt)
    else:
        page.keyboard.type(prompt)
    page.wait_for_timeout(500)
    if prompt.startswith("/"):  # dismiss Open WebUI's slash prompt picker so Enter sends the text as typed
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    page.keyboard.press("Enter")
    t0 = time.time()
    page.wait_for_timeout(3000)
    shot(page, out_dir, "11-sent.png")
    # Completion is decided by Open WebUI's own task registry (GET /api/tasks/chat/{id}), not by DOM heuristics.
    m = re.search(r"/c/([0-9a-f-]{36})", page.url)
    chat_id = m.group(1) if m else None
    token = page.evaluate("() => localStorage.getItem('token')")
    reloaded = False
    stopped = None
    tasks = None
    while time.time() - t0 < wait_s:
        if stop_after_s and stopped is None and time.time() - t0 > stop_after_s:
            shot(page, out_dir, "14-before-stop.png")
            btn = page.locator("button[aria-label='Stop'], button:has-text('Stop')")
            if btn.count():
                btn.first.click()
                stopped = "ui-button"
            elif chat_id:
                stopped = page.evaluate("""async ([cid, t]) => { const r = await fetch('/api/tasks/chat/' + cid + '/stop', {method: 'POST', headers: {Authorization: 'Bearer ' + t}}); return 'api:' + r.status; }""", [chat_id, token])  # noqa: E501
            page.wait_for_timeout(3000)
            shot(page, out_dir, "15-after-stop.png")
        if reload_after_s and not reloaded and time.time() - t0 > reload_after_s:
            shot(page, out_dir, "12-before-reload.png")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            reloaded = True
            shot(page, out_dir, "13-after-reload.png")
        if chat_id:
            tasks = page.evaluate("""async ([cid, t]) => { const r = await fetch('/api/tasks/chat/' + cid, {headers: {Authorization: 'Bearer ' + t}}); return r.ok ? await r.json() : {status: r.status}; }""", [chat_id, token])  # noqa: E501
            ids = (tasks or {}).get("task_ids") or []
            if time.time() - t0 > 8 and not ids:
                break
        page.wait_for_timeout(3000)
    page.wait_for_timeout(1500)
    shot(page, out_dir, "19-answer.png")
    result = {"chat_id": chat_id, "url": page.url, "seconds": round(time.time() - t0, 1), "reloaded": reloaded, "stopped": stopped,  # noqa: E501
              "tasks": tasks, "follow_ups": []}
    # Optional follow-up turns in the same chat (used for clarification → approval round trips).
    for i, fu in enumerate(follow_ups or []):
        page.wait_for_timeout(1500)
        box = page.locator("#chat-input").first
        box.click()
        if box.evaluate("e => e.tagName") == "TEXTAREA":
            box.fill(fu)
        else:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(fu)
        page.wait_for_timeout(400)
        if fu.startswith("/"):
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        t1 = time.time()
        page.wait_for_timeout(3000)
        shot(page, out_dir, f"2{i}-followup-sent.png")
        while time.time() - t1 < follow_up_wait:
            tk = page.evaluate("""async ([cid, t]) => { const r = await fetch('/api/tasks/chat/' + cid, {headers: {Authorization: 'Bearer ' + t}}); return r.ok ? await r.json() : {status: r.status}; }""", [chat_id, token])  # noqa: E501
            if time.time() - t1 > 8 and not ((tk or {}).get("task_ids") or []):
                break
            page.wait_for_timeout(3000)
        page.wait_for_timeout(1500)
        shot(page, out_dir, f"2{i}-followup-answer.png")
        result["follow_ups"].append({"prompt": fu, "seconds": round(time.time() - t1, 1)})
    if chat_id:
        chat_json = page.evaluate("""async ([cid, t]) => { const r = await fetch('/api/v1/chats/' + cid, {headers: {Authorization: 'Bearer ' + t}}); return r.ok ? await r.json() : {status: r.status}; }""", [chat_id, token])  # noqa: E501
        # Open WebUI persists the full tree under chat.history.messages (chat.messages holds only the linear user turns).
        hist = ((((chat_json or {}).get("chat") or {}).get("history")) or {}).get("messages") or {}
        msgs = sorted(hist.values(), key=lambda x: x.get("timestamp") or 0) if isinstance(hist, dict) else []
        assistant = [x for x in msgs if x.get("role") == "assistant"]
        last = assistant[-1] if assistant else {}
        result["assistant_turns"] = [{"content": (x.get("content") or "")[:4000], "done": x.get("done"),
                                      "sources": len(x.get("sources") or []), "statuses": len(x.get("statusHistory") or [])}
                                     for x in assistant]
        result["assistant"] = {
            "content": (last.get("content") or "")[:20000],
            "done": last.get("done"),
            "model": last.get("model"),
            "sources": [{"name": (src.get("source") or {}).get("name"), "url": (src.get("source") or {}).get("url"),
                         "metadata": src.get("metadata")} for src in (last.get("sources") or [])],
            "statusHistory": [{"description": st.get("description"), "done": st.get("done")} for st in (last.get("statusHistory") or [])],  # noqa: E501
            "error": last.get("error"),
        }
        result["message_count"] = len(msgs)
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
    ap.add_argument("--stop-after", type=int, default=None, help="press Stop after N seconds (cancel test)")
    ap.add_argument("--follow-up", action="append", default=[], help="additional message(s) to send after the first answer")
    ap.add_argument("--follow-up-wait", type=int, default=600)
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
                out["chat"] = chat(page, a.url, a.model, a.prompt, a.out_dir, a.wait, a.reload_after, a.stop_after, a.follow_up, a.follow_up_wait)  # noqa: E501
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

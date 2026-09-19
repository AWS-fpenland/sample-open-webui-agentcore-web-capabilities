# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
title: AI-Q package actions
id: aiq_actions
description: Export, Re-run with…, Compare and Open buttons for AI-Q research packages on AgentCore.
version: 0.1.0
license: MIT-0
"""

# Open WebUI Action function (v0.11.3 contract, research-tracks/phase3-owui-surfaces Q1): sub-actions come from the
# module-level `actions` list; each click POSTs the message body; we resolve the package id from the assistant message's
# footer (`aiq-package:<job_id>`), call the AgentCore runtime with the user's own Cognito access token, and answer with
# persisted `message`/`files`/`status` events. Actions run inside the HTTP request, so long work (a re-run) is only
# *submitted* here — progress lives in the Workbench or via `/status` in chat. Regenerate fires actions with
# event.id == 'regenerate-response'; those calls are ignored (A Q1-09).

import asyncio
import io
import json
import re
import uuid
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

actions = [
    {"id": "export", "name": "Export… (PDF, DOCX, slides, ZIP)", "description": "Export this research package"},
    {"id": "rerun", "name": "Re-run with…", "description": "Re-run this research with different models"},
    {"id": "compare", "name": "Compare", "description": "Compare with the package this one re-ran"},
    {"id": "open", "name": "Open package", "description": "Open in the Research Workbench"},
]

PKG_RE = re.compile(r"aiq-package:(job_[a-f0-9]{32})")
JOB_RE = re.compile(r"aiq-job:(job_[a-f0-9]{32}):(?:completed|clarifying|running|failed|cancelled)")
FORMATS = ("pdf", "docx", "pptx", "html", "md", "json", "csv", "bibtex", "ris", "csl", "zip")


class Action:
    # Open WebUI reads the sub-action list from the *instance* (`function_module.actions`), not from the module.
    actions = actions

    class Valves(BaseModel):
        RUNTIME_ARN: str = Field(default="", description="AgentCore Runtime ARN for AI-Q.")
        REGION: str = Field(default="us-east-1")
        WORKBENCH_URL: str = Field(default="", description="Research Workbench base URL.")
        DEFAULT_FORMATS: str = Field(
            default="pdf,docx,pptx,zip", description="Formats produced by Export… when no choice is typed."
        )
        priority: int = Field(default=0)

    def __init__(self):
        self.valves = self.Valves()

    # ---------------------------------------------------------------- runtime call --
    def _endpoint(self) -> str:
        from urllib.parse import quote

        return f"https://bedrock-agentcore.{self.valves.REGION}.amazonaws.com/runtimes/{quote(self.valves.RUNTIME_ARN.strip(), safe='')}/invocations?qualifier=DEFAULT"  # noqa: E501

    async def _bearer(self, __user__: dict, __request__) -> str | None:
        try:
            oauth_manager = __request__.app.state.oauth_manager
            user_id = __user__.get("id")
            session_id = __request__.cookies.get("oauth_session_id")
            if not session_id:
                from open_webui.models.oauth_sessions import OAuthSessions

                sessions = [
                    s for s in await OAuthSessions.get_sessions_by_user_id(user_id) if not (s.provider or "").startswith("mcp:")
                ]
                if sessions:
                    session_id = sorted(sessions, key=lambda s: s.updated_at or 0)[-1].id
            if session_id:
                token = await oauth_manager.get_oauth_token(user_id, session_id)
                return (token or {}).get("access_token")
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _invoke(self, bearer: str, payload: dict, timeout_s: int = 180) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"aiq-action-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}",
        }
        out: list[dict] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http:
            async with http.post(self._endpoint(), data=json.dumps(payload).encode(), headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"runtime HTTP {resp.status}: {(await resp.text())[:300]}")
                async for raw in resp.content:
                    s = raw.decode("utf-8", "ignore").strip()
                    if s.startswith("data:"):
                        s = s[5:].strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                        if isinstance(obj, str):
                            obj = json.loads(obj)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
        return out

    @staticmethod
    def _package_id(body: dict) -> str | None:
        msgs = body.get("messages") or []
        target = next((m for m in msgs if m.get("id") == body.get("id")), None) or (msgs[-1] if msgs else None)
        content = (target or {}).get("content") or ""
        m = PKG_RE.findall(content) or JOB_RE.findall(content)
        return m[-1] if m else None

    def _wb(self, path: str) -> str | None:
        base = self.valves.WORKBENCH_URL.rstrip("/")
        return f"{base}{path}" if base else None

    # ---------------------------------------------------------------- entry --
    async def action(
        self,
        body: dict,
        __id__: str | None = None,
        __user__: dict | None = None,
        __event_emitter__=None,
        __event_call__=None,
        __request__=None,
    ):
        if (body.get("event") or {}).get("id") == "regenerate-response":
            return visible()
        __user__ = __user__ or {}
        sub = (__id__ or "").split(".")[-1] or "open"
        pkg = self._package_id(body)

        appended: list[str] = []

        async def say(text: str):
            # Live append for clients that render `message` events; the return value below makes it stick regardless
            # (Open WebUI v0.11 merges the action's returned `messages[*]` into the chat and persists it — verified live).
            appended.append(text)
            if __event_emitter__:
                await __event_emitter__({"type": "message", "data": {"content": "\n\n" + text}})

        def visible():
            if not appended:
                return visible()
            msgs = body.get("messages") or []
            current = next((m for m in reversed(msgs) if m.get("id") == body.get("id")), msgs[-1] if msgs else {})
            content = (current.get("content") or "").rstrip() + "".join("\n\n" + t for t in appended)
            return {"messages": [{"id": body.get("id") or current.get("id"), "role": "assistant", "content": content}]}

        async def status(text: str, done: bool = True):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        if not pkg:
            await say("_No research package is attached to this message._")
            return visible()
        if sub == "open":
            link = self._wb(f"/p/{pkg}")
            await say(
                f"**Research package** `{pkg}` — "
                + (f"[Open in Workbench ↗]({link})" if link else "Workbench URL is not configured.")
            )
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "notification", "data": {"type": "success", "content": "Workbench link added below the answer"}}
                )
            return visible()
        bearer = await self._bearer(__user__, __request__)
        if not bearer:
            await say("_Your Cognito session is required (sign in with SSO)._")
            return visible()
        if sub == "export":
            chosen = None
            if __event_call__:
                try:
                    chosen = await __event_call__(
                        {
                            "type": "input",
                            "data": {
                                "title": "Export formats",
                                "message": "Comma-separated: pdf, docx, pptx, html, md, json, csv, bibtex, ris, csl, zip",
                                "placeholder": self.valves.DEFAULT_FORMATS,
                            },
                        }
                    )
                except Exception:  # noqa: BLE001 — dialog closed / reload
                    chosen = None
            fmts = [
                f.strip().lower() for f in str(chosen or self.valves.DEFAULT_FORMATS).split(",") if f.strip().lower() in FORMATS
            ] or ["pdf"]
            lines = []
            files = []
            for fmt in fmts[:6]:
                await status(f"Rendering {fmt.upper()}…", done=False)
                try:
                    evs = await self._invoke(bearer, {"op": "export", "job_id": pkg, "format": fmt}, 240)
                except Exception as e:  # noqa: BLE001
                    lines.append(f"- {fmt.upper()}: failed ({e.__class__.__name__})")
                    continue
                err = next((e for e in evs if e.get("type") == "error"), None)
                ex = next((e for e in evs if e.get("type") == "export"), None)
                if err or not ex:
                    lines.append(
                        f"- {fmt.upper()}: {((err or {}).get('data') or {}).get('error', {}).get('message', 'no result')}"
                    )
                    continue
                d = ex["data"]
                lines.append(
                    f"- **{d['filename']}** — {(d.get('size') or 0) / 1024:.0f} KB{' · derived' if d.get('derived') else ''} · [download (10 min)]({d['url']})"  # noqa: E501
                )
                try:
                    fid = await self._attach(d["url"], d["filename"], d["content_type"], __user__)
                    if fid:
                        files.append(
                            {
                                "type": "file",
                                "id": fid,
                                "url": fid,
                                "name": d["filename"],
                                "size": d.get("size"),
                                "meta": {"content_type": d["content_type"]},
                            }
                        )
                except Exception:  # noqa: BLE001
                    pass
            await status("Exports ready", done=True)
            if files and __event_emitter__:
                await __event_emitter__({"type": "files", "data": {"files": files}})
            link = self._wb(f"/exports/{pkg}")
            await say("**Exports**\n" + "\n".join(lines) + (f"\n\n[Export center ↗]({link})" if link else ""))
            return visible()
        if sub == "rerun":
            chosen = None
            if __event_call__:
                try:
                    chosen = await __event_call__(
                        {
                            "type": "input",
                            "data": {
                                "title": "Re-run with…",
                                "message": "role=model[@lane] pairs, e.g. writer=anthropic.claude-sonnet-5@mantle_messages planner=global.amazon.nova-2-lite-v1:0 (blank = same models)",  # noqa: E501
                                "placeholder": "writer=…",
                            },
                        }
                    )
                except Exception:  # noqa: BLE001
                    chosen = None
            models: dict[str, Any] = {}
            for tok in str(chosen or "").split():
                role, _, ref = tok.partition("=")
                if role in ("router", "clarifier", "shallow", "planner", "researcher", "writer") and ref:
                    mid, _, lane = ref.partition("@")
                    models[role] = {
                        "model_id": mid,
                        "lane": lane or ("mantle_messages" if mid.startswith("anthropic.") else "converse"),
                    }
            payload = {"op": "packages.rerun", "job_id": pkg, "client_request_id": f"rerun-{uuid.uuid4().hex}"}
            if models:
                payload["models"] = models
            try:
                evs = await self._invoke(bearer, payload, 120)
            except Exception as e:  # noqa: BLE001
                await say(f"_Re-run failed: {e.__class__.__name__}_")
                return visible()
            err = next((e for e in evs if e.get("type") == "error"), None)
            acc = next((e for e in evs if e.get("type") == "job.accepted"), None)
            if err or not acc:
                await say(f"**Re-run refused:** {((err or {}).get('data') or {}).get('error', {}).get('message', 'no job id')}")
                return visible()
            new_job = acc["job_id"]
            roles = (acc.get("data") or {}).get("models") or {}
            writer = (roles.get("writer") or {}).get("human_name") or (roles.get("writer") or {}).get("model_id") or "default"
            link = self._wb(f"/p/{new_job}")
            await say(
                f"**Re-run started** `{new_job}` (writer: {writer}; lineage: re-run of `{pkg[:12]}…`). "
                + (f"[Watch progress in the Workbench ↗]({link})" if link else "Use `/library` to watch progress.")
                + f"\n\n_aiq-package:{new_job}_"
            )
            return visible()
        if sub == "compare":
            evs = await self._invoke(bearer, {"op": "packages.get", "job_id": pkg}, 60)
            man = next(((e.get("data") or {}).get("manifest") for e in evs if e.get("type") == "package"), None) or {}
            other = (man.get("lineage") or {}).get("parent_package_id")
            if not other:
                await say("_Nothing to compare with yet — re-run this package first, then Compare._")
                return visible()
            evs = await self._invoke(bearer, {"op": "packages.compare", "job_id": pkg, "other_job_id": other}, 90)
            cmp_ = next(((e.get("data") or {}) for e in evs if e.get("type") == "comparison"), None)
            if not cmp_:
                await say("_Compare failed._")
                return visible()
            run = cmp_.get("run") or {}
            src = cmp_.get("sources") or {}
            link = self._wb(f"/compare/{pkg}/{other}")
            await say(
                f"**Compare** `{pkg[:12]}…` (A) ⇄ `{other[:12]}…` (B): cost ${run.get('cost_a') or 0:.3f} vs ${run.get('cost_b') or 0:.3f}; "  # noqa: E501
                f"citations verified {run.get('citations_a')} vs {run.get('citations_b')}; sources shared {len(src.get('shared', []))}, "  # noqa: E501
                f"A-only {len(src.get('only_a', []))}, B-only {len(src.get('only_b', []))}."
                + (f" [Side by side ↗]({link})" if link else "")
            )
            return visible()
        return None

    async def _attach(self, url: str, name: str, content_type: str, __user__: dict) -> str | None:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    return visible()
                data = await resp.read()
        if len(data) > 25 * 1024 * 1024:
            return visible()
        fid = str(uuid.uuid4())
        _, path = Storage.upload_file(io.BytesIO(data), f"{fid}_{name}", {"OpenWebUI-User-Id": __user__.get("id", "")})
        f = Files.insert_new_file(
            __user__["id"],
            FileForm(
                id=fid,
                filename=name,
                path=path,
                data={},
                meta={"name": name, "content_type": content_type, "size": len(data), "source": "aiq_actions"},
            ),
        )
        if asyncio.iscoroutine(f):
            f = await f
        return fid if f else None

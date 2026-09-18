# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
title: AI-Q Research on AgentCore
id: aiq_agentcore
description: AI-Q research on AgentCore — cited answers, deep research, Research Packages, exports, model roles, Workbench.
version: 0.3.0
license: MIT-0
"""

# This manifold pipe exposes AI-Q's research modes as Open WebUI models:
#   aiq_agentcore.auto          AI-Q Research (auto route)
#   aiq_agentcore.shallow       AI-Q Quick answer (cited)
#   aiq_agentcore.deep          AI-Q Deep research
#   aiq_agentcore.deep_clarify  AI-Q Deep research (clarify first)
#
# Identity: the signed-in user's own Cognito access token (Open WebUI OAuth session)
# is sent as the bearer to the AgentCore Runtime, whose CUSTOM_JWT authorizer
# trusts this deployment's Cognito app client. The runtime derives the tenant
# from the verified token; the pipe never sends a user id.
#
# Long runs: deep research is submitted as a job; this pipe then tails the
# job's durable event journal from a cursor. Open WebUI keeps this generator
# running after the browser disconnects; if the pipe itself is restarted the
# next user message resumes from the job id embedded in the last assistant
# message ("job:" footer) — nothing is re-run.
#
# Cancel: Open WebUI's Stop cancels this task; the pipe forwards a cancel to the
# runtime (cooperative) and re-raises so Open WebUI finalises the message.

__pipe_version__ = "0.3.0"

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncIterator

import aiohttp
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODES = [
    (
        "auto",
        "AI-Q Research (auto)",
        "Routes between a quick cited answer and deep research; may ask a clarifying question first.",
    ),
    ("shallow", "AI-Q Quick answer", "Fast, bounded web research with citations."),
    ("deep", "AI-Q Deep research", "Multi-phase research (planner, researchers, writer) producing a cited report."),
    (
        "deep_clarify",
        "AI-Q Deep research (clarify first)",
        "Asks clarifying questions, then runs deep research after you confirm.",
    ),
]
JOB_FOOTER_RE = re.compile(r"aiq-job:(job_[a-f0-9]{32}):(clarifying|completed|cancelled|running|failed)")
COMMANDS = {
    "/cancel",
    "/status",
    "/collections",
    "/help",
    "/library",
    "/models",
    "/model",
    "/export",
    "/rerun",
    "/compare",
    "/eval",
    "/package",
    "/open",
}
ROLES = ("router", "clarifier", "shallow", "planner", "researcher", "writer")
PKG_FOOTER_RE = re.compile(r"aiq-package:(job_[a-f0-9]{32})")
TERMINAL = {"completed", "cancelled"}


class Pipe:
    class Valves(BaseModel):
        RUNTIME_ARN: str = Field(
            default=os.environ.get("AIQ_RUNTIME_ARN", ""), description="AgentCore Runtime ARN for AI-Q (aiq_<runId>)."
        )
        REGION: str = Field(default=os.environ.get("AWS_REGION", "us-east-1"), description="AgentCore data-plane region.")
        RUN_ID: str = Field(default="", description="Run id shown in footers (informational).")
        REQUEST_TIMEOUT_S: int = Field(default=3300, description="Max seconds per streaming request (runtime cap is 3600).")
        TAIL_RETRIES: int = Field(default=20, description="Reconnect attempts while tailing a job.")
        SHOW_TOOL_EVENTS: bool = Field(default=True, description="Show tool calls as status lines.")
        DEFAULT_COLLECTION_PREFIX: str = Field(default="chat", description="Collection name prefix for uploaded files.")
        APPROVAL_KEYWORDS: str = Field(
            default="approve,yes,go,proceed,start", description="Replies that approve a pending clarification/plan."
        )
        WORKBENCH_URL: str = Field(
            default=os.environ.get("AIQ_WORKBENCH_URL", ""),
            description="Research Workbench base URL (same Cognito pool); used for deep links on package cards.",
        )
        ATTACH_EXPORTS: bool = Field(
            default=True, description="Attach export files to the chat message (Open WebUI Files API) in addition to the link."
        )

    class UserValves(BaseModel):
        SOURCES: str = Field(
            default="web_search,documents",
            description="Data sources AI-Q may use, comma-separated: web_search, documents, news, prediction_markets.",
        )
        PAGE_FETCH: bool = Field(
            default=True, description="Allow the researchers to open web pages (AgentCore Browser) for depth."
        )
        REPORT_FOLLOWUPS: bool = Field(
            default=True, description="Let follow-up questions in this chat refer to the last completed report."
        )
        MODEL_WRITER: str = Field(
            default="",
            description="Writer model for your sessions (id[@lane]); blank = your Workbench default or the deployment default. Validated against the capability matrix.",  # noqa: E501
        )
        MODEL_PLANNER: str = Field(default="", description="Planner/orchestrator model (id[@lane]); blank = default.")
        MODEL_RESEARCHER: str = Field(default="", description="Researcher model (id[@lane]); blank = default.")
        MODEL_SHALLOW: str = Field(default="", description="Quick-answer model (id[@lane]); blank = default.")
        MODEL_CLARIFIER: str = Field(default="", description="Clarifier model (id[@lane]); blank = default.")

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _user_sources(__user__: dict) -> tuple[list[str] | None, bool, bool]:
        uv = (__user__ or {}).get("valves")
        get = (
            (lambda k, d: getattr(uv, k, d))
            if uv is not None and not isinstance(uv, dict)
            else (lambda k, d: (uv or {}).get(k, d))
        )
        raw = str(get("SOURCES", "web_search,documents") or "")
        allowed = {"web_search", "documents", "news", "prediction_markets"}
        sources = [x.strip() for x in raw.split(",") if x.strip() in allowed]
        return (sources or None), bool(get("PAGE_FETCH", True)), bool(get("REPORT_FOLLOWUPS", True))

    # ------------------------------------------------------------------ models --
    def pipes(self) -> list[dict]:
        return [{"id": mid, "name": name} for mid, name, _ in MODES]

    # ------------------------------------------------------------------ helpers --
    def _endpoint(self) -> str:
        arn = self.valves.RUNTIME_ARN.strip()
        if not arn:
            raise RuntimeError("RUNTIME_ARN valve is not set")
        from urllib.parse import quote

        return f"https://bedrock-agentcore.{self.valves.REGION}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"  # noqa: E501

    @staticmethod
    def _session_id(kind: str, chat_id: str, user_id: str) -> str:
        base = f"aiq-{kind}-{chat_id}-{user_id}".replace(" ", "")
        return (base + "-" + "0" * 40)[:64] if len(base) < 33 else base[:128]

    async def _bearer(self, __user__: dict, __oauth_token__, __request__) -> str | None:
        tok = (__oauth_token__ or {}).get("access_token") if isinstance(__oauth_token__, dict) else None
        if tok:
            return tok
        try:  # fallback: Open WebUI OAuth session manager (same approach as the Claude pipe)
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
        except Exception as e:  # noqa: BLE001
            log.warning("aiq_agentcore: no user OAuth token (%s)", e)
        return None

    async def _invoke(self, bearer: str, session_id: str, payload: dict, timeout_s: int | None = None) -> AsyncIterator[dict]:
        """POST /invocations and yield decoded JSON events (SSE `data:` lines or raw JSON lines)."""
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        }
        timeout = aiohttp.ClientTimeout(total=timeout_s or self.valves.REQUEST_TIMEOUT_S, connect=20, sock_read=600)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(self._endpoint(), data=json.dumps(payload).encode(), headers=headers) as resp:
                if resp.status != 200:
                    text = (await resp.text())[:500]
                    raise RuntimeError(f"AgentCore runtime HTTP {resp.status}: {text}")
                buf = b""
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        s = line.decode("utf-8", "ignore").strip()
                        if not s or s.startswith(":"):
                            continue
                        if s.startswith("data:"):
                            s = s[5:].strip()
                        if not s:
                            continue
                        try:
                            obj = json.loads(s)
                        except json.JSONDecodeError:
                            # BedrockAgentCoreApp wraps yielded strings as JSON strings; unwrap.
                            try:
                                inner = json.loads(json.loads(f'"{s}"'))
                                obj = inner
                            except Exception:
                                continue
                        if isinstance(obj, str):
                            try:
                                obj = json.loads(obj)
                            except json.JSONDecodeError:
                                continue
                        if isinstance(obj, dict):
                            yield obj
                if buf.strip():
                    try:
                        obj = json.loads(buf.decode("utf-8", "ignore").strip().removeprefix("data:").strip())
                        if isinstance(obj, str):
                            obj = json.loads(obj)
                        if isinstance(obj, dict):
                            yield obj
                    except Exception:
                        pass

    @staticmethod
    def _parse_model_ref(text: str) -> dict | None:
        """`model_id[@lane]` → {model_id, lane}; lanes: converse (default), mantle_chat, mantle_messages."""
        text = (text or "").strip()
        if not text:
            return None
        mid, _, lane = text.partition("@")
        lane = lane.strip() or (
            "mantle_messages" if mid.startswith("anthropic.") and not mid.startswith(("us.", "global.")) else "converse"
        )
        return {"model_id": mid.strip(), "lane": lane}

    def _models_from_valves(self, __user__: dict) -> dict:
        uv = (__user__ or {}).get("valves")
        get = (lambda k: getattr(uv, k, "")) if uv is not None and not isinstance(uv, dict) else (lambda k: (uv or {}).get(k, ""))
        out = {}
        for role in ("writer", "planner", "researcher", "shallow", "clarifier"):
            ref = self._parse_model_ref(str(get(f"MODEL_{role.upper()}") or ""))
            if ref:
                out[role] = ref
        return out

    @staticmethod
    def _models_from_chat(messages: list[dict]) -> dict:
        """`/model writer=<id>[@lane] planner=<id>` lines earlier in this chat set the session's roles (latest wins)."""
        out: dict = {}
        for m in messages:
            if m.get("role") != "user" or not isinstance(m.get("content"), str):
                continue
            text = m["content"].strip()
            if not text.lower().startswith("/model"):
                continue
            if text.lower().strip() in ("/model reset", "/model clear"):
                out = {}
                continue
            for tok in text.split()[1:]:
                role, _, ref = tok.partition("=")
                if role in ROLES and ref:
                    out[role] = Pipe._parse_model_ref(ref)
        return out

    def _workbench_link(self, path: str) -> str | None:
        base = (self.valves.WORKBENCH_URL or "").rstrip("/")
        return f"{base}{path}" if base else None

    def _package_card(self, job_id: str, summary: dict | None, state: dict) -> str:
        """The result card every research answer ends with (11-architecture.md §2): counts, cost, models, links, commands."""
        s = summary or {}
        counts = s.get("counts") or {}
        models = s.get("models") or {}
        writer = (models.get("writer") or {}).get("human_name") or (models.get("writer") or {}).get("model_id") or "—"
        planner = (models.get("planner") or {}).get("human_name") or (models.get("planner") or {}).get("model_id") or "—"
        cost = s.get("cost_usd")
        cost_txt = (
            f"${cost:.2f}"
            if isinstance(cost, (int, float)) and cost >= 0.005
            else (f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a")
        )
        secs = s.get("duration_seconds")
        dur = f" · {int(secs // 60)}m {int(secs % 60):02d}s" if isinstance(secs, (int, float)) else ""
        wb = self._workbench_link(f"/p/{job_id}")
        links = []
        if wb:
            links.append(f"[Open in Workbench ↗]({wb})")
            links.append(f"[Export ⤓]({self._workbench_link(f'/exports/{job_id}')})")
            links.append(f"[Library ▦]({self._workbench_link('/')})")
        lines = [
            "",
            "---",
            f"**Research package** `{job_id[:12]}…` — {s.get('status', state.get('terminal') or 'completed')}{dur}",
            f"Sources **{counts.get('sources', state.get('sources', 0))}** · citations verified **{counts.get('citations_verified', '?')}** / "  # noqa: E501
            f"unverified **{counts.get('citations_unverified', '?')}** · artifacts **{counts.get('artifacts', 0)}** · cost **{cost_txt}**",  # noqa: E501
            f"Models: planner *{planner}* · writer *{writer}*",
            (" · ".join(links) if links else ""),
            "_In chat: `/export pdf` · `/rerun writer=<model>` · `/compare` · `/library` · `/models`_",
        ]
        return "\n".join(x for x in lines if x is not None)

    @staticmethod
    def _pending_job(messages: list[dict]) -> tuple[str, str] | None:
        """Find the most recent assistant footer `aiq-job:<id>:<state>`."""
        for m in reversed(messages):
            if m.get("role") != "assistant":
                continue
            content = m.get("content") if isinstance(m.get("content"), str) else ""
            found = JOB_FOOTER_RE.findall(content or "")
            if found:
                return found[-1][0], found[-1][1]
        return None

    @staticmethod
    def _footer(job_id: str, state: str, extra: str = "") -> str:
        # Rendered by Open WebUI's markdown as a small italic line; parsed back by JOB_FOOTER_RE on the next turn.
        return f"\n\n_aiq-job:{job_id}:{state} · aiq-package:{job_id}{(' · ' + extra) if extra else ''}_"

    @staticmethod
    def _source_event(src: dict) -> dict:
        url = src.get("url") or ""
        name = src.get("title") or url or src.get("document_key") or src.get("source_id")
        meta = {"source": url or f"document://{src.get('document_key') or src.get('source_id')}", "name": name}
        if src.get("retrieved_at"):
            meta["date_accessed"] = src["retrieved_at"]
        return {
            "type": "source",
            "data": {
                "source": {"name": name, "url": url} if url else {"name": name},
                "document": [src.get("snippet") or name],
                "metadata": [meta],
            },
        }

    async def _read_files(self, files: list[dict]) -> list[dict]:
        """Read attached Open WebUI files (server-side) into base64 documents for ingestion."""
        out = []
        for item in files or []:
            if item.get("type") not in (None, "file") or not item.get("id"):
                continue
            try:
                from open_webui.models.files import Files
                from open_webui.storage.provider import Storage

                f = Files.get_file_by_id(item["id"])
                if f is None:
                    continue
                name = (f.meta or {}).get("name") or f.filename or item.get("name") or "document"
                ctype = (f.meta or {}).get("content_type") or "application/octet-stream"
                data = None
                try:
                    path = Storage.get_file(f.path)
                    with open(path, "rb") as fh:
                        data = fh.read()
                except Exception:  # noqa: BLE001 — fall back to extracted text
                    text = (f.data or {}).get("content")
                    if text:
                        data, ctype, name = text.encode("utf-8"), "text/plain", name + ".txt"
                if not data:
                    continue
                out.append(
                    {"name": name, "content_type": ctype, "content_b64": base64.b64encode(data).decode(), "size_bytes": len(data)}
                )
            except Exception as e:  # noqa: BLE001
                log.warning("aiq_agentcore: could not read file %s (%s)", item.get("id"), e)
        return out

    # ------------------------------------------------------------------ main --
    async def pipe(
        self,
        body: dict,
        __user__: dict | None = None,
        __metadata__: dict | None = None,
        __event_emitter__=None,
        __oauth_token__=None,
        __files__=None,
        __request__=None,
    ):
        __user__ = __user__ or {}
        __metadata__ = __metadata__ or {}
        if __metadata__.get("task"):  # title/tags/follow-up generation: cheap deterministic reply
            msgs = body.get("messages") or []
            last = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "")
            return str(last)[:60] if isinstance(last, str) else "AI-Q research"

        model_id = body.get("model", "")
        mode = model_id.split(".", 1)[1] if "." in model_id else "auto"
        if mode not in {m for m, _, _ in MODES}:
            mode = "auto"
        chat_id = __metadata__.get("chat_id") or f"nochat-{uuid.uuid4().hex[:8]}"
        message_id = __metadata__.get("message_id") or uuid.uuid4().hex
        user_id = __user__.get("id", "anon")
        messages = [
            {
                "role": m.get("role", "user"),
                "content": m.get("content")
                if isinstance(m.get("content"), str)
                else " ".join(b.get("text", "") for b in (m.get("content") or []) if isinstance(b, dict)),
            }
            for m in body.get("messages") or []
            if m.get("role") in ("user", "assistant", "system")
        ]
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "").strip()

        bearer = await self._bearer(__user__, __oauth_token__, __request__)
        if not bearer:
            return (
                "AI-Q on AgentCore needs your signed-in SSO session (Cognito access token) to identify you. "
                "Please sign in with SSO and try again."
            )

        emitter = __event_emitter__

        async def status(desc: str, done: bool = False, hidden: bool = False):
            if emitter:
                await emitter({"type": "status", "data": {"description": desc, "done": done, "hidden": hidden}})

        async def source(src: dict):
            if emitter:
                await emitter(self._source_event(src))

        sess_job = self._session_id("job", message_id, user_id)  # one microVM per job (ADR-20), never shared across a chat
        sess_tail = self._session_id("tail", chat_id, user_id)
        sess_ctl = self._session_id("ctl", chat_id, user_id)

        # ---- slash commands -------------------------------------------------
        cmd = question.split()[0].lower() if question else ""
        pending = self._pending_job(messages)
        session_models = {**self._models_from_valves(__user__), **self._models_from_chat(messages)}
        if cmd in COMMANDS:
            if cmd == "/help":
                wb = self._workbench_link("/")
                return (
                    "**AI-Q commands**\n\n"
                    "- `/status` · `/cancel` — this chat's last research job\n"
                    "- `/library [n]` — your latest research packages\n"
                    "- `/package` — the card of this chat's last package\n"
                    "- `/models` — models you may choose, with prices and role scores (from the live capability matrix)\n"
                    "- `/model writer=<id>[@lane] planner=<id> …` — set models for this chat (validated); `/model reset`\n"
                    "- `/export pdf|docx|pptx|html|md|json|csv|bibtex|ris|csl|zip` — export the last package (attached + link)\n"
                    "- `/rerun [writer=<id> …]` — re-run the last package with different models (lineage kept)\n"
                    "- `/compare [job_id]` — compare the last package with its parent or another package\n"
                    "- `/eval [writer=<id> …]` — run the evaluation set with a model configuration\n"
                    "- `/collections` — your document collections\n\n"
                    + (f"Workbench: {wb}\n\n" if wb else "")
                    + "Attach files to add them to this chat's collection; reply **approve** to a clarifying question to start deep "  # noqa: E501
                    "research, or describe changes."
                )
            if cmd == "/model":
                if len(question.split()) == 1:
                    return self._render_models_in_use(session_models)
                if question.strip().lower() in ("/model reset", "/model clear"):
                    return "Session model selection cleared — deployment defaults (or your Workbench preferences) apply from the next message."  # noqa: E501
                chosen = {**session_models}
                async for ev in self._invoke(bearer, sess_ctl, {"op": "models.validate", "models": chosen}, 60):
                    d = ev.get("data") or {}
                    if ev.get("type") == "error":
                        return f"Model selection refused: {d.get('error', {}).get('message', 'unknown')}"
                    if not d.get("ok"):
                        probs = "\n".join(f"- **{p['role']}** `{p['model_id']}` — {p['reason']}" for p in d.get("problems", []))
                        return (
                            f"**Model selection refused** (nothing changed):\n\n{probs}\n\nUse `/models` to see what is offered."
                        )
                    est = d.get("estimate_usd") or {}
                    lines = [
                        f"- **{r}** → *{v.get('human_name') or v['model_id']}* (`{v['model_id']}`, {v.get('lane')}, {v.get('source')})"  # noqa: E501
                        for r, v in (d.get("resolved") or {}).items()
                    ]
                    est_txt = (
                        f"\n\nEstimated deep run: **${est['low']:.2f}–${est['high']:.2f}** ({est.get('basis')})"
                        if est.get("low") is not None
                        else ""
                    )
                    return "**Models for this chat** (applied to every following message):\n\n" + "\n".join(lines) + est_txt
                return "Could not validate the selection."
            if cmd == "/models":
                return await self._render_models_catalog(bearer, sess_ctl, question)
            if cmd == "/library":
                n = 10
                parts = question.split()
                if len(parts) > 1 and parts[1].isdigit():
                    n = max(1, min(50, int(parts[1])))
                rows = []
                async for ev in self._invoke(bearer, sess_ctl, {"op": "packages.list", "limit": n}, 60):
                    if ev.get("type") == "error":
                        return f"Error: {(ev.get('data') or {}).get('error', {}).get('message')}"
                    rows = (ev.get("data") or {}).get("items") or []
                if not rows:
                    return "Your library is empty — ask a research question to create your first package."
                wb = self._workbench_link("/")
                out = [
                    "**Your research library** (newest first)",
                    "",
                    "| Package | Status | Mode | Sources | Writer | Cost | Created |",
                    "|---|---|---|---|---|---|---|",
                ]
                for r in rows:
                    title = (r.get("title") or r.get("question") or "")[:70].replace("|", "/")
                    link = self._workbench_link(f"/p/{r['job_id']}")
                    name = f"[{title}]({link})" if link else title
                    w = ((r.get("models") or {}).get("writer") or {}).get("human_name") or "—"
                    c = r.get("cost_usd")
                    out.append(
                        f"| {name} | {r.get('status')} | {r.get('mode')} | {(r.get('counts') or {}).get('sources', '—')} | {w} | "
                        f"{('$%.2f' % c) if isinstance(c, (int, float)) else '—'} | {str(r.get('created_at', ''))[:16].replace('T', ' ')} |"  # noqa: E501
                    )
                if wb:
                    out.append(f"\n[Open the Workbench ↗]({wb})")
                return "\n".join(out)
            if cmd in ("/package", "/open"):
                pkg = self._last_package(messages)
                if not pkg:
                    return "No research package in this chat yet."
                summ = None
                async for ev in self._invoke(bearer, sess_ctl, {"op": "packages.get", "job_id": pkg}, 60):
                    if ev.get("type") == "package":
                        summ = (ev.get("data") or {}).get("summary")
                    elif ev.get("type") == "error":
                        return f"Error: {(ev.get('data') or {}).get('error', {}).get('message')}"
                return self._package_card(pkg, summ, {}) + self._footer(pkg, (summ or {}).get("status", "completed"))
            if cmd == "/export":
                pkg = self._last_package(messages)
                fmt = question.split()[1].lower() if len(question.split()) > 1 else "pdf"
                if not pkg:
                    return "No research package in this chat yet — run a research question first."
                return self._export(bearer, sess_ctl, pkg, fmt, emitter, __user__, __request__)
            if cmd == "/rerun":
                pkg = self._last_package(messages)
                if not pkg:
                    return "No research package in this chat yet."
                overrides = dict(session_models)
                for tok in question.split()[1:]:
                    role, _, ref = tok.partition("=")
                    if role in ROLES and ref:
                        overrides[role] = self._parse_model_ref(ref)
                payload = {
                    "op": "packages.rerun",
                    "job_id": pkg,
                    "client_request_id": f"rerun-{message_id}",
                    "conversation_id": chat_id,
                }
                if overrides:
                    payload["models"] = overrides
                new_job = None
                async for ev in self._invoke(bearer, sess_job, payload, 120):
                    if ev.get("type") == "error":
                        return f"Re-run refused: {(ev.get('data') or {}).get('error', {}).get('message')}"
                    if ev.get("type") == "job.accepted":
                        new_job = ev.get("job_id")
                if not new_job:
                    return "AI-Q did not return a job id."
                await status(f"Re-running package {pkg[:12]}… as {new_job[:12]}… (lineage: rerun)")
                return self._tail(bearer, sess_tail, sess_ctl, new_job, 0, status, source, mode)
            if cmd == "/compare":
                pkg = self._last_package(messages)
                if not pkg:
                    return "No research package in this chat yet."
                parts = question.split()
                other = parts[1] if len(parts) > 1 and parts[1].startswith("job_") else None
                if other is None:  # default: compare with the parent (the package this one re-ran)
                    async for ev in self._invoke(bearer, sess_ctl, {"op": "packages.get", "job_id": pkg}, 60):
                        if ev.get("type") == "package":
                            other = (((ev.get("data") or {}).get("manifest") or {}).get("lineage") or {}).get("parent_package_id")
                    if not other:
                        return "Nothing to compare with: give a package id (`/compare job_…`) or re-run this package first."
                return await self._render_compare(bearer, sess_ctl, pkg, other)
            if cmd == "/eval":
                overrides = dict(session_models)
                for tok in question.split()[1:]:
                    role, _, ref = tok.partition("=")
                    if role in ROLES and ref:
                        overrides[role] = self._parse_model_ref(ref)
                payload = {"op": "eval", "conversation_id": chat_id}
                if overrides:
                    payload["models"] = overrides
                async for ev in self._invoke(bearer, sess_job, payload, 120):
                    d = ev.get("data") or {}
                    if ev.get("type") == "error":
                        return f"Evaluation refused: {d.get('error', {}).get('message')}"
                    if ev.get("type") == "eval.started":
                        wb = self._workbench_link("/lab")
                        models_txt = ", ".join(
                            f"{r}: {v.get('human_name') or v.get('model_id')}" for r, v in (d.get("models") or {}).items()
                        )
                        return (
                            f"**Evaluation started** `{d.get('eval_id')}` — {len(d.get('job_ids') or [])} questions running as packages "  # noqa: E501
                            f"(lineage: eval).\n\nModels: {models_txt}\n\n"
                            + (f"Leaderboard: [Model Lab ↗]({wb})" if wb else "Use `/library` to watch progress.")
                            + "\n\n_Judge: deterministic metrics only (citations verified, sources, cost, latency, keyword hits) — no LLM judge in this build._"  # noqa: E501
                        )
                return "Evaluation did not start."
            if cmd == "/collections":
                lines = []
                async for ev in self._invoke(bearer, sess_ctl, {"op": "collections"}, 60):
                    for c in (ev.get("data") or {}).get("collections", []):
                        lines.append(
                            f"- **{c['name']}** — {len(c['documents'])} document(s): "
                            + ", ".join(d["name"] for d in c["documents"][:10])
                        )
                return "**Your document collections**\n\n" + (
                    "\n".join(lines) if lines else "_none yet — attach a file to create one_"
                )
            if not pending:
                return "No AI-Q job is associated with this chat yet."
            job_id, _ = pending
            op = "cancel" if cmd == "/cancel" else "status"
            out = []
            async for ev in self._invoke(bearer, sess_ctl, {"op": op, "job_id": job_id}, 60):
                d = ev.get("data") or {}
                if ev.get("type") == "error":
                    return f"Error: {d.get('error', {}).get('message', 'unknown')}"
                out.append(
                    f"Job `{job_id}` — status **{d.get('status')}**, events {d.get('last_seq')}, mode {d.get('mode')}"
                    + (", cancel requested" if d.get("cancel_requested") else "")
                )
            return "\n".join(out) + self._footer(job_id, "cancelled" if op == "cancel" else str(d.get("status")))

        # ---- documents: attach → ingest into this chat's collection ---------
        collection = None
        docs = await self._read_files(__files__ or __metadata__.get("files") or [])
        if docs or any(i.get("type") == "file" for i in (__files__ or [])):
            collection = f"{self.valves.DEFAULT_COLLECTION_PREFIX}-{chat_id[:12]}"
        if docs:
            await status(f"Adding {len(docs)} document(s) to collection {collection}…")
            sync = None
            async for ev in self._invoke(bearer, sess_ctl, {"op": "ingest", "collection": collection, "documents": docs}, 300):
                t = ev.get("type", "")
                d = ev.get("data") or {}
                if t == "document.stored":
                    await status(f"Stored {d.get('name')} ({d.get('size')} bytes)")
                elif t == "document.rejected":
                    await status(f"Rejected {d.get('name')}: {d.get('reason')}", done=True)
                elif t == "collection.sync":
                    sync = d
                elif t == "error":
                    return f"Document ingestion failed: {d.get('error', {}).get('message')}"
            if sync:
                await status(
                    f"Indexing {sync.get('documents')} document(s) (ingestion job {sync.get('ingestion_job_id', '?')[:8]}…) — "
                    "retrieval becomes available when indexing completes.",
                    done=True,
                )

        # ---- resume a pending clarification with this reply -----------------
        if pending and pending[1] == "clarifying" and question:
            job_id, _ = pending
            approve_words = {w.strip().lower() for w in self.valves.APPROVAL_KEYWORDS.split(",")}
            lowered = question.lower().strip(" .!")
            approval = "reject" if lowered in {"cancel", "reject", "stop", "no"} else "approve"
            payload = {
                "op": "approve",
                "job_id": job_id,
                "approval": approval,
                "messages": messages,
                "revision": None if lowered in approve_words else question,
                "collection": collection,
            }
            resume_after = 0
            async for ev in self._invoke(bearer, sess_job, payload, 120):
                if ev.get("type") == "error":
                    return f"Could not resume job `{job_id}`: {(ev.get('data') or {}).get('error', {}).get('message')}"
                if ev.get("type") == "cancelled":
                    return f"Research job `{job_id}` was cancelled." + self._footer(job_id, "cancelled")
                if ev.get("type") == "job.accepted" and isinstance(ev.get("seq"), int):
                    resume_after = ev["seq"]  # tail only what the resumed run produces, not the replayed pause
            await status("Clarification received — resuming deep research…")
            return self._tail(bearer, sess_tail, sess_ctl, job_id, resume_after, status, source, mode)

        if not question:
            return "Ask a research question, or attach documents to build a collection."

        # ---- run -------------------------------------------------------------
        sources, page_fetch, followups = self._user_sources(__user__)
        if sources is not None and not page_fetch and "web_search" in sources:
            sources = [x for x in sources if x != "web_search"] + ["web_search_no_pages"]
        base = {
            "mode": mode,
            "messages": messages,
            "client_request_id": message_id,
            "conversation_id": chat_id,
            "collection": collection,
            "data_sources": sources,
        }
        if session_models:
            base["models"] = session_models
        if followups and pending and pending[1] == "completed" and mode in ("auto", "shallow"):
            base["active_report_job_id"] = pending[0]  # follow-up over the last completed report in this chat
        if mode in ("deep", "deep_clarify"):
            await status("Submitting deep research job…")
            job_id = None
            async for ev in self._invoke(bearer, sess_job, {"op": "submit", **base}, 120):
                if ev.get("type") == "error":
                    return f"AI-Q could not start: {(ev.get('data') or {}).get('error', {}).get('message', 'unknown error')}"
                if ev.get("type") == "job.accepted":
                    job_id = ev.get("job_id")
            if not job_id:
                return "AI-Q did not return a job id."
            return self._tail(bearer, sess_tail, sess_ctl, job_id, 0, status, source, mode)
        # shallow / auto: synchronous streamed turn (journaled; replayable on retry)
        return self._chat(bearer, sess_job, sess_ctl, {"op": "chat", **base}, status, source, mode)

    # ------------------------------------------------------------------ streams --
    async def _render(self, ev: dict, state: dict, status, source) -> AsyncIterator[str]:
        t = ev.get("type", "")
        d = ev.get("data") or {}
        if ev.get("job_id"):
            state["job_id"] = ev["job_id"]
        if isinstance(ev.get("seq"), int) and ev["seq"] > state.get("cursor", 0):
            state["cursor"] = ev["seq"]
        if t == "job.accepted":
            if d.get("replayed"):
                await status("Reconnected to the existing job — replaying progress…")
        elif t == "route":
            depth = d.get("depth") or d.get("mode") or "auto"
            extra = " (clarifier on)" if d.get("clarifier") else ""
            await status(f"Routing: {depth} research{extra}" + (f" — {d['reason']}" if d.get("reason") else ""))
            models = d.get("models") or {}
            if models:  # ADR-23: the models in use are always visible in the transcript
                shown = []
                for role in ("planner", "researcher", "writer") if depth == "deep" else ("shallow",):
                    v = models.get(role) or {}
                    if v.get("model_id"):
                        tag = "" if v.get("source") in (None, "deploy_default") else " ✎"
                        shown.append(f"{role} = {v.get('human_name') or v['model_id']}{tag}")
                if shown:
                    await status("Models: " + " · ".join(shown), done=True)
        elif t == "package":
            state["package"] = d
            state["job_id"] = d.get("job_id") or state.get("job_id")
        elif t == "status":
            await status(str(d.get("description", ""))[:300], done=bool(d.get("done")))
        elif t == "tool.call" and self.valves.SHOW_TOOL_EVENTS:
            inp = d.get("input") or {}
            q = inp.get("query") or inp.get("url") or ""
            await status(f"{d.get('tool', 'tool')}: {str(q)[:120]}")
        elif t == "source":
            await source(d)
            state["sources"] = state.get("sources", 0) + 1
        elif t == "plan":
            steps = d.get("steps") or []
            yield "**Research plan**\n\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)) + "\n\n"
        elif t == "clarification":
            state["clarifying"] = True
            yield (
                d.get("question") or "Could you clarify your request?"
            ) + "\n\n_Reply with your answer, **approve** to proceed as-is, or **cancel**._"
        elif t == "plan.approval_required":
            state["clarifying"] = True
            yield "\n" + (d.get("prompt") or "Reply **approve** to start deep research, or describe changes.")
        elif t == "delta":
            yield d.get("text", "")
        elif t == "report":
            text = d.get("text") or ""
            if text and not state.get("streamed_text"):
                yield text
            state["report"] = True
        elif t == "citations":
            v, u = d.get("verified", 0), d.get("unverified", 0)
            await status(f"Citations verified: {v} ok, {u} unverified", done=True)
        elif t == "usage":
            state["usage"] = d
        elif t == "tool.result" and d.get("tool") == "agentcore_fetch_page":
            if d.get("error"):
                await status(f"Page skipped ({d.get('error')}): {str(d.get('url', ''))[:90]}")
            else:
                await status(f"Read page ({d.get('chars', 0)} chars): {str(d.get('url', ''))[:90]}")
        elif t == "guardrail":
            act = d.get("action")
            if act in ("GUARDRAIL_INTERVENED", "MODIFIED", "ERROR"):
                label = act.lower().replace("_", " ")
                if d.get("enforced") is False:
                    label += f" ({d.get('mode', 'audit')} mode, not enforced)"
                reasons = ", ".join(str(r) for r in (d.get("reasons") or [])[:3])
                await status(
                    f"Content policy ({d.get('source', '').lower()}): {label}" + (f" · {reasons}" if reasons else ""), done=True
                )
        elif t == "artifact":
            name = d.get("name") or "artifact"
            if d.get("markdown"):
                yield "\n\n" + d["markdown"] + "\n\n"
            else:
                await status(f"Artifact captured: {name}")
        elif t == "warning":
            await status(f"Warning: {d.get('message', '')[:200]}")
        elif t == "error":
            err = d.get("error") or {}
            yield f"\n\n**AI-Q error** ({err.get('code', 'error')}): {err.get('message', 'unknown')}" + (
                "\n\n_You can retry; nothing is duplicated._" if err.get("retryable") else ""
            )
            if d.get("terminal"):
                state["terminal"] = "failed"
        elif t == "cancelled":
            yield "\n\n_Research cancelled._"
            state["terminal"] = "cancelled"
        elif t == "completed":
            state["terminal"] = "completed"
            await status("Done", done=True, hidden=True)

    def _usage_line(self, state: dict) -> str:
        u = state.get("usage") or {}
        parts = []
        if u.get("searches"):
            parts.append(f"{u['searches']} search{'es' if u['searches'] != 1 else ''}")
        if u.get("retrievals"):
            parts.append(f"{u['retrievals']} document lookup{'s' if u['retrievals'] != 1 else ''}")
        if u.get("input_tokens") or u.get("output_tokens"):
            parts.append(f"{u.get('input_tokens', 0)}+{u.get('output_tokens', 0)} tokens")
        if state.get("sources"):
            parts.append(f"{state['sources']} source{'s' if state['sources'] != 1 else ''}")
        return " · ".join(parts)

    async def _chat(self, bearer: str, sess: str, sess_ctl: str, payload: dict, status, source, mode: str):
        state: dict[str, Any] = {"cursor": 0}
        try:
            async for ev in self._invoke(bearer, sess, payload):
                async for piece in self._render(ev, state, status, source):
                    yield piece
            if state.get("job_id"):
                st = "clarifying" if state.get("clarifying") else (state.get("terminal") or "completed")
                if state.get("package") and st == "completed":
                    yield self._package_card(state["job_id"], state["package"], state)
                yield self._footer(state["job_id"], st, self._usage_line(state))
        except (asyncio.CancelledError, GeneratorExit):
            if state.get("job_id"):
                self._schedule_cancel(bearer, sess_ctl, state["job_id"])
                self._schedule_status(status, f"Stopped — research job cancelled (aiq-job:{state['job_id']}:cancelled)")
            raise
        except Exception as e:  # noqa: BLE001
            yield f"\n\n**AI-Q request failed:** {str(e)[:300]}"

    async def _tail(self, bearer: str, sess_tail: str, sess_ctl: str, job_id: str, after: int, status, source, mode: str):
        state: dict[str, Any] = {"cursor": after, "job_id": job_id}
        attempts = 0
        try:
            await status("Deep research running — you can close this tab; progress is durable.")
            while not state.get("terminal") and not state.get("clarifying") and attempts <= self.valves.TAIL_RETRIES:
                attempts += 1
                try:
                    async for ev in self._invoke(
                        bearer, sess_tail, {"op": "events", "job_id": job_id, "after": state["cursor"], "tail": True}
                    ):
                        attempts = 0
                        async for piece in self._render(ev, state, status, source):
                            yield piece
                        if state.get("terminal") or state.get("clarifying"):
                            break
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as e:  # noqa: BLE001 — network blip: reconnect from the cursor
                    await status(f"Reconnecting to job (from event {state['cursor']})… {str(e)[:80]}")
                    await asyncio.sleep(min(2**attempts, 30))
            st = "clarifying" if state.get("clarifying") else (state.get("terminal") or "running")
            if state.get("package") and st == "completed":
                yield self._package_card(job_id, state["package"], state)
            yield self._footer(job_id, st, self._usage_line(state))
        except (asyncio.CancelledError, GeneratorExit):
            self._schedule_cancel(bearer, sess_ctl, job_id)
            self._schedule_status(status, f"Stopped — research job cancelled (aiq-job:{job_id}:cancelled)")
            raise

    # ------------------------------------------------------------------ phase 3 helpers --
    @staticmethod
    def _last_package(messages: list[dict]) -> str | None:
        for m in reversed(messages):
            if m.get("role") != "assistant" or not isinstance(m.get("content"), str):
                continue
            found = PKG_FOOTER_RE.findall(m["content"])
            if found:
                return found[-1]
            found = JOB_FOOTER_RE.findall(m["content"])
            if found and found[-1][1] == "completed":
                return found[-1][0]
        return None

    def _render_models_in_use(self, session_models: dict) -> str:
        if not session_models:
            return (
                "No session model overrides — deployment defaults or your Workbench preferences apply. "
                "Set with `/model writer=<id>[@lane] planner=<id> researcher=<id> shallow=<id> clarifier=<id>`; see `/models`."
            )
        return "**Session model overrides**\n\n" + "\n".join(
            f"- **{r}** → `{v['model_id']}` ({v.get('lane')})" for r, v in session_models.items()
        )

    async def _render_models_catalog(self, bearer: str, sess_ctl: str, question: str) -> str:
        parts = question.split()
        role = parts[1] if len(parts) > 1 and parts[1] in ROLES else "writer"
        data = None
        async for ev in self._invoke(bearer, sess_ctl, {"op": "models", "role": role}, 60):
            if ev.get("type") == "error":
                return f"Error: {(ev.get('data') or {}).get('error', {}).get('message')}"
            if ev.get("type") == "models":
                data = ev.get("data") or {}
        if not data:
            return "The capability matrix is not available."
        mx = data.get("matrix") or {}
        st = mx.get("status") or {}
        offered = (data.get("offered_by_role") or {}).get(role) or []
        out = [
            f"**Models offered for the {role} role** — from the capability matrix probed {mx.get('generated_at', '?')} "
            f"({(mx.get('summary') or {}).get('offered', '?')} of {(mx.get('summary') or {}).get('entries', '?')} lane entries offered; "  # noqa: E501
            f"digest `{(mx.get('digest') or '')[:12]}`)",
            "",
            "| Model | id@lane | role score | $/1M in · out | TTFT |",
            "|---|---|---|---|---|",
        ]
        for e in offered[:25]:
            price = f"{e['input_per_1m']:.2f} · {e['output_per_1m']:.2f}" if e.get("input_per_1m") is not None else "unpriced"
            est = " (est.)" if e.get("price_source") == "overlay" else ""
            lane = "" if e["lane"] == "converse" else f"@{e['lane']}"
            out.append(
                f"| {e['name']} | `{e['id']}{lane}` | {e.get('score', '—')} | {price}{est} | {e.get('ttft_ms') or '—'} ms |"
            )
        res = data.get("resolved") or {}
        cur = ", ".join(
            f"{r}: {v.get('human_name') or v.get('model_id')}"
            for r, v in res.items()
            if r in ("shallow", "planner", "researcher", "writer")
        )
        excluded = [e for e in (data.get("entries") or []) if not e.get("offered")]
        out += [
            "",
            f"Current selection → {cur}",
            f"Excluded lane entries: {len(excluded)} (reasons in the Workbench Model Lab; e.g. "
            + "; ".join(
                sorted(
                    {
                        (e.get("exclusion_reasons") or ["?"])[0].split(":")[0]
                        + ":"
                        + (e.get("exclusion_reasons") or ["?"])[0].split(":")[-1]
                        for e in excluded
                    }
                )[:4]
            )
            + ")",
            "",
            f"Set: `/model {role}=<id>[@lane]` · other roles: `/models planner` · `/models shallow`",
        ]
        if not st.get("loaded"):
            out.append(f"\n⚠ matrix status: {st.get('error')}")
        wb = self._workbench_link("/lab")
        if wb:
            out.append(f"\n[Model Lab ↗]({wb})")
        return "\n".join(out)

    async def _render_compare(self, bearer: str, sess_ctl: str, a: str, b: str) -> str:
        data = None
        async for ev in self._invoke(bearer, sess_ctl, {"op": "packages.compare", "job_id": a, "other_job_id": b}, 90):
            if ev.get("type") == "error":
                return f"Compare failed: {(ev.get('data') or {}).get('error', {}).get('message')}"
            if ev.get("type") == "comparison":
                data = ev.get("data") or {}
        if not data:
            return "No comparison returned."
        ra, rb, run = data.get("a") or {}, data.get("b") or {}, data.get("run") or {}
        wa = ((ra.get("models") or {}).get("writer") or {}).get("human_name", "—")
        wb_ = ((rb.get("models") or {}).get("writer") or {}).get("human_name", "—")
        src = data.get("sources") or {}
        out = [
            f"**Compare** `{a[:12]}…` (A, writer {wa}) ⇄ `{b[:12]}…` (B, writer {wb_})",
            "",
            "| | A | B |",
            "|---|---|---|",
            f"| cost | ${run.get('cost_a') or 0:.3f} | ${run.get('cost_b') or 0:.3f} |",
            f"| duration | {run.get('seconds_a') or '—'} s | {run.get('seconds_b') or '—'} s |",
            f"| citations verified | {run.get('citations_a')} | {run.get('citations_b')} |",
            f"| words | {run.get('words_a')} | {run.get('words_b')} |",
            f"| sources | {len(src.get('shared', [])) + len(src.get('only_a', []))} | {len(src.get('shared', [])) + len(src.get('only_b', []))} |",  # noqa: E501
            "",
            f"Sources: **{len(src.get('shared', []))} shared**, {len(src.get('only_a', []))} only in A, {len(src.get('only_b', []))} only in B",  # noqa: E501
            "",
        ]
        secs = data.get("sections") or []
        if secs:
            out.append("**Sections**")
            for sct in secs[:14]:
                mark = "both" if sct["in_a"] and sct["in_b"] else ("A only" if sct["in_a"] else "B only")
                out.append(f"- {sct['title']} — {mark}")
        link = self._workbench_link(f"/compare/{a}/{b}")
        if link:
            out.append(f"\n[Full side-by-side in the Workbench ↗]({link})")
        return "\n".join(out)

    async def _export(self, bearer: str, sess_ctl: str, job_id: str, fmt: str, emitter, __user__: dict, __request__):
        """`/export <fmt>`: render on the runtime, then a short-lived link and (optionally) an Open WebUI attachment (A Q7)."""
        yield f"Rendering **{fmt.upper()}** export for package `{job_id[:12]}…`…\n\n"
        data = None
        async for ev in self._invoke(bearer, sess_ctl, {"op": "export", "job_id": job_id, "format": fmt}, 240):
            d = ev.get("data") or {}
            if ev.get("type") == "error":
                yield f"**Export failed:** {d.get('error', {}).get('message')}"
                return
            if ev.get("type") == "export":
                data = d
        if not data:
            yield "No export returned."
            return
        size_kb = (data.get("size") or 0) / 1024
        derived = " · derived summary" if data.get("derived") else ""
        yield (f"**{data['filename']}** — {size_kb:.0f} KB{derived} · [download (link valid 10 min)]({data['url']})\n\n")
        if self.valves.ATTACH_EXPORTS and emitter is not None:
            try:  # copy into Open WebUI Files so the attachment survives after the link expires (owner-scoped, A Q7-03)
                file_meta = await self._attach_file(data["url"], data["filename"], data["content_type"], __user__)
                if file_meta:
                    await emitter(
                        {
                            "type": "files",
                            "data": {
                                "files": [
                                    {
                                        "type": "file",
                                        "id": file_meta["id"],
                                        "url": file_meta["id"],
                                        "name": data["filename"],
                                        "size": data.get("size"),
                                        "meta": {"content_type": data["content_type"]},
                                    }
                                ]
                            },
                        }
                    )
                    yield "_Attached to this message as a file._"
            except Exception as e:  # noqa: BLE001
                log.warning("aiq_agentcore: attach failed (%s)", e)
                yield f"_Attachment skipped ({e.__class__.__name__}); use the link above._"
        pkg_link = self._workbench_link(f"/exports/{job_id}")
        if pkg_link:
            yield f"\n\n[All formats in the Export center ↗]({pkg_link})"
        yield self._footer(job_id, "completed")

    async def _attach_file(self, url: str, name: str, content_type: str, __user__: dict) -> dict | None:
        import io
        import uuid as _uuid

        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"download HTTP {resp.status}")
                data = await resp.read()
        if len(data) > 25 * 1024 * 1024:
            raise RuntimeError("file larger than 25 MB")
        fid = str(_uuid.uuid4())
        _, path = Storage.upload_file(io.BytesIO(data), f"{fid}_{name}", {"OpenWebUI-User-Id": __user__.get("id", "")})
        form = FileForm(
            id=fid,
            filename=name,
            path=path,
            data={},
            meta={"name": name, "content_type": content_type, "size": len(data), "source": "aiq_agentcore export"},
        )
        f = Files.insert_new_file(__user__["id"], form)
        if asyncio.iscoroutine(f):
            f = await f
        return {"id": fid} if f else None

    def _schedule_status(self, status, text: str) -> None:
        """Best-effort persisted status after a Stop: emitted from a fresh task because the pipe's own task is
        being cancelled (status events are persisted to the message by Open WebUI)."""
        try:
            asyncio.get_running_loop().create_task(status(text, True))
        except RuntimeError:
            pass

    def _schedule_cancel(self, bearer: str, sess_ctl: str, job_id: str) -> None:
        async def _cancel():
            try:
                async for _ in self._invoke(bearer, sess_ctl, {"op": "cancel", "job_id": job_id}, 60):
                    pass
            except Exception as e:  # noqa: BLE001
                log.warning("aiq_agentcore: cancel failed for %s (%s)", job_id, e)

        try:
            asyncio.get_running_loop().create_task(_cancel())
        except RuntimeError:
            pass

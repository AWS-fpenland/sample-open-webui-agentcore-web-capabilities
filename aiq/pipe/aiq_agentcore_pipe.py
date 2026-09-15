# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
title: AI-Q Research on AgentCore
id: aiq_agentcore
description: NVIDIA AI-Q research agents running on Amazon Bedrock AgentCore Runtime — quick cited answers, deep multi-phase research with progress, reconnect, cancel, clarification, and per-user document collections.
version: 0.1.0
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

__pipe_version__ = "0.1.0"

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
    ("auto", "AI-Q Research (auto)", "Routes between a quick cited answer and deep research; may ask a clarifying question first."),
    ("shallow", "AI-Q Quick answer", "Fast, bounded web research with citations."),
    ("deep", "AI-Q Deep research", "Multi-phase research (planner, researchers, writer) producing a cited report."),
    ("deep_clarify", "AI-Q Deep research (clarify first)", "Asks clarifying questions, then runs deep research after you confirm."),
]
JOB_FOOTER_RE = re.compile(r"aiq-job:(job_[a-f0-9]{32}):(clarifying|completed|cancelled|running|failed)")
COMMANDS = {"/cancel", "/status", "/collections", "/help"}
TERMINAL = {"completed", "cancelled"}


class Pipe:
    class Valves(BaseModel):
        RUNTIME_ARN: str = Field(default=os.environ.get("AIQ_RUNTIME_ARN", ""),
                                 description="AgentCore Runtime ARN for AI-Q (aiq_<runId>).")
        REGION: str = Field(default=os.environ.get("AWS_REGION", "us-east-1"), description="AgentCore data-plane region.")
        RUN_ID: str = Field(default="", description="Run id shown in footers (informational).")
        REQUEST_TIMEOUT_S: int = Field(default=3300, description="Max seconds per streaming request (runtime cap is 3600).")
        TAIL_RETRIES: int = Field(default=20, description="Reconnect attempts while tailing a job.")
        SHOW_TOOL_EVENTS: bool = Field(default=True, description="Show tool calls as status lines.")
        DEFAULT_COLLECTION_PREFIX: str = Field(default="chat", description="Collection name prefix for uploaded files.")
        APPROVAL_KEYWORDS: str = Field(default="approve,yes,go,proceed,start",
                                       description="Replies that approve a pending clarification/plan.")

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ models --
    def pipes(self) -> list[dict]:
        return [{"id": mid, "name": name} for mid, name, _ in MODES]

    # ------------------------------------------------------------------ helpers --
    def _endpoint(self) -> str:
        arn = self.valves.RUNTIME_ARN.strip()
        if not arn:
            raise RuntimeError("RUNTIME_ARN valve is not set")
        from urllib.parse import quote

        return f"https://bedrock-agentcore.{self.valves.REGION}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"

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

                sessions = [s for s in await OAuthSessions.get_sessions_by_user_id(user_id)
                            if not (s.provider or "").startswith("mcp:")]
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
        headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                   "Accept": "text/event-stream, application/json",
                   "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id}
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
        return f"\n\n---\n_aiq-job:{job_id}:{state}{(' · ' + extra) if extra else ''}_"

    @staticmethod
    def _source_event(src: dict) -> dict:
        url = src.get("url") or ""
        name = src.get("title") or url or src.get("document_key") or src.get("source_id")
        meta = {"source": url or f"document://{src.get('document_key') or src.get('source_id')}", "name": name}
        if src.get("retrieved_at"):
            meta["date_accessed"] = src["retrieved_at"]
        return {"type": "source", "data": {"source": {"name": name, "url": url} if url else {"name": name},
                                           "document": [src.get("snippet") or name],
                                           "metadata": [meta]}}

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
                out.append({"name": name, "content_type": ctype, "content_b64": base64.b64encode(data).decode(),
                            "size_bytes": len(data)})
            except Exception as e:  # noqa: BLE001
                log.warning("aiq_agentcore: could not read file %s (%s)", item.get("id"), e)
        return out

    # ------------------------------------------------------------------ main --
    async def pipe(self, body: dict, __user__: dict | None = None, __metadata__: dict | None = None,
                   __event_emitter__=None, __oauth_token__=None, __files__=None, __request__=None):
        __user__ = __user__ or {}
        __metadata__ = __metadata__ or {}
        if __metadata__.get("task"):  # title/tags/follow-up generation: cheap deterministic reply
            msgs = body.get("messages") or []
            last = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "")
            return (str(last)[:60] if isinstance(last, str) else "AI-Q research")

        model_id = body.get("model", "")
        mode = model_id.split(".", 1)[1] if "." in model_id else "auto"
        if mode not in {m for m, _, _ in MODES}:
            mode = "auto"
        chat_id = __metadata__.get("chat_id") or f"nochat-{uuid.uuid4().hex[:8]}"
        message_id = __metadata__.get("message_id") or uuid.uuid4().hex
        user_id = __user__.get("id", "anon")
        messages = [{"role": m.get("role", "user"), "content": m.get("content") if isinstance(m.get("content"), str)
                     else " ".join(b.get("text", "") for b in (m.get("content") or []) if isinstance(b, dict))}
                    for m in body.get("messages") or [] if m.get("role") in ("user", "assistant", "system")]
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "").strip()

        bearer = await self._bearer(__user__, __oauth_token__, __request__)
        if not bearer:
            return ("AI-Q on AgentCore needs your signed-in SSO session (Cognito access token) to identify you. "
                    "Please sign in with SSO and try again.")

        emitter = __event_emitter__

        async def status(desc: str, done: bool = False, hidden: bool = False):
            if emitter:
                await emitter({"type": "status", "data": {"description": desc, "done": done, "hidden": hidden}})

        async def source(src: dict):
            if emitter:
                await emitter(self._source_event(src))

        sess_job = self._session_id("job", chat_id, user_id)
        sess_tail = self._session_id("tail", chat_id, user_id)
        sess_ctl = self._session_id("ctl", chat_id, user_id)

        # ---- slash commands -------------------------------------------------
        cmd = question.split()[0].lower() if question else ""
        pending = self._pending_job(messages)
        if cmd in COMMANDS:
            if cmd == "/help":
                return ("**AI-Q commands**\n\n- `/status` — status of this chat's last research job\n- `/cancel` — cancel it\n"
                        "- `/collections` — list your document collections\n\nAttach files to add them to this chat's collection; "
                        "reply **approve** to a clarifying question to start deep research, or describe changes.")
            if cmd == "/collections":
                lines = []
                async for ev in self._invoke(bearer, sess_ctl, {"op": "collections"}, 60):
                    for c in (ev.get("data") or {}).get("collections", []):
                        lines.append(f"- **{c['name']}** — {len(c['documents'])} document(s): " + ", ".join(d['name'] for d in c['documents'][:10]))
                return "**Your document collections**\n\n" + ("\n".join(lines) if lines else "_none yet — attach a file to create one_")
            if not pending:
                return "No AI-Q job is associated with this chat yet."
            job_id, _ = pending
            op = "cancel" if cmd == "/cancel" else "status"
            out = []
            async for ev in self._invoke(bearer, sess_ctl, {"op": op, "job_id": job_id}, 60):
                d = ev.get("data") or {}
                if ev.get("type") == "error":
                    return f"Error: {d.get('error', {}).get('message', 'unknown')}"
                out.append(f"Job `{job_id}` — status **{d.get('status')}**, events {d.get('last_seq')}, mode {d.get('mode')}"
                           + (f", cancel requested" if d.get("cancel_requested") else ""))
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
                await status(f"Indexing {sync.get('documents')} document(s) (ingestion job {sync.get('ingestion_job_id', '?')[:8]}…) — "
                             "retrieval becomes available when indexing completes.", done=True)

        # ---- resume a pending clarification with this reply -----------------
        if pending and pending[1] == "clarifying" and question:
            job_id, _ = pending
            approve_words = {w.strip().lower() for w in self.valves.APPROVAL_KEYWORDS.split(",")}
            lowered = question.lower().strip(" .!")
            approval = "reject" if lowered in {"cancel", "reject", "stop", "no"} else "approve"
            payload = {"op": "approve", "job_id": job_id, "approval": approval, "messages": messages,
                       "revision": None if lowered in approve_words else question, "collection": collection}
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
        base = {"mode": mode, "messages": messages, "client_request_id": message_id, "conversation_id": chat_id,
                "collection": collection}
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
            yield (d.get("question") or "Could you clarify your request?") + \
                  "\n\n_Reply with your answer, **approve** to proceed as-is, or **cancel**._"
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
        elif t == "warning":
            await status(f"Warning: {d.get('message', '')[:200]}")
        elif t == "error":
            err = d.get("error") or {}
            yield f"\n\n**AI-Q error** ({err.get('code', 'error')}): {err.get('message', 'unknown')}" + \
                  ("\n\n_You can retry; nothing is duplicated._" if err.get("retryable") else "")
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
                    async for ev in self._invoke(bearer, sess_tail, {"op": "events", "job_id": job_id,
                                                                       "after": state["cursor"], "tail": True}):
                        attempts = 0
                        async for piece in self._render(ev, state, status, source):
                            yield piece
                        if state.get("terminal") or state.get("clarifying"):
                            break
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as e:  # noqa: BLE001 — network blip: reconnect from the cursor
                    await status(f"Reconnecting to job (from event {state['cursor']})… {str(e)[:80]}")
                    await asyncio.sleep(min(2 ** attempts, 30))
            st = "clarifying" if state.get("clarifying") else (state.get("terminal") or "running")
            yield self._footer(job_id, st, self._usage_line(state))
        except (asyncio.CancelledError, GeneratorExit):
            self._schedule_cancel(bearer, sess_ctl, job_id)
            self._schedule_status(status, f"Stopped — research job cancelled (aiq-job:{job_id}:cancelled)")
            raise

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

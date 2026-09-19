# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-request model selection without forking upstream (ADR-23).

Upstream AI-Q binds one LangChain client per role at workflow load and looks it up at run time through
``aiq_agent.common.llm_provider.LLMProvider.get(role)`` (deep researcher: ``register.py`` configures ORCHESTRATOR /
ROUTER / PLANNER / RESEARCHER / REPORT_WRITER; shallow researcher and clarifier only ``set_default``). This module
patches ``LLMProvider.get`` — the same boundary-patch pattern as ``bedrock_compat`` — so that, when the current
``RunContext`` carries ``model_overrides``, the call returns a client for the *requested* model instead of the bound one.

Role mapping (ours → upstream): planner → ORCHESTRATOR, ROUTER (source router), PLANNER · researcher → RESEARCHER (deep) ·
writer → REPORT_WRITER · clarifier → CLARIFIER · shallow → RESEARCHER on the shallow workflow (recognised because its
provider has no configured roles, only a default). The intent router is not reachable through ``LLMProvider`` and
therefore stays on the deployment default (recorded honestly in the package manifest as ``source: deploy_default``).

Lanes: ``converse`` → ``ChatBedrockConverse``; ``mantle_chat`` → ``ChatOpenAI`` against
``https://bedrock-mantle.<region>.api.aws/v1``; ``mantle_messages`` → ``ChatAnthropic`` against
``https://bedrock-mantle.<region>.api.aws/anthropic``. Mantle lanes authenticate with a **short-term Bedrock API key**
minted from the runtime role at job start (``aws-bedrock-token-generator``; ≤ 12 h) and passed as the lane's API key
(Mantle rejects a request carrying both ``Authorization`` and ``x-api-key`` — verified 2026-09-18). Clients are cached per
job; sampling settings are copied from the bound client (temperature, max_tokens); provider-specific request fields
(e.g. Nemotron ``reasoning_effort``) are dropped because they do not transfer across families.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .run_context import get_run_context

log = logging.getLogger(__name__)
_PATCHED = False
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def mantle_token(region: str, max_age_s: float = 120.0) -> str:
    """Short-term Bedrock API key for the Mantle lanes.

    The generator presigns a token for up to 12 h, but the token is only valid while the *temporary credentials* it embeds
    are — and an AgentCore Runtime's role credentials rotate. A key minted at job start and held by the client for a long
    writer stage therefore dies with HTTP 401 "security token expired" (observed live 2026-09-18 on a 35-minute deep run).
    Signing is local and cheap, so the cache is short and every model call re-reads the key (see `_ModelErrorJournal`)."""
    import time

    tok, at = _TOKEN_CACHE.get(region, ("", 0.0))
    if tok and time.monotonic() - at < max_age_s:
        return tok
    from aws_bedrock_token_generator import provide_token

    tok = provide_token(region=region)
    _TOKEN_CACHE[region] = (tok, time.monotonic())
    return tok


def _settings_from(base: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dst in (("temperature", "temperature"), ("max_tokens", "max_tokens")):
        v = getattr(base, src, None)
        if v is not None:
            out[dst] = v
    return out


class _ModelErrorJournal:
    """LangChain callback: journals every provider error of an overridden client as a `warning` event (and a WARNING log),
    so users and operators see *why* a selected model failed instead of upstream's generic 'Research could not be completed'."""

    raise_error = False
    ignore_llm = False
    ignore_chain = True
    ignore_agent = True
    ignore_retriever = True
    ignore_chat_model = False
    ignore_retry = True
    ignore_custom_event = True
    run_inline = True

    def __init__(self, role: str, model_id: str, lane: str) -> None:
        self.role, self.model_id, self.lane = role, model_id, lane

    def _record(self, error: BaseException) -> None:
        detail = str(error)[:600]
        payload = {
            "event": "model.error",
            "role": self.role,
            "model_id": self.model_id,
            "lane": self.lane,
            "error_type": type(error).__name__,
            "message": detail,
        }
        log.warning(json.dumps(payload))
        ctx = get_run_context()
        if ctx is not None:
            try:
                ctx.note("warning", {"kind": "model_error", **{k: v for k, v in payload.items() if k != "event"}})
            except Exception:  # noqa: BLE001
                pass

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._record(error)

    async def aon_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._record(error)

    # --- Mantle lanes: re-read the short-term API key before every call (credentials behind it rotate) ---
    client: Any = None
    region: str = ""

    def _refresh_key(self) -> None:
        if self.client is None or self.lane not in ("mantle_chat", "mantle_messages"):
            return
        try:
            changed = refresh_mantle_key(self.client, self.lane, self.region)
        except Exception as e:  # noqa: BLE001 — never fail a model call because a refresh failed; the call surfaces its own error
            log.warning(json.dumps({"event": "mantle.token.refresh_failed", "lane": self.lane, "error": repr(e)[:200]}))
            return
        if changed:
            log.warning(
                json.dumps({"event": "mantle.token.refreshed", "role": self.role, "model_id": self.model_id, "lane": self.lane})
            )

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self._refresh_key()

    def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self._refresh_key()

    async def aon_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self._refresh_key()

    async def aon_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self._refresh_key()

    def __getattr__(self, name: str) -> Any:  # every other callback is a no-op
        if name.startswith(("on_", "aon_")):
            return _noop if name.startswith("on_") else _anoop
        raise AttributeError(name)


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _anoop(*args: Any, **kwargs: Any) -> None:
    return None


def refresh_mantle_key(client: Any, lane: str, region: str) -> bool:
    """Point the LangChain client's underlying SDK client(s) at a freshly minted Bedrock API key. Returns True if it changed.

    The Anthropic and OpenAI SDKs read ``api_key`` at request time, so mutating the attribute is enough — no client rebuild,
    no lost conversation state."""
    tok = mantle_token(region)
    targets = []
    if lane == "mantle_messages":
        targets = [getattr(client, "_client", None), getattr(client, "_async_client", None)]
    elif lane == "mantle_chat":
        targets = [getattr(client, "root_client", None), getattr(client, "root_async_client", None)]
    changed = False
    for t in targets:
        if t is not None and getattr(t, "api_key", None) != tok:
            t.api_key = tok
            changed = True
    return changed


def build_client(model_id: str, lane: str, base: Any, region: str) -> Any:
    settings = _settings_from(base)
    if lane == "converse":
        from langchain_aws import ChatBedrockConverse

        return ChatBedrockConverse(model_id=model_id, region_name=region, **settings)
    if lane == "mantle_chat":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_id,
            base_url=f"https://bedrock-mantle.{region}.api.aws/v1",
            api_key=mantle_token(region),
            stream_usage=True,
            **settings,
        )
    if lane == "mantle_messages":
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import AIMessage, HumanMessage

        class _MantleAnthropic(ChatAnthropic):
            """ChatAnthropic that never ends a conversation with an assistant turn.

            Upstream AI-Q (deepagents middleware, continuation steps) sometimes sends a message list whose last item is an
            AIMessage, which the Anthropic API treats as *prefill*. Claude Sonnet 5 on Mantle rejects that: HTTP 400 "This model
            does not support assistant message prefill. The conversation must end with a user message." (observed live
            2026-09-19 01:18Z). Appending an explicit user turn keeps the semantics — continue the previous answer — on every model."""

            @staticmethod
            def _no_prefill(messages):  # type: ignore[no-untyped-def]
                if messages and isinstance(messages[-1], AIMessage):
                    log.warning(json.dumps({"event": "mantle.prefill_rewritten", "model_id": messages_model_id}))
                    return [
                        *messages,
                        HumanMessage(
                            content="Continue exactly from where your previous message stopped. Do not repeat earlier text."
                        ),
                    ]  # noqa: E501
                return messages

            def _generate(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
                return super()._generate(self._no_prefill(messages), *args, **kwargs)

            async def _agenerate(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
                return await super()._agenerate(self._no_prefill(messages), *args, **kwargs)

            def _stream(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
                return super()._stream(self._no_prefill(messages), *args, **kwargs)

            def _astream(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
                return super()._astream(self._no_prefill(messages), *args, **kwargs)

        messages_model_id = model_id

        # Claude Sonnet 5 on Mantle rejects `temperature` ("`temperature` is deprecated for this model", HTTP 400 —
        # observed live 2026-09-18 when the writer copied the config's 0.2). Sampling parameters do not transfer across
        # families; the Anthropic lane runs with the model defaults (the Model Lab records this per model: `temperature` probe).
        settings.pop("temperature", None)
        settings.setdefault("max_tokens", 8192)
        return _MantleAnthropic(
            model=model_id,
            base_url=f"https://bedrock-mantle.{region}.api.aws/anthropic",
            api_key=mantle_token(region),
            **settings,
        )
    raise ValueError(f"unknown lane {lane}")


def _our_role(provider: Any, role: Any) -> str | None:
    name = getattr(role, "value", str(role))
    configured = getattr(provider, "_llms", {}) or {}
    deep = any(getattr(r, "value", str(r)) == "orchestrator" for r in configured)
    if deep:
        return {
            "orchestrator": "planner",
            "router": "planner",
            "planner": "planner",
            "researcher": "researcher",
            "report_writer": "writer",
        }.get(name)
    return {"clarifier": "clarifier", "researcher": "shallow", "report_writer": "writer"}.get(name)


def apply() -> bool:
    """Install the ``LLMProvider.get`` patch once. Returns False when upstream is not importable (unit tests)."""
    global _PATCHED
    if _PATCHED:
        return True
    try:
        from aiq_agent.common.llm_provider import LLMProvider
    except Exception as e:  # noqa: BLE001
        log.debug("llm_overrides not installed (%s)", e.__class__.__name__)
        return False
    original = LLMProvider.get

    def patched_get(self, role):  # type: ignore[no-untyped-def]
        base = original(self, role)
        ctx = get_run_context()
        overrides = getattr(ctx, "model_overrides", None) if ctx else None
        if not overrides:
            return base
        ours = _our_role(self, role)
        choice = overrides.get(ours) if ours else None
        if not choice:
            return base
        mid, lane = choice["model_id"], choice.get("lane", "converse")
        base_id = getattr(base, "model_id", None) or getattr(base, "model", None) or getattr(base, "model_name", None)
        if lane == "converse" and base_id == mid:
            return base
        cache = ctx.model_clients  # type: ignore[union-attr]
        key = (ours, mid, lane)
        if key not in cache:
            region = os.environ.get("AIQ_REGION") or os.environ.get("AWS_REGION", "us-east-1")
            client = build_client(mid, lane, base, region)
            try:  # journal provider errors + refresh the Mantle key before each call (see _ModelErrorJournal)
                journal = _ModelErrorJournal(ours, mid, lane)
                journal.client, journal.region = client, region
                client.callbacks = [*(getattr(client, "callbacks", None) or []), journal]
            except Exception:  # noqa: BLE001
                pass
            cache[key] = client
            ctx.note(
                "status",
                {"description": f"Model for {ours}: {choice.get('human_name') or mid} ({lane})", "done": True, "tool": "models"},
            )
            # WARNING on purpose: upstream AI-Q's relay logger reconfigures logging inside the workflow and INFO from
            # foreign loggers never reaches CloudWatch
            # (verified 2026-09-18: the journal `status` event appeared, this line did not).
            log.warning(
                json.dumps({"event": "model.override", "job_id": ctx.job_id, "role": ours, "model_id": mid, "lane": lane})
            )
        return cache[key]

    LLMProvider.get = patched_get  # type: ignore[method-assign]
    _PATCHED = True
    return True

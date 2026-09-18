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


def mantle_token(region: str) -> str:
    """Short-term Bedrock API key for the Mantle lanes (cached ~50 min; keys are valid up to 12 h)."""
    import time

    tok, at = _TOKEN_CACHE.get(region, ("", 0.0))
    if tok and time.monotonic() - at < 3000:
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

    def __getattr__(self, name: str) -> Any:  # every other callback is a no-op
        if name.startswith(("on_", "aon_")):
            return _noop if name.startswith("on_") else _anoop
        raise AttributeError(name)


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _anoop(*args: Any, **kwargs: Any) -> None:
    return None


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

        # Claude Sonnet 5 on Mantle rejects `temperature` ("`temperature` is deprecated for this model", HTTP 400 —
        # observed live 2026-09-18 when the writer copied the config's 0.2). Sampling parameters do not transfer across
        # families; the Anthropic lane runs with the model defaults (the Model Lab records this per model: `temperature` probe).
        settings.pop("temperature", None)
        settings.setdefault("max_tokens", 8192)
        return ChatAnthropic(
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
            try:  # journal provider errors of the selected model (see _ModelErrorJournal)
                client.callbacks = [*(getattr(client, "callbacks", None) or []), _ModelErrorJournal(ours, mid, lane)]
            except Exception:  # noqa: BLE001
                pass
            cache[key] = client
            ctx.note(
                "status",
                {"description": f"Model for {ours}: {choice.get('human_name') or mid} ({lane})", "done": True, "tool": "models"},
            )
            # WARNING on purpose: upstream AI-Q's relay logger reconfigures logging inside the workflow and INFO from
            # foreign loggers never reaches CloudWatch (verified 2026-09-18: the journal `status` event appeared, this line did not).
            log.warning(
                json.dumps({"event": "model.override", "job_id": ctx.job_id, "role": ours, "model_id": mid, "lane": lane})
            )
        return cache[key]

    LLMProvider.get = patched_get  # type: ignore[method-assign]
    _PATCHED = True
    return True

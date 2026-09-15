# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compatibility shim: let upstream AI-Q agents call Amazon Bedrock through LangChain.

Upstream AI-Q was validated against NVIDIA NIM / OpenAI-style chat models and passes two
kinds of provider-specific keyword arguments into LangChain model calls:

* ``llm.bind_tools(tools, parallel_tool_calls=...)`` (``shallow_researcher/agent.py``,
  ``clarifier/agent.py``) — OpenAI/NIM-only.
* ``extra_headers=...`` on invocations (NeMo Relay / NIM request headers) — OpenAI-only.

``ChatBedrockConverse`` forwards bound/extra kwargs into the Converse request after
snake→camel conversion, and ``_converse_params()`` has an explicit signature, so the
first model call fails with ``TypeError: ... unexpected keyword argument 'extraHeaders'``
(observed live 2026-09-15) or ``'parallelToolCalls'``.

Rather than patching upstream source, this module wraps ``ChatBedrockConverse``:
``bind_tools`` drops ``parallel_tool_calls`` and ``_converse_params`` ignores any keyword
its signature does not accept (logged once per key). Bedrock Converse always allows
parallel tool use; upstream's "exactly one tool call" intent is enforced by AI-Q's own
post-check, and the dropped headers only carried NIM/relay metadata. Idempotent; applied
once at engine start. Proposed upstream fix: guard both kwargs on the model class.
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

_APPLIED = False
_WARNED: set[str] = set()


def apply() -> bool:
    global _APPLIED
    if _APPLIED:
        return False
    try:
        from langchain_aws import ChatBedrockConverse
    except Exception as e:  # pragma: no cover - langchain-aws absent in adapter-only test envs
        log.warning("bedrock_compat: langchain_aws not importable (%s); shim not applied", e)
        return False

    original_bind = ChatBedrockConverse.bind_tools

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        if "parallel_tool_calls" in kwargs:
            dropped = kwargs.pop("parallel_tool_calls")
            log.debug("bedrock_compat: dropping parallel_tool_calls=%r for ChatBedrockConverse", dropped)
        return original_bind(self, tools, tool_choice=tool_choice, **kwargs)

    original_params = ChatBedrockConverse._converse_params
    accepted = set(inspect.signature(original_params).parameters) - {"self"}
    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in inspect.signature(original_params).parameters.values())

    def _converse_params(self, **kwargs):  # type: ignore[no-untyped-def]
        if not has_var_kw:
            for key in [k for k in kwargs if k not in accepted]:
                kwargs.pop(key)
                if key not in _WARNED:
                    _WARNED.add(key)
                    log.warning("bedrock_compat: ignoring unsupported Converse kwarg %r from upstream AI-Q", key)
        return original_params(self, **kwargs)

    ChatBedrockConverse.bind_tools = bind_tools  # type: ignore[method-assign]
    ChatBedrockConverse._converse_params = _converse_params  # type: ignore[method-assign]
    _APPLIED = True
    log.info("bedrock_compat: ChatBedrockConverse shims applied (bind_tools, _converse_params)")
    return True

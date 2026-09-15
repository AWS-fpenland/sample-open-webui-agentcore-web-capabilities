# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compatibility shim: let upstream AI-Q agents call Amazon Bedrock through LangChain.

Upstream AI-Q calls ``llm.bind_tools(tools, parallel_tool_calls=...)`` in two
places (``shallow_researcher/agent.py`` retry path and ``clarifier/agent.py``).
That keyword is NVIDIA-NIM/OpenAI-specific; ``ChatBedrockConverse`` forwards
unknown bound kwargs into the Converse request and the call fails with
``TypeError: unexpected keyword argument 'parallelToolCalls'``.

Rather than patching upstream source, this module wraps ``ChatBedrockConverse.bind_tools``
to drop ``parallel_tool_calls`` (Bedrock Converse always allows parallel tool
use; the upstream intent — "exactly one tool call" — is enforced by AI-Q's own
post-check). The shim is idempotent and applied once at engine start.

Proposed upstream fix: guard the kwarg on the model class or use
``tool_choice``; tracked in docs/05-implementation-and-deployment.md.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_APPLIED = False


def apply() -> bool:
    global _APPLIED
    if _APPLIED:
        return False
    try:
        from langchain_aws import ChatBedrockConverse
    except Exception as e:  # pragma: no cover - langchain-aws absent in adapter-only test envs
        log.warning("bedrock_compat: langchain_aws not importable (%s); shim not applied", e)
        return False

    original = ChatBedrockConverse.bind_tools

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
        if "parallel_tool_calls" in kwargs:
            dropped = kwargs.pop("parallel_tool_calls")
            log.debug("bedrock_compat: dropping parallel_tool_calls=%r for ChatBedrockConverse", dropped)
        return original(self, tools, tool_choice=tool_choice, **kwargs)

    bind_tools.__wrapped_by_aiq_agentcore__ = True  # type: ignore[attr-defined]
    ChatBedrockConverse.bind_tools = bind_tools  # type: ignore[method-assign]
    _APPLIED = True
    log.info("bedrock_compat: ChatBedrockConverse.bind_tools shim applied")
    return True

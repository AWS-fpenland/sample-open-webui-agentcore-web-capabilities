# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``agentcore_knowledge_search`` — AI-Q data-source tool over the run's Bedrock
Knowledge Base, filtered to the verified tenant (and optional collection).

Replaces the upstream knowledge layer (Chroma/LlamaIndex) for this deployment:
embeddings and chunking are server-side in the Knowledge Base, and per-user
isolation is enforced by a metadata filter on ``tenant_key`` that the tool
takes from the run context — never from model arguments.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from .. import knowledge
from ..run_context import get_run_context

log = logging.getLogger(__name__)


class AgentCoreKnowledgeSearchConfig(FunctionBaseConfig, name="agentcore_knowledge_search"):
    """Tenant-scoped retrieval from the caller's uploaded document collections (Bedrock Knowledge Base)."""

    top_k: int = Field(default=6, ge=1, le=25)


@register_function(config_type=AgentCoreKnowledgeSearchConfig)
async def agentcore_knowledge_search(config: AgentCoreKnowledgeSearchConfig, builder: Builder):
    if not os.environ.get("AIQ_KB_ID"):
        async def _unavailable(question: str) -> str:
            """Document knowledge search (unavailable - missing AIQ_KB_ID)."""
            return "Error: document search is not configured"

        yield FunctionInfo.from_fn(_unavailable, description=_unavailable.__doc__)
        return

    async def _knowledge_search(question: str) -> str:
        """Search the user's uploaded documents (their private collections). Returns passages with
        Citation/Source lines; cite documents by name and page."""
        ctx = get_run_context()
        if ctx is None:
            return "Error: no research context (documents can only be searched inside a job)"
        query = (question or "").strip()
        if not query:
            return "Error: empty query"
        ctx.counters["retrievals"] = ctx.counters.get("retrievals", 0) + 1
        ctx.note("tool.call", {"tool": "agentcore_knowledge_search", "input": {"query": query[:200],
                                                                                "collection": ctx.collection}})
        try:
            results = await asyncio.get_running_loop().run_in_executor(
                None, lambda: knowledge.retrieve(ctx.tenant_key, query, ctx.collection, config.top_k))
        except Exception as e:  # noqa: BLE001 — surface as a tool error, never crash the agent
            log.warning("knowledge retrieve failed: %s", e)
            ctx.note("tool.result", {"tool": "agentcore_knowledge_search", "error": str(e)[:300]})
            return f"Error: document search failed ({str(e)[:200]})"
        for src in knowledge.results_as_sources(results):
            ctx.record_source(src)
        ctx.note("tool.result", {"tool": "agentcore_knowledge_search", "results": len(results)})
        return knowledge.format_for_agent(results)

    yield FunctionInfo.from_fn(_knowledge_search, description=_knowledge_search.__doc__)

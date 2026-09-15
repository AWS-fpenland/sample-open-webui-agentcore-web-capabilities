# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``agentcore_web_search`` — AI-Q data-source tool backed by the AgentCore Gateway
managed Web Search connector (replaces Tavily; same single-string contract).

Output layout mirrors upstream's Tavily/Exa sources so AI-Q's text-based
citation capture (``<Document href="url">`` blocks split by ``\\n\\n---\\n\\n``)
keeps working unchanged. Every result is also recorded in the run context as a
retrieved Source with provenance (tool, retrieved_at, publishedDate).

Acceptable-use notes (AWS Web Search): results are used only to answer the
current job; citations and links are retained in the report; snippets are kept
only in the job's citation ledger (30-day TTL) and never bulk-stored or indexed.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import UTC, datetime

from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from ..contracts import Source
from ..mcp_gateway import GatewayError, GatewayMcpClient, extract_json_content
from ..run_context import get_run_context

log = logging.getLogger(__name__)
_XML_INVALID = re.compile(r"[^\x09\x0A\x0D\x20-퟿-�]")
QUERY_MAX = 200  # connector limit (docs, 2026-09-15)


class AgentCoreWebSearchConfig(FunctionBaseConfig, name="agentcore_web_search"):
    """Managed web search through an AgentCore Gateway (AWS_IAM inbound, SigV4)."""

    max_results: int = Field(default=5, ge=1, le=25, description="Results per query (connector allows 1-25)")
    max_content_length: int | None = Field(default=1200, description="Truncate each result's text to this many chars")
    tool_name: str = Field(default_factory=lambda: os.environ.get("AIQ_WEB_SEARCH_TOOL", "web-search-tool___WebSearch"))
    gateway_url: str | None = Field(default=None, description="Override AIQ_GATEWAY_URL")
    timeout_seconds: float = Field(default=30.0)


def _clean(text: str) -> str:
    return html.escape(_XML_INVALID.sub("", text or ""), quote=False)


def format_results(results: list[dict], max_content_length: int | None) -> str:
    blocks = []
    for r in results:
        url = str(r.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = _clean(str(r.get("title") or url))
        text = str(r.get("text") or r.get("snippet") or "")
        if max_content_length and len(text) > max_content_length:
            text = text[:max_content_length].rstrip() + "…"
        published = r.get("publishedDate") or r.get("published_date")
        meta = f"\n<published>{_clean(str(published))}</published>" if published else ""
        blocks.append(f'<Document href="{_clean(url)}">\n<title>\n{title}\n</title>{meta}\n{_clean(text)}\n</Document>')
    return "\n\n---\n\n".join(blocks) if blocks else "Search returned no results"


@register_function(config_type=AgentCoreWebSearchConfig)
async def agentcore_web_search(config: AgentCoreWebSearchConfig, builder: Builder):
    gateway_url = config.gateway_url or os.environ.get("AIQ_GATEWAY_URL")
    if not gateway_url:
        async def _unavailable(question: str) -> str:
            """Web search (unavailable - missing AIQ_GATEWAY_URL)."""
            return "Error: web search is not configured (AIQ_GATEWAY_URL is missing)"

        yield FunctionInfo.from_fn(_unavailable, description=_unavailable.__doc__)
        return

    client = GatewayMcpClient(url=gateway_url, timeout=config.timeout_seconds)

    async def _search(question: str) -> str:
        """Search the public web for current information. Returns titled documents with source URLs
        (cite them); each block is a distinct source."""
        query = (question or "").strip()
        if not query:
            return "Error: empty query"
        if len(query) > QUERY_MAX:
            query = query[:QUERY_MAX]
        ctx = get_run_context()
        if ctx and ctx.cancelled is not None and ctx.cancelled.is_set():
            return "Error: research cancelled"
        if ctx:
            ctx.counters["searches"] = ctx.counters.get("searches", 0) + 1
            ctx.note("tool.call", {"tool": "agentcore_web_search", "input": {"query": query, "max_results": config.max_results}})
        try:
            res = await client.call_tool(config.tool_name, {"query": query, "maxResults": config.max_results})
        except GatewayError as e:
            log.warning("agentcore_web_search failed: %s", e)
            if ctx:
                ctx.note("tool.result", {"tool": "agentcore_web_search", "error": str(e)[:300]})
            return f"Error: web search failed ({str(e)[:200]})"
        data = extract_json_content(res)
        results = data.get("results", []) if isinstance(data, dict) else []
        retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        recorded = 0
        for r in results:
            url = str(r.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            src = Source(source_id=Source.make_id(url), url=url, title=(r.get("title") or None), kind="web_search",
                         retrieved_at=retrieved_at, tool="agentcore_web_search",
                         snippet=(str(r.get("text") or "")[:500] or None))
            if ctx:
                ctx.record_source(src)
            recorded += 1
        if ctx:
            ctx.note("tool.result", {"tool": "agentcore_web_search", "results": recorded, "query": query})
        return format_results(results, config.max_content_length)

    try:
        yield FunctionInfo.from_fn(_search, description=_search.__doc__)
    finally:
        await client.aclose()

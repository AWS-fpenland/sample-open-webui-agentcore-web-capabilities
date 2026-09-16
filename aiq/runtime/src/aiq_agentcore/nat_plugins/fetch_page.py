# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``agentcore_fetch_page`` — AI-Q tool that loads one public web page in an **Amazon Bedrock
AgentCore Browser** session and returns bounded readable text for the researcher.

Why Browser (not a raw HTTP fetch): JavaScript-rendered pages, consistent egress from an
AWS-managed, isolated Chromium (never from the runtime's own network), no cookies/profiles,
one fresh session per page, hard TTL. Playwright is used only as a CDP client over the
signed automation stream (no local browser binaries).

Safety: URL policy (url_policy.py) before navigation and again on the final URL after
redirects; downloads, dialogs, popups and non-GET navigations are blocked; extraction is
``document.body.innerText`` truncated to ``max_chars``; the returned text is wrapped as
untrusted content. Every fetched page is recorded as a retrieved Source with provenance.
Terms: page text is used only for the current job and its citation ledger (30-day TTL).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import UTC, datetime

from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from ..contracts import Source
from ..run_context import get_run_context
from ..url_policy import evaluate_url, policy_from_env

log = logging.getLogger(__name__)
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")


class AgentCoreFetchPageConfig(FunctionBaseConfig, name="agentcore_fetch_page"):
    """Load a public web page with AgentCore Browser and return its readable text."""

    max_chars: int = Field(default=12000, ge=1000, le=60000)
    navigation_timeout_seconds: float = Field(default=30.0)
    session_timeout_seconds: int = Field(default=180, ge=60, le=900, description="AWS-side browser session TTL")
    max_pages_per_job: int = Field(default=12, ge=1, le=50)
    browser_identifier: str = Field(default="aws.browser.v1")
    region: str | None = Field(default=None)


def clean_text(text: str, max_chars: int) -> tuple[str, bool]:
    text = _WS.sub(" ", text or "")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _NL.sub("\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "…", True
    return text, False


def render(url: str, final_url: str, title: str, text: str, truncated: bool) -> str:
    """Tavily-compatible layout so AI-Q's citation capture sees the page as a source."""
    note = " (truncated)" if truncated else ""
    return (f'<Document href="{final_url}">\n<title>\n{title or final_url}\n</title>\n'
            f"<note>Untrusted page content{note}; requested {url}</note>\n{text}\n</Document>")


async def _load_with_browser(url: str, cfg: AgentCoreFetchPageConfig, region: str) -> dict:
    """Run one fresh AgentCore Browser session and extract text. Blocking SDK calls run in a thread."""
    from bedrock_agentcore.tools.browser_client import BrowserClient
    from playwright.async_api import async_playwright

    loop = asyncio.get_running_loop()
    client = BrowserClient(region)
    session_id = await loop.run_in_executor(None, lambda: client.start(identifier=cfg.browser_identifier,
                                                                       session_timeout_seconds=cfg.session_timeout_seconds,
                                                                       name="aiq-fetch-page"))
    started = time.monotonic()
    try:
        ws_url, headers = await loop.run_in_executor(None, client.generate_ws_headers)
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=45_000)
            try:
                context = await browser.new_context(accept_downloads=False, java_script_enabled=True,
                                                    user_agent="Mozilla/5.0 (compatible; AIQ-AgentCore-Research/1.0)")
                page = await context.new_page()
                page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
                # Block non-document heavy resources and any non-GET navigation.
                async def route(r):
                    req = r.request
                    if req.method != "GET" or req.resource_type in ("media", "font", "websocket", "eventsource", "other"):
                        await r.abort()
                    else:
                        await r.continue_()
                await context.route("**/*", route)
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=int(cfg.navigation_timeout_seconds * 1000))
                await page.wait_for_timeout(800)
                final_url = page.url
                status = resp.status if resp else None
                title = (await page.title()) or ""
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                return {"final_url": final_url, "status": status, "title": title, "text": text,
                        "session_id": session_id, "seconds": round(time.monotonic() - started, 2)}
            finally:
                await browser.close()
    finally:
        try:
            await loop.run_in_executor(None, client.stop)
        except Exception as e:  # noqa: BLE001 — the AWS TTL reclaims the session regardless
            log.warning("fetch_page: browser session stop failed (%s); TTL will reclaim %s", e, session_id)


@register_function(config_type=AgentCoreFetchPageConfig)
async def agentcore_fetch_page(config: AgentCoreFetchPageConfig, builder: Builder):
    region = config.region or os.environ.get("AIQ_REGION") or os.environ.get("AWS_REGION", "us-east-1")
    policy = policy_from_env()

    async def _fetch(url: str) -> str:
        """Open one public https web page (from search results) and return its readable text so you can
        cite specific facts. Use only for pages you need in depth; results are truncated."""
        ctx = get_run_context()
        if ctx and ctx.cancelled is not None and ctx.cancelled.is_set():
            return "Error: research cancelled"
        if ctx and ctx.counters.get("pages", 0) >= config.max_pages_per_job:
            return f"Error: page budget exhausted ({config.max_pages_per_job} pages per job)"
        decision = evaluate_url(url, **policy)
        if not decision.allowed:
            if ctx:
                ctx.note("tool.result", {"tool": "agentcore_fetch_page", "url": str(url)[:200],
                                         "error": f"policy:{decision.reason}"})
            return f"Error: URL not allowed ({decision.reason})"
        if ctx:
            ctx.counters["pages"] = ctx.counters.get("pages", 0) + 1
            ctx.note("tool.call", {"tool": "agentcore_fetch_page", "input": {"url": decision.normalized}})
        try:
            result = await _load_with_browser(decision.normalized, config, region)
        except Exception as e:  # noqa: BLE001 — surface as a tool error string (AI-Q contract)
            log.warning("fetch_page failed for %s: %s", decision.host, e.__class__.__name__)
            if ctx:
                ctx.note("tool.result", {"tool": "agentcore_fetch_page", "url": decision.normalized,
                                         "error": e.__class__.__name__})
            return f"Error: page load failed ({e.__class__.__name__})"
        final = evaluate_url(result["final_url"], **policy)
        if not final.allowed:
            if ctx:
                ctx.note("tool.result", {"tool": "agentcore_fetch_page", "url": decision.normalized,
                                         "error": f"redirect_policy:{final.reason}"})
            return f"Error: page redirected to a disallowed location ({final.reason})"
        text, truncated = clean_text(result["text"], config.max_chars)
        if not text:
            if ctx:
                ctx.note("tool.result", {"tool": "agentcore_fetch_page", "url": final.normalized, "error": "empty_page",
                                         "http_status": result["status"]})
            return "Error: page had no readable text"
        retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ctx:
            ctx.record_source(Source(source_id=Source.make_id(final.normalized), url=final.normalized,
                                     title=result["title"] or None, kind="web_page", retrieved_at=retrieved_at,
                                     tool="agentcore_fetch_page", snippet=text[:500]))
            ctx.note("tool.result", {"tool": "agentcore_fetch_page", "url": final.normalized, "chars": len(text),
                                     "truncated": truncated, "http_status": result["status"], "seconds": result["seconds"],
                                     "browser_session": result["session_id"]})
        return render(decision.normalized, final.normalized, result["title"], text, truncated)

    yield FunctionInfo.from_fn(_fetch, description=_fetch.__doc__)

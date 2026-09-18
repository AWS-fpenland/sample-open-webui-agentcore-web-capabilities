# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PRIMARY PDF path: print the styled HTML with an Amazon Bedrock AgentCore Browser session (ADR-26).

Evidence: research-tracks/phase3-export/probe-browser-pdf.txt — ``aws.browser.v1`` permits ``Page.printToPDF``;
a 3-page report printed in 0.16 s (≈2.6 s to first byte including the 1.9 s session start), text and link
annotations preserved, ``@media print`` honoured. Highest fidelity of every option measured (web fonts, CSS tables,
figures), and it reuses the exact pattern of ``nat_plugins/fetch_page.py``: ``BrowserClient(region).start(...)``,
``generate_ws_headers()``, Playwright as a CDP client over the signed stream (no local browser binaries).

Isolation: one fresh session per export, JavaScript disabled, and EVERY network request is aborted — the page can
only render the HTML string it was given (images are data URIs). The session is always stopped in ``finally``; the
AWS-side ``session_timeout_seconds`` reclaims it even if this process dies.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

DEFAULT_BROWSER_IDENTIFIER = "aws.browser.v1"


async def print_html_to_pdf(html: str, *, region: str, browser_identifier: str = DEFAULT_BROWSER_IDENTIFIER,
                            timeout_s: float = 60.0, session_name: str = "aiq-export-pdf") -> bytes:
    """Render a complete HTML document to A4 PDF bytes with the managed browser (print media, backgrounds on)."""
    if not html.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        raise ValueError("print_html_to_pdf expects a complete HTML document")
    from bedrock_agentcore.tools.browser_client import BrowserClient

    loop = asyncio.get_running_loop()
    client = BrowserClient(region)
    # AWS-side TTL: long enough for the print, short enough that an orphaned session costs little.
    session_timeout = int(min(max(timeout_s + 60, 60), 900))
    t0 = time.monotonic()
    session_id = await loop.run_in_executor(
        None, lambda: client.start(identifier=browser_identifier, session_timeout_seconds=session_timeout,
                                   name=session_name))
    log.info("export pdf: browser session %s started in %.2fs", session_id, time.monotonic() - t0)
    try:
        return await asyncio.wait_for(_print(client, html, timeout_s), timeout=timeout_s)
    finally:
        try:
            await loop.run_in_executor(None, client.stop)
            log.info("export pdf: browser session %s stopped (total %.2fs)", session_id, time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001 — the AWS TTL reclaims the session regardless
            log.warning("export pdf: browser session stop failed (%s); TTL will reclaim %s", e, session_id)


async def _print(client, html: str, timeout_s: float) -> bytes:
    from playwright.async_api import async_playwright

    loop = asyncio.get_running_loop()
    ws_url, headers = await loop.run_in_executor(None, client.generate_ws_headers)
    ms = int(timeout_s * 1000)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers, timeout=ms)
        try:
            context = await browser.new_context(accept_downloads=False, java_script_enabled=False)

            async def deny(route) -> None:
                # Only the document we set and its inline data: URIs may load; nothing leaves the browser.
                if route.request.url.startswith("data:"):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await context.route("**/*", deny)
            page = await context.new_page()
            await page.set_content(html, wait_until="load", timeout=ms)
            await page.emulate_media(media="print")
            pdf = await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            if not pdf.startswith(b"%PDF"):
                raise RuntimeError("browser returned non-PDF bytes")
            return bytes(pdf)
        finally:
            await browser.close()

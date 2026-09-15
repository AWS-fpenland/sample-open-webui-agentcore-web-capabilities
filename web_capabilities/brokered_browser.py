"""Curated rendering with request fulfillment and externally isolated Browser.

Only deploy with the dedicated no-egress VPC and default-block DNS Firewall
construct. This controller never continues a Browser network request. The managed
microVM/browser sandbox remains a trusted AWS boundary, not a customer OS firewall.
No arbitrary browsing, credentials, forms, profiles, mounts or recordings.
"""

import asyncio

from web_capabilities.browser import BrowserError, BrowserFetcher, BrowserPolicyError
from web_capabilities.http_fetch import FetchBudget, PublicHTTPSFetcher
from web_capabilities.documents import utf8_content_type
from web_capabilities.url_policy import URLPolicyError


EXTRACT = """limit => {
    const root = document.querySelector('main') || document.querySelector('article') || document.body;
    const text = root ? root.innerText : '';
    const links = [];
    if (root) for (const anchor of root.querySelectorAll('a[href]')) {
        if (links.length >= 20) break;
        if (anchor.href.startsWith('https://') && anchor.href.length <= 2048 && !links.includes(anchor.href)) links.push(anchor.href);
    }
    return {title: document.title.slice(0, 2000), text: text.slice(0, limit), truncated: text.length > limit, links};
}"""


class BrokeredBrowserFetcher(BrowserFetcher):
    def __init__(self, *, policy, region, browser_identifier, broker=None, **kwargs):
        super().__init__(region=region, browser_identifier=browser_identifier,
                         allowed_hosts=policy.hosts, enabled=True, network_policy_ready=True, **kwargs)
        self.policy = policy
        self.broker = broker or PublicHTTPSFetcher(allowed_hosts=policy.hosts, max_redirects=0,
                                                  max_response_bytes=1024 * 1024, total_timeout=10)
        if self.broker.max_redirects != 0:
            raise BrowserPolicyError("Brokered Browser rejects all redirects")

    async def _extract(self, url, state):
        navigation_url = self.policy.validate(url)
        await self._validate_url(navigation_url)
        budget = FetchBudget(max_requests=16, max_bytes=3 * 1024 * 1024, total_timeout=24)
        session = await self._acquire(state, "session", asyncio.to_thread(self._start, state))
        endpoint = session["streams"]["automationStream"]["streamEndpoint"]
        headers = await asyncio.to_thread(self._headers, endpoint, state)
        factory = self.playwright_factory
        if factory is None:
            from playwright.async_api import async_playwright

            factory = async_playwright
        playwright = await self._acquire(state, "playwright", factory().start())
        browser = await self._acquire(state, "browser", playwright.chromium.connect_over_cdp(
            endpoint, headers=headers, timeout=5000))
        context = await self._acquire(state, "context", browser.new_context(
            service_workers="block", accept_downloads=False, permissions=[]))
        state["blocked_subresources"] = 0
        state["route_events"] = 0
        semaphore = asyncio.Semaphore(3)

        async def route_request(route):
            state["route_events"] += 1
            request = route.request
            if state["route_events"] > 32:
                state["denied"] = True
                await route.abort("blockedbyclient")
                return
            try:
                if request.method != "GET":
                    raise BrowserPolicyError("Only GET reading requests are permitted")
                if request.resource_type in {"image", "media", "font"}:
                    state["blocked_subresources"] += 1
                    await route.abort("blockedbyclient")
                    return
                try:
                    target = self.policy.validate(request.url)
                except URLPolicyError:
                    if request.resource_type != "stylesheet":
                        raise
                    state["blocked_subresources"] += 1
                    await route.abort("blockedbyclient")
                    return
                async with semaphore:
                    response = await self.broker.fetch(target, budget=budget, follow_redirects=False)
                if response.status != 200 or response.final_url != target:
                    raise BrowserPolicyError("Only direct successful reading responses are permitted")
                response.body.decode("utf-8", errors="strict")
                await route.fulfill(status=200, body=response.body, headers={
                    "content-type": utf8_content_type(response.content_type) + "; charset=utf-8",
                    "cache-control": "no-store", "x-content-type-options": "nosniff",
                    "referrer-policy": "no-referrer"
                })
            except Exception:
                state["denied"] = True
                await route.abort("blockedbyclient")

        async def close_socket(websocket):
            state["denied"] = True
            await websocket.close()

        await context.route("**/*", route_request)
        await context.route_web_socket("**/*", close_socket)
        page = await context.new_page()

        async def close_popup(popup):
            state["denied"] = True
            await popup.close()

        async def close_download(download):
            state["denied"] = True
            await download.cancel()
            await page.close()

        context.on("page", close_popup)
        page.on("download", close_download)
        response = await page.goto(navigation_url, wait_until="domcontentloaded", timeout=15000)
        if response is None or response.status != 200 or response.headers.get("content-type", "").split(";", 1)[0] not in {
            "text/html", "text/plain"
        }:
            raise BrowserError("Unsupported rendered document")
        await page.wait_for_timeout(500)
        if page.url != navigation_url or state.get("denied"):
            raise BrowserPolicyError("Page violated curated reading policy")
        document = await page.evaluate(EXTRACT, self.max_text)
        if (not isinstance(document, dict) or not isinstance(document.get("text"), str)
                or len(document["text"]) > self.max_text or not isinstance(document.get("title"), str)
                or len(document["title"]) > 2000 or type(document.get("truncated")) is not bool
                or not isinstance(document.get("links"), list) or len(document["links"]) > 20
                or any(not isinstance(link, str) or len(link) > 2048 for link in document["links"])):
            raise BrowserError("Invalid rendered extraction")
        if page.url != navigation_url or state.get("denied"):
            raise BrowserPolicyError("Page violated curated reading policy")
        return {**document, "requested_url": url, "final_url": navigation_url,
                "source_capability": "agentcore-browser-brokered", "extraction": "rendered-main-or-body",
                "broker_requests": budget.requests_used, "broker_bytes": budget.bytes_used,
                "blocked_optional_resources": state["blocked_subresources"],
                "aws_start_request_id": session.get("ResponseMetadata", {}).get("RequestId")}

    async def load(self, url):
        document = await super().load(url)
        document["session_terminated"] = True
        return document

    async def _cleanup(self, state):
        await super()._cleanup(state)
        if "session" in state:
            observed = await asyncio.wait_for(asyncio.to_thread(state["client"].get_browser_session,
                browserIdentifier=self.identifier, sessionId=state["session"]["sessionId"]), timeout=5)
            if observed.get("status") != "TERMINATED":
                raise BrowserError("Browser termination not confirmed")

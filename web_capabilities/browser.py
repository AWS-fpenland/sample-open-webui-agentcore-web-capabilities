"""Experimental fixed public read-only extraction; NOT SSRF-complete alone.

Local DNS checks are defense in depth, NOT remote-browser network pinning.
UNSHIPPABLE until independently enforced full egress policy is proven; setting
network_policy_ready is an attestation, not enforcement. Playwright routes have
a redirect interception gap: continuing a request can reach unvalidated redirect
destinations. Observed redirect rejection happens AFTER navigation, not before
network access. External egress must enforce destinations, redirects, credentials,
methods and transfer-byte limits. DOM truncation does NOT bound browser fetch
bytes or DOM memory. The automationStream key follows the user's live lifecycle
probe and boto3 1.43.94 model; real CDP/navigation and egress remain unverified.
That probe observed TERMINATED after stop; cleanup does not poll session status.
The request budget reserves 20 seconds for bounded, shielded cleanup. A late
SDK start is stopped by its worker even if the request has already timed out.
"""

import asyncio
import ipaddress
import math
import re
import socket
from urllib.parse import urlsplit


CAPABILITY_STATUS = "UNSHIPPABLE"
_EXTRACT_SCRIPT = """limit => {
    const root = document.querySelector('main') || document.querySelector('article') || document.body;
    const text = root ? root.innerText : '';
    return {title: document.title.slice(0, 2000), text: text.slice(0, limit), truncated: text.length > limit};
}"""


class BrowserError(RuntimeError):
    """Opaque browser failure, without page content or automation endpoints."""


class BrowserPolicyError(BrowserError):
    """Disabled capability or a request outside the configured public policy."""


async def _resolve(host):
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [record[4][0] for record in records]


def _public(address):
    parsed = ipaddress.ip_address(address)
    return parsed.is_global and not parsed.is_multicast and not parsed.is_unspecified


def _canonical_url(url):
    """Minimally normalize an already policy-validated HTTPS URL."""
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    query = "?" + parsed.query if "?" in url.split("#", 1)[0] else ""
    return f"{parsed.scheme.lower()}://{host}{parsed.path or '/'}{query}"


class BrowserFetcher:
    def __init__(self, *, region, browser_identifier=None, allowed_hosts=(), enabled=False,
                 network_policy_ready=False, deadline=45, max_text=20000,
                 aws_client=None, playwright_factory=None, resolver=None, header_signer=None):
        if enabled is not True or network_policy_ready is not True:
            raise BrowserPolicyError("Browser and externally enforced egress must be enabled")
        if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
            raise BrowserPolicyError("An explicit AWS region is required")
        if (not isinstance(browser_identifier, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+", browser_identifier)):
            raise BrowserPolicyError("An explicit custom browser identifier is required")
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or not 20 < deadline <= 45
                or type(max_text) is not int or not 1 <= max_text <= 20000):
            raise BrowserPolicyError("Invalid extraction limits")
        if isinstance(allowed_hosts, str):
            raise BrowserPolicyError("Exact trusted public hosts are required")
        self.hosts = frozenset(allowed_hosts)
        if not self.hosts or any(not isinstance(host, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) for host in self.hosts):
            raise BrowserPolicyError("Exact trusted public hosts are required")
        self.identifier, self.region = browser_identifier, region
        self.deadline, self.max_text = deadline, max_text
        self.aws_client, self.playwright_factory = aws_client, playwright_factory
        self.resolver, self.header_signer = resolver or _resolve, header_signer

    async def _validate_url(self, url):
        try:
            if (not isinstance(url, str) or len(url) > 8192 or "\\" in url
                    or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url)):
                raise ValueError
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or host not in self.hosts):
                raise ValueError
            try:
                ipaddress.ip_address(host)
            except ValueError:
                labels = host.split(".")
                if (len(host) > 253 or len(labels) < 2 or labels[-1].isdigit() or host.endswith(".home.arpa")
                        or labels[-1] in {"localhost", "local", "internal", "lan", "home", "onion", "invalid", "test"}
                        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
                    raise ValueError
                addresses = await self.resolver(host)
            else:
                addresses = [host]
            if not addresses or not all(_public(address) for address in addresses):
                raise ValueError
        except Exception:
            raise BrowserPolicyError("URL denied by public host policy") from None

    def _start(self, state):
        client = self.aws_client
        if client is None:
            import boto3
            from botocore.config import Config

            state["sdk"] = boto3.Session(region_name=self.region)
            client = state["sdk"].client("bedrock-agentcore", config=Config(
                connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}))
        state["client"] = client
        session = client.start_browser_session(browserIdentifier=self.identifier, sessionTimeoutSeconds=120)
        state["session"] = session
        if state.get("abandoned"):
            client.stop_browser_session(browserIdentifier=self.identifier, sessionId=session["sessionId"])
        return session

    def _headers(self, endpoint, state):
        session_id = state["session"].get("sessionId")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", session_id):
            raise BrowserError("Invalid automation session")
        host = f"bedrock-agentcore.{self.region}.amazonaws.com"
        path = f"/browser-streams/{self.identifier}/sessions/{session_id}/automation"
        if not isinstance(endpoint, str) or endpoint not in (f"wss://{host}{path}", f"wss://{host}:443{path}"):
            raise BrowserError("Invalid automation stream")
        https_url = "https" + endpoint[3:]
        if self.header_signer:
            return self.header_signer(https_url)
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        credentials = state["sdk"].get_credentials().get_frozen_credentials()
        request = AWSRequest(method="GET", url=https_url)
        SigV4Auth(credentials, "bedrock-agentcore", self.region).add_auth(request)
        return dict(request.headers.items())

    async def _acquire(self, state, key, operation):
        async def store():
            state[key] = await operation
            if state.get("abandoned") and key in {"context", "browser", "playwright"}:
                close = state[key].stop if key == "playwright" else state[key].close
                await asyncio.wait_for(close(), timeout=1)
            return state[key]
        state["pending"] = asyncio.create_task(store())
        return await asyncio.shield(state["pending"])

    async def _cleanup(self, state):
        failed = False
        pending = state.get("pending")
        if pending:
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=8)
            except BaseException:
                pass
        state["abandoned"] = True
        for key in ("context", "browser", "playwright"):
            if key in state:
                try:
                    operation = state[key].stop if key == "playwright" else state[key].close
                    await asyncio.wait_for(operation(), timeout=1)
                except BaseException:
                    failed = True
        if "session" in state:
            try:
                await asyncio.wait_for(asyncio.to_thread(state["client"].stop_browser_session,
                    browserIdentifier=self.identifier, sessionId=state["session"]["sessionId"]), timeout=8)
            except BaseException:
                failed = True
        if failed:
            raise BrowserError("Browser cleanup failed")

    async def _extract(self, url, state):
        await self._validate_url(url)
        navigation_url = _canonical_url(url)
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
        if not callable(getattr(context, "route_web_socket", None)):
            raise BrowserPolicyError("WebSocket interception is required")

        async def route_request(route):
            request = route.request
            try:
                await self._validate_url(request.url)
                headers = await request.all_headers()
                if request.method not in ("GET", "HEAD") or any(
                        name.lower() in {"authorization", "proxy-authorization", "cookie"} for name in headers):
                    raise BrowserPolicyError("Non-read-only request denied")
                await route.continue_()
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
        response = await page.goto(navigation_url, wait_until="domcontentloaded", timeout=self.deadline * 1000)
        if response is None or response.status >= 300 or response.headers.get(
                "content-type", "").split(";", 1)[0].strip().lower() not in {
                    "text/html", "application/xhtml+xml", "text/plain"}:
            raise BrowserError("Document response rejected")
        final_url = page.url
        await self._validate_url(final_url)
        if response.request.redirected_from is not None or _canonical_url(final_url) != navigation_url:
            raise BrowserPolicyError("Observed redirect rejected after navigation")
        document = await page.evaluate(_EXTRACT_SCRIPT, self.max_text)
        if (not isinstance(document, dict) or not isinstance(document.get("text"), str)
                or len(document["text"]) > self.max_text or not isinstance(document.get("title"), str)
                or len(document["title"]) > 2000 or type(document.get("truncated")) is not bool):
            raise BrowserError("Invalid bounded extraction result")
        if state.get("denied") or page.url != final_url:
            raise BrowserPolicyError("Page violated extraction policy")
        return {"title": document["title"], "requested_url": url, "final_url": final_url,
                "text": document["text"], "truncated": document["truncated"], "source_capability": CAPABILITY_STATUS}

    async def load(self, url):
        """Extract exactly one URL in a fresh session/context; never fall back."""
        state = {}
        try:
            async with asyncio.timeout(self.deadline - 20):
                return await self._extract(url, state)
        except BrowserError:
            raise
        except Exception:
            raise BrowserError("Browser extraction failed") from None
        finally:
            cleanup = asyncio.create_task(asyncio.wait_for(self._cleanup(state), timeout=20))
            cancelled = asyncio.current_task().cancelling() > 0
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            if cancelled:
                cleanup.exception()
                raise asyncio.CancelledError
            try:
                cleanup.result()
            except Exception:
                raise BrowserError("Browser cleanup failed") from None

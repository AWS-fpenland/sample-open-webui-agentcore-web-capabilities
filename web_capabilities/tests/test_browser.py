"""Offline fakes corrected to user-reported live automationStream shape; no CDP/egress verification."""

import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from web_capabilities.browser import BrowserError, BrowserFetcher, BrowserPolicyError, _EXTRACT_SCRIPT


URL = "https://example.com/page"
ENDPOINT = "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/CustomBrowser-123/sessions/session-1/automation"

def setup(endpoint=ENDPOINT, **overrides):
    events, handlers = [], {}
    response = NS(status=200, headers={"content-type": "text/html; charset=utf-8"}, request=NS(redirected_from=None))
    content = NS(text="x" * 20001)
    async def evaluate(script, limit):
        assert script == _EXTRACT_SCRIPT and "text.slice(0, limit)" in script
        return dict(title="Title", text=content.text[:limit], truncated=len(content.text) > limit)
    page = NS(url=URL, evaluate=AsyncMock(side_effect=evaluate),
              close=AsyncMock(), on=Mock(side_effect=lambda name, handler: handlers.update({name: handler})))
    async def goto(*args, **kwargs):
        assert "route" in handlers and "socket" in handlers
        return response
    page.goto = AsyncMock(side_effect=goto)
    async def install(pattern, handler):
        assert pattern == "**/*"
        handlers["route"] = handler
    async def sockets(pattern, handler): handlers["socket"] = handler
    context = NS(route=AsyncMock(side_effect=install), route_web_socket=AsyncMock(side_effect=sockets),
                 new_page=AsyncMock(return_value=page), close=AsyncMock(side_effect=lambda: events.append("context")),
                 on=Mock(side_effect=lambda name, handler: handlers.update({name: handler})))
    browser = NS(new_context=AsyncMock(return_value=context), close=AsyncMock(side_effect=lambda: events.append("browser")))
    playwright = NS(chromium=NS(connect_over_cdp=AsyncMock(return_value=browser)),
                    stop=AsyncMock(side_effect=lambda: events.append("playwright")))
    factory = Mock(return_value=NS(start=AsyncMock(return_value=playwright)))
    aws = NS(start_browser_session=Mock(return_value={"sessionId": "session-1", "streams": {
        "automationStream": {"streamEndpoint": endpoint}}}),
        stop_browser_session=Mock(side_effect=lambda **kwargs: events.append("aws")))
    signer, resolver = Mock(return_value={"Authorization": "signed"}), AsyncMock(return_value=["93.184.216.34"])
    options = dict(region="us-east-1", enabled=True, network_policy_ready=True, browser_identifier="CustomBrowser-123",
                   allowed_hosts=["example.com"], aws_client=aws, playwright_factory=factory,
                   header_signer=signer, resolver=resolver)
    options.update(overrides)
    return NS(fetcher=BrowserFetcher(**options), aws=aws, page=page, context=context, browser=browser,
              playwright=playwright, content=content, response=response, handlers=handlers, events=events,
              signer=signer, resolver=resolver)


@pytest.mark.parametrize("options", [{"enabled": False}, {"network_policy_ready": False},
    {"browser_identifier": None}, {"browser_identifier": "aws.browser.v1"}, {"allowed_hosts": []},
    {"allowed_hosts": ["*.example.com"]}, {"allowed_hosts": "example.com"}, {"deadline": 46},
    {"deadline": float("nan")}, {"deadline": 20}, {"max_text": 20001}, {"max_text": True}, {"region": None}, {"region": "us-east-1/evil"}])
def test_configuration_fails_closed(options):
    with pytest.raises(BrowserPolicyError): setup(**options)


def test_egress_attestation_defaults_false():
    with pytest.raises(BrowserPolicyError): BrowserFetcher(region="us-east-1", enabled=True, browser_identifier="CustomBrowser-123", allowed_hosts=["example.com"])

@pytest.mark.parametrize("length,endpoint", [(20000, ENDPOINT), (20001, ENDPOINT.replace(".com/", ".com:443/"))])
@pytest.mark.parametrize("requested,canonical", [(URL, URL), ("https://example.com", "https://example.com/"),
    ("https://example.com:443/", "https://example.com/"), ("HTTPS://EXAMPLE.COM/#part", "https://example.com/"),
    ("https://example.com/%2F?q=%2f#fragment", "https://example.com/%2F?q=%2f")])
def test_fresh_lifecycle_bounded_output_and_signing(length, endpoint, requested, canonical):
    fake = setup(endpoint=endpoint)
    fake.page.url = canonical
    fake.content.text = "x" * length
    async def run():
        for _ in range(2):
            result = await fake.fetcher.load(requested)
            assert result == dict(title="Title", requested_url=requested, final_url=canonical, text="x" * 20000,
                                  truncated=length > 20000, source_capability="UNSHIPPABLE")
    asyncio.run(run())
    fake.page.goto.assert_called_with(canonical, wait_until="domcontentloaded", timeout=45000)
    assert fake.events == ["context", "browser", "playwright", "aws"] * 2
    assert fake.aws.start_browser_session.call_count == fake.browser.new_context.await_count == 2
    fake.aws.start_browser_session.assert_called_with(browserIdentifier="CustomBrowser-123", sessionTimeoutSeconds=120)
    fake.aws.stop_browser_session.assert_called_with(browserIdentifier="CustomBrowser-123", sessionId="session-1")
    fake.browser.new_context.assert_called_with(service_workers="block", accept_downloads=False, permissions=[])
    fake.signer.assert_called_with("https" + endpoint[3:])
    fake.playwright.chromium.connect_over_cdp.assert_called_with(
        endpoint, headers={"Authorization": "signed"}, timeout=5000)

@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com:444",
    "https://sub.example.com", "https://example.com.evil.com", "https://127.0.0.1", "https://[::1]",
    "https://example.com./", "https://example.com/ bad", "https://example.com/\\evil", "https://example.com/" + "x" * 8192])
def test_url_denied_before_aws(url):
    fake = setup()
    with pytest.raises(BrowserPolicyError): asyncio.run(fake.fetcher.load(url))
    fake.aws.start_browser_session.assert_not_called()

@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"],
    ["fc00::1"], ["100.64.0.1"], ["224.0.0.1"], ["93.184.216.34", "192.168.1.1"], ["invalid"]])
def test_dns_denied(addresses):
    fake = setup(resolver=AsyncMock(return_value=addresses))
    with pytest.raises(BrowserPolicyError): asyncio.run(fake.fetcher.load(URL))
    fake.aws.start_browser_session.assert_not_called()

@pytest.mark.parametrize("method,url,headers,denied", [
    ("GET", URL, {}, False), ("HEAD", URL, {}, False), ("POST", URL, {}, True),
    ("GET", "https://evil.com", {}, True), ("GET", "http://example.com", {}, True),
    ("GET", URL, {"Cookie": "session=1"}, True), ("GET", URL, {"Authorization": "token"}, True)])
def test_context_routes(method, url, headers, denied):
    fake = setup()
    request = NS(method=method, url=url, all_headers=AsyncMock(return_value=headers))
    route = NS(request=request, continue_=AsyncMock(), abort=AsyncMock())
    async def goto(*args, **kwargs):
        await fake.handlers["route"](route)
        return fake.response
    fake.page.goto.side_effect = goto
    if denied:
        with pytest.raises(BrowserPolicyError): asyncio.run(fake.fetcher.load(URL))
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
    else:
        asyncio.run(fake.fetcher.load(URL))
        route.continue_.assert_awaited_once_with()
        route.abort.assert_not_awaited()

@pytest.mark.parametrize("failure", ["navigation", "connect", "context", "socket", "status", "mime", "final", "close",
    "redirect", "redirect_status", "extraction"] + [("endpoint", endpoint) for endpoint in (
    ENDPOINT + "?", ENDPOINT + "#", ENDPOINT + "?token=secret", ENDPOINT + "#fragment", ENDPOINT + "\n",
    ENDPOINT.replace("wss://", "wss://user:pass@"), ENDPOINT.replace("wss", "https", 1),
    *(ENDPOINT.replace(old, new) for old, new in [("us-east-1", "us-west-2"), ("amazonaws.com", "evil.com"),
    (".com/", ".com:444/"), ("CustomBrowser-123", "OtherBrowser-123"), ("session-1", "session-2"),
    ("automation", "other"), ("bedrock", "bed\trock"), (".com/", ".com.evil.com/")]))])
def test_failures_still_stop(failure):
    fake = setup()
    if failure == "navigation": fake.page.goto.side_effect = RuntimeError("sensitive content")
    if failure == "connect": fake.playwright.chromium.connect_over_cdp.side_effect = RuntimeError()
    if failure == "context": fake.browser.new_context.side_effect = RuntimeError()
    if failure == "socket": fake.context.route_web_socket = None
    if failure == "status": fake.response.status = 404
    if failure == "redirect_status": fake.response.status = 302
    if failure == "mime": fake.response.headers = {"content-type": "application/json"}
    if failure == "final": fake.page.url = "https://evil.com"
    if failure == "close": fake.context.close.side_effect = RuntimeError()
    if failure == "redirect": fake.response.request.redirected_from = NS(url=URL)
    if failure == "extraction": fake.page.evaluate.side_effect = lambda *args: dict(text="x" * 20001)
    if isinstance(failure, tuple): fake.aws.start_browser_session.return_value["streams"]["automationStream"]["streamEndpoint"] = failure[1]
    with pytest.raises(BrowserError) as error: asyncio.run(fake.fetcher.load(URL))
    assert "sensitive" not in str(error.value)
    fake.aws.stop_browser_session.assert_called_once()
    if isinstance(failure, tuple): assert not fake.signer.called and not fake.playwright.chromium.connect_over_cdp.called


def test_cancellation_during_start_and_navigation():
    async def run(during_start):
        fake, started, release = setup(), threading.Event(), threading.Event()
        original = fake.aws.start_browser_session.return_value
        def start(**kwargs):
            started.set()
            release.wait(2)
            return original
        async def goto(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        if during_start: fake.aws.start_browser_session.side_effect = start
        else: fake.page.goto.side_effect = goto
        task = asyncio.create_task(fake.fetcher.load(URL))
        while not started.is_set(): await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        fake.aws.stop_browser_session.assert_called_once()
    for during_start in (True, False): asyncio.run(run(during_start))


def test_fixed_remote_extraction_and_closed_events():
    fake = setup(max_text=3)
    async def run():
        assert (await fake.fetcher.load(URL))["text"] == "xxx"
        popup, download, socket = NS(close=AsyncMock()), NS(cancel=AsyncMock()), NS(close=AsyncMock())
        await fake.handlers["page"](popup)
        await fake.handlers["download"](download)
        await fake.handlers["socket"](socket)
        for operation in (popup.close, download.cancel, socket.close, fake.page.close): operation.assert_awaited_once()
    asyncio.run(run())
    fake.page.evaluate.assert_awaited_once_with(_EXTRACT_SCRIPT, 3)
    assert "document.querySelector('main') || document.querySelector('article') || document.body" in _EXTRACT_SCRIPT


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "localhost", "host.local", "host.home.arpa"])
def test_allowlisting_cannot_override_local_policy(host):
    fake = setup(allowed_hosts=[host])
    with pytest.raises(BrowserPolicyError): asyncio.run(fake.fetcher.load("https://" + host))
    fake.aws.start_browser_session.assert_not_called()


def test_deadline_still_cleans_up():
    fake = setup(deadline=20.02)
    async def goto(*args, **kwargs): await asyncio.Event().wait()
    fake.page.goto.side_effect = goto
    with pytest.raises(BrowserError): asyncio.run(fake.fetcher.load(URL))
    assert fake.events == ["context", "browser", "playwright", "aws"]

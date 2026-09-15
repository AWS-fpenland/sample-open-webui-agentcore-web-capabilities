"""Offline AWS/CDP/HTTP mocks, not proof of provisioned Browser egress isolation."""

import asyncio
import json
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from multidict import CIMultiDict

from web_capabilities import brokered_browser
from web_capabilities.brokered_browser import BrokeredBrowserFetcher, EXTRACT
from web_capabilities.browser import BrowserError, BrowserPolicyError
from web_capabilities.url_policy import URLPolicy


URL = "https://example.com/docs/page"
BROWSER_ID = "CustomBrowser-123"
MEBIBYTE = 1024 * 1024
RULES = {
    "example.com": {"exact": ["/"], "prefix": ["/docs/"]},
    "quotes.toscrape.com": {"exact": ["/"], "prefix": ["/page/"]},
    "docs.aws.amazon.com": {"prefix": ["/bedrock-agentcore/"]},
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline mocks must not resolve or connect")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def route_spec(url=URL, method="GET", resource_type="document"):
    return SimpleNamespace(url=url, method=method, resource_type=resource_type)


class MemoryContent:
    def __init__(self, body):
        self.body = body
        self.offset = 0

    async def read(self, size):
        await asyncio.sleep(0)
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class MemoryTransport:
    def __init__(self):
        self.body = b"<p>Visible</p>"
        self.headers = {"Content-Type": "text/html"}
        self.status = 200
        self.calls = []
        self.closed = 0
        self.active = 0
        self.max_active = 0

    @asynccontextmanager
    async def open(self, target, method, timeout):
        self.calls.append(target)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            yield SimpleNamespace(status=self.status, headers=CIMultiDict(self.headers),
                                  content=MemoryContent(self.body))
        finally:
            self.active -= 1
            self.closed += 1


class BrowserHarness:
    def __init__(self, *, routes=None, concurrent=False, max_text=20000):
        self.routes = routes if routes is not None else [route_spec()]
        self.concurrent = concurrent
        self.events = []
        self.runs = []
        self.text = "Visible text"
        self.title = "Page title"
        self.document_override = None
        self.navigation_status = 200
        self.navigation_type = "text/html; charset=utf-8"
        self.navigation_url = None
        self.during_navigation = None
        self.during_evaluation = None
        self.termination = "TERMINATED"
        self.start_count = 0
        self.transport = MemoryTransport()

        def start(**kwargs):
            self.start_count += 1
            self.events.append("aws.start")
            session_id = f"session-{self.start_count}"
            endpoint = (f"wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/"
                        f"{BROWSER_ID}/sessions/{session_id}/automation")
            return {"sessionId": session_id, "streams": {"automationStream": {"streamEndpoint": endpoint}},
                    "ResponseMetadata": {"RequestId": f"start-request-{self.start_count}"}}

        def stop(**kwargs):
            self.events.append("aws.stop")

        def get(**kwargs):
            self.events.append("aws.get")
            return {"status": self.termination}

        self.aws = SimpleNamespace(start_browser_session=Mock(side_effect=start),
                                   stop_browser_session=Mock(side_effect=stop),
                                   get_browser_session=Mock(side_effect=get))
        self.factory = Mock(return_value=SimpleNamespace(start=AsyncMock(side_effect=self.new_playwright)))
        self.signer = Mock(return_value={"Authorization": "mock-cdp-signature"})
        self.resolver = AsyncMock(return_value=["93.184.216.34"])
        self.fetcher = BrokeredBrowserFetcher(
            policy=URLPolicy(RULES), region="us-east-1", browser_identifier=BROWSER_ID,
            aws_client=self.aws, playwright_factory=self.factory, header_signer=self.signer,
            resolver=self.resolver, max_text=max_text,
        )
        self.fetcher.broker.resolver = AsyncMock(return_value=["93.184.216.34"])
        self.fetcher.broker.transport = self.transport
        self.fetcher.broker.fetch = AsyncMock(wraps=self.fetcher.broker.fetch)

    async def new_playwright(self):
        handlers = {}
        run = SimpleNamespace(handlers=handlers, routes=[])
        self.runs.append(run)

        async def install_route(pattern, callback):
            assert pattern == "**/*"
            handlers["route"] = callback

        async def install_socket(pattern, callback):
            assert pattern == "**/*"
            handlers["socket"] = callback

        async def dispatch(specification):
            route = SimpleNamespace(request=specification, fulfill=AsyncMock(), abort=AsyncMock(),
                                    continue_=AsyncMock(side_effect=AssertionError("Forbidden continue")),
                                    fetch=AsyncMock(side_effect=AssertionError("Forbidden route.fetch")))
            run.routes.append(route)
            await handlers["route"](route)

        async def goto(url, **kwargs):
            assert "route" in handlers and "socket" in handlers
            run.page.url = self.navigation_url or url
            if self.concurrent:
                await asyncio.gather(*[dispatch(specification) for specification in self.routes])
            else:
                for specification in self.routes:
                    await dispatch(specification)
            if self.during_navigation:
                await self.during_navigation(run)
            return SimpleNamespace(status=self.navigation_status, headers={"content-type": self.navigation_type})

        async def evaluate(script, limit):
            assert script == EXTRACT
            if self.during_evaluation:
                await self.during_evaluation(run)
            if self.document_override is not None:
                return self.document_override
            return {"title": self.title[:2000], "text": self.text[:limit],
                    "truncated": len(self.text) > limit, "links": []}

        run.page = SimpleNamespace(
            url=URL, goto=AsyncMock(side_effect=goto), evaluate=AsyncMock(side_effect=evaluate),
            wait_for_timeout=AsyncMock(), close=AsyncMock(),
            on=Mock(side_effect=lambda name, callback: handlers.update({name: callback})),
        )
        run.context = SimpleNamespace(
            route=AsyncMock(side_effect=install_route), route_web_socket=AsyncMock(side_effect=install_socket),
            new_page=AsyncMock(return_value=run.page),
            close=AsyncMock(side_effect=lambda: self.events.append("context.close")),
            on=Mock(side_effect=lambda name, callback: handlers.update({name: callback})),
        )
        run.browser = SimpleNamespace(
            new_context=AsyncMock(return_value=run.context),
            close=AsyncMock(side_effect=lambda: self.events.append("browser.close")),
        )
        run.playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=run.browser)),
            stop=AsyncMock(side_effect=lambda: self.events.append("playwright.stop")),
        )
        run.dispatch = dispatch
        return run.playwright

    def assert_routes_closed(self):
        for run in self.runs:
            for route in run.routes:
                route.continue_.assert_not_awaited()
                route.fetch.assert_not_awaited()
                assert route.fulfill.await_count + route.abort.await_count == 1

    def assert_cleanup(self):
        assert self.events[-5:] == ["context.close", "browser.close", "playwright.stop", "aws.stop", "aws.get"]
        self.aws.get_browser_session.assert_called_with(
            browserIdentifier=BROWSER_ID, sessionId=f"session-{self.start_count}",
        )


def test_fresh_contexts_fulfill_only_and_confirm_termination():
    fake = BrowserHarness(routes=[route_spec(), route_spec("https://example.com/docs/app.js", resource_type="script")])

    async def run():
        for index in range(2):
            document = await fake.fetcher.load(URL)
            assert document["session_terminated"] is True
            assert document["source_capability"] == "agentcore-browser-brokered"
            assert document["broker_requests"] == 2
            assert document["broker_bytes"] == 2 * len(fake.transport.body)
            assert document["blocked_optional_resources"] == 0
            assert document["aws_start_request_id"] == f"start-request-{index + 1}"

    asyncio.run(run())
    assert fake.runs[0].context is not fake.runs[1].context
    for current in fake.runs:
        current.browser.new_context.assert_awaited_once_with(
            service_workers="block", accept_downloads=False, permissions=[],
        )
        current.context.route.assert_awaited_once()
        current.context.route_web_socket.assert_awaited_once()
        current.page.goto.assert_awaited_once_with(URL, wait_until="domcontentloaded", timeout=15000)
        current.page.evaluate.assert_awaited_once_with(EXTRACT, 20000)
        current.routes[0].fulfill.assert_awaited_once_with(
            status=200, body=fake.transport.body, headers={
                "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
                "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
            },
        )
    calls = fake.fetcher.broker.fetch.await_args_list
    assert calls[0].kwargs["budget"] is calls[1].kwargs["budget"]
    assert calls[2].kwargs["budget"] is calls[3].kwargs["budget"]
    assert calls[0].kwargs["budget"] is not calls[2].kwargs["budget"]
    assert all(call.kwargs["follow_redirects"] is False for call in calls)
    assert fake.aws.stop_browser_session.call_count == fake.aws.get_browser_session.call_count == 2
    fake.assert_routes_closed()
    fake.assert_cleanup()


@pytest.mark.parametrize("url", [
    "https://example.com/admin", "https://example.com/docs/page?search=secret", "https://example.com/docs/page?",
    "https://example.com/docs/../admin", "https://example.com/docs/%2e%2e/admin", "https://other.example.com/",
    "https://127.0.0.1/", "https://user@example.com/docs/page", "http://example.com/docs/page",
])
def test_top_level_policy_denies_before_aws_or_dns(url):
    fake = BrowserHarness()
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(url))
    fake.aws.start_browser_session.assert_not_called()
    fake.resolver.assert_not_awaited()
    fake.fetcher.broker.fetch.assert_not_awaited()


@pytest.mark.parametrize("url", ["https://example.com/admin", "https://example.com/docs/app.js?v=1",
                                  "https://example.com/docs/app.js?", "https://evil.com/script.js"])
def test_denied_subresources_abort_and_make_extraction_fail(url):
    fake = BrowserHarness(routes=[route_spec(), route_spec(url, resource_type="script")])
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    assert fake.fetcher.broker.fetch.await_count == 1
    fake.runs[0].routes[1].abort.assert_awaited_once_with("blockedbyclient")
    fake.runs[0].page.evaluate.assert_not_awaited()
    fake.assert_routes_closed()
    fake.assert_cleanup()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "HEAD", "OPTIONS", "CONNECT"])
def test_denied_methods_are_honest_errors(method):
    fake = BrowserHarness(routes=[route_spec(method=method, resource_type="fetch")])
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.fetcher.broker.fetch.assert_not_awaited()
    fake.assert_routes_closed()
    fake.assert_cleanup()


@pytest.mark.parametrize("resource_type", ["image", "media", "font"])
def test_optional_resources_are_aborted_with_explicit_partial_metadata(resource_type):
    fake = BrowserHarness(routes=[route_spec(), route_spec("https://external.invalid/asset", resource_type=resource_type)])
    document = asyncio.run(fake.fetcher.load(URL))
    assert document["blocked_optional_resources"] == 1 and document["broker_requests"] == 1
    fake.runs[0].routes[1].abort.assert_awaited_once_with("blockedbyclient")
    fake.assert_routes_closed()


@pytest.mark.parametrize("resource_type", ["image", "media", "font"])
@pytest.mark.parametrize("method", ["POST", "HEAD", "PUT"])
def test_optional_resource_label_cannot_hide_a_denied_method(resource_type, method):
    fake = BrowserHarness(routes=[route_spec(), route_spec(method=method, resource_type=resource_type)])
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.assert_routes_closed()


@pytest.mark.parametrize("route_count, succeeds", [(16, True), (17, False)])
def test_one_shared_sixteen_request_budget(route_count, succeeds):
    fake = BrowserHarness(routes=[route_spec() for index in range(route_count)], concurrent=True)
    assert fake.fetcher.broker.max_response_bytes == MEBIBYTE
    if succeeds:
        assert asyncio.run(fake.fetcher.load(URL))["broker_requests"] == 16
    else:
        with pytest.raises(BrowserError):
            asyncio.run(fake.fetcher.load(URL))
    budgets = [call.kwargs["budget"] for call in fake.fetcher.broker.fetch.await_args_list]
    assert len({id(budget) for budget in budgets}) == 1
    assert budgets[0].request_count == 16
    assert len(fake.transport.calls) == 16
    assert fake.transport.max_active <= 3
    fake.assert_routes_closed()
    fake.assert_cleanup()


def test_page_budget_configuration_includes_total_deadline(monkeypatch):
    factory = Mock(wraps=brokered_browser.FetchBudget)
    monkeypatch.setattr(brokered_browser, "FetchBudget", factory)
    fake = BrowserHarness()
    asyncio.run(fake.fetcher.load(URL))
    factory.assert_called_once_with(max_requests=16, max_bytes=3 * MEBIBYTE, total_timeout=24)
    assert fake.fetcher.broker.total_timeout == 10


@pytest.mark.parametrize("route_count, body_size, succeeds", [(3, MEBIBYTE, True), (4, MEBIBYTE, False), (1, MEBIBYTE + 1, False)])
def test_one_mebibyte_responses_and_three_mebibyte_page_budget(route_count, body_size, succeeds):
    fake = BrowserHarness(routes=[route_spec() for index in range(route_count)], concurrent=True)
    fake.transport.body = b"a" * body_size
    if succeeds:
        assert asyncio.run(fake.fetcher.load(URL))["broker_bytes"] == 3 * MEBIBYTE
    else:
        with pytest.raises(BrowserError):
            asyncio.run(fake.fetcher.load(URL))
    budgets = [call.kwargs["budget"] for call in fake.fetcher.broker.fetch.await_args_list]
    assert len({id(budget) for budget in budgets}) == 1
    assert budgets[0].total_bytes <= 3 * MEBIBYTE
    fake.assert_routes_closed()
    fake.assert_cleanup()


def test_all_route_events_are_bounded_even_when_optional():
    fake = BrowserHarness(routes=[route_spec(resource_type="image") for index in range(33)])
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.fetcher.broker.fetch.assert_not_awaited()
    fake.assert_routes_closed()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_abort_without_second_fetch(status):
    fake = BrowserHarness()
    fake.transport.status = status
    fake.transport.headers = {"Location": "https://example.com/docs/other"}
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    assert len(fake.transport.calls) == 1
    fake.assert_routes_closed()
    fake.assert_cleanup()


def test_redirect_capable_injected_broker_is_rejected():
    with pytest.raises(BrowserPolicyError):
        BrokeredBrowserFetcher(policy=URLPolicy(RULES), region="us-east-1", browser_identifier=BROWSER_ID,
                               broker=SimpleNamespace(max_redirects=5))


@pytest.mark.parametrize("status", [204, 400, 401, 404, 500])
def test_failed_http_responses_cannot_be_fulfilled_as_success(status):
    fake = BrowserHarness()
    fake.transport.status = status
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.runs[0].routes[0].fulfill.assert_not_awaited()
    fake.assert_routes_closed()
    fake.assert_cleanup()


def test_unexpected_broker_final_url_cannot_be_fulfilled():
    fake = BrowserHarness()
    fake.fetcher.broker.fetch.side_effect = None
    fake.fetcher.broker.fetch.return_value = SimpleNamespace(
        status=200, final_url="https://example.com/docs/other", body=b"body", content_type="text/html",
    )
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.runs[0].routes[0].fulfill.assert_not_awaited()
    fake.assert_routes_closed()


@pytest.mark.parametrize("status", ["ACTIVE", "STOPPING", "READY", None])
def test_stop_is_not_reported_as_termination_without_get_confirmation(status):
    fake = BrowserHarness()
    fake.termination = status
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.aws.stop_browser_session.assert_called_once()
    fake.aws.get_browser_session.assert_called_once()


def test_get_failure_cannot_claim_termination():
    fake = BrowserHarness()
    fake.aws.get_browser_session.side_effect = RuntimeError("sensitive AWS content")
    with pytest.raises(BrowserError) as failure:
        asyncio.run(fake.fetcher.load(URL))
    assert "sensitive" not in str(failure.value)
    fake.aws.stop_browser_session.assert_called_once()
    fake.aws.get_browser_session.assert_called_once()


@pytest.mark.parametrize("resource", ["context", "browser", "playwright", "aws"])
def test_cleanup_failures_prevent_success_and_attempt_remaining_cleanup(resource):
    fake = BrowserHarness()

    async def fail_cleanup(run):
        operation = (fake.aws.stop_browser_session if resource == "aws"
                     else run.playwright.stop if resource == "playwright"
                     else getattr(run, resource).close)
        operation.side_effect = RuntimeError("private cleanup detail")

    fake.during_navigation = fail_cleanup
    with pytest.raises(BrowserError) as failure:
        asyncio.run(fake.fetcher.load(URL))
    assert "private" not in str(failure.value)
    fake.runs[0].context.close.assert_awaited_once()
    fake.runs[0].browser.close.assert_awaited_once()
    fake.runs[0].playwright.stop.assert_awaited_once()
    fake.aws.stop_browser_session.assert_called_once()


@pytest.mark.parametrize("event_name", ["socket", "page", "download"])
def test_websockets_popups_and_downloads_close_and_fail_honestly(event_name):
    fake = BrowserHarness()
    attempted = SimpleNamespace(close=AsyncMock(), cancel=AsyncMock())

    async def trigger(run):
        await run.handlers[event_name](attempted)

    fake.during_navigation = trigger
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    if event_name == "download":
        attempted.cancel.assert_awaited_once()
        fake.runs[0].page.close.assert_awaited_once()
    else:
        attempted.close.assert_awaited_once()
    fake.assert_cleanup()


@pytest.mark.parametrize("stage", ["navigation", "evaluation"])
def test_policy_is_rechecked_after_navigation_and_extraction(stage):
    fake = BrowserHarness()

    async def change_url(run):
        run.page.url = "https://example.com/docs/other"

    if stage == "navigation":
        fake.during_navigation = change_url
    else:
        fake.during_evaluation = change_url
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.assert_cleanup()


def test_late_denied_route_during_extraction_is_not_reported_as_success():
    fake = BrowserHarness()

    async def denied_request(run):
        await run.dispatch(route_spec("https://example.com/admin", resource_type="fetch"))

    fake.during_evaluation = denied_request
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.assert_routes_closed()
    fake.assert_cleanup()


def test_cancellation_still_stops_and_gets_termination():
    async def run():
        fake = BrowserHarness()
        started = asyncio.Event()

        async def stall(current):
            started.set()
            await asyncio.Future()

        fake.during_navigation = stall
        task = asyncio.create_task(fake.fetcher.load(URL))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        fake.assert_cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"text": "a" * 20001}, {"title": "a" * 2001}, {"text": None}, {"title": None},
    {"truncated": 1}, {"links": ["https://example.com/"] * 21}, {"links": [123]},
    {"links": ["https://example.com/" + "a" * 2048]}, {"links": "not-a-list"},
])
def test_malformed_or_unbounded_js_results_are_rejected(changes):
    fake = BrowserHarness()
    fake.document_override = {"title": "Title", "text": "Text", "truncated": False, "links": [], **changes}
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.assert_cleanup()


def test_bounded_extraction_script_on_local_mock_dom():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is optional for this offline mock-DOM script check")
    javascript = """
const vm = require('node:vm');
const fs = require('node:fs');
const script = JSON.parse(fs.readFileSync(0, 'utf8'));
const anchors = [{href: 'http://denied.invalid/'}, {href: 'https://' + 'a'.repeat(2048)}];
for (let index = 0; index < 40; index++) anchors.push({href: 'https://example.com/docs/' + index});
anchors.splice(3, 0, anchors[2]);
const root = {innerText: 'a'.repeat(20001), querySelectorAll: () => anchors};
const document = {title: 'b'.repeat(2001), querySelector: () => root, body: root};
const result = vm.runInNewContext('(' + script + ')(20000)', {document}, {timeout: 1000});
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run([executable, "-e", javascript], input=json.dumps(EXTRACT),
                               text=True, capture_output=True, check=True, timeout=5)
    document = json.loads(completed.stdout)
    assert len(document["text"]) == 20000 and len(document["title"]) == 2000
    assert document["truncated"] is True
    assert document["links"] == [f"https://example.com/docs/{index}" for index in range(20)]


@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=iso-8859-1", "text/plain"])
def test_invalid_utf8_is_aborted_not_replaced_or_claimed_successful(content_type):
    fake = BrowserHarness()
    fake.transport.headers = {"Content-Type": content_type}
    fake.transport.body = b"private\xffcontent"
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.runs[0].routes[0].fulfill.assert_not_awaited()
    fake.assert_routes_closed()
    fake.assert_cleanup()


@pytest.mark.parametrize("content_type", [
    "text/html; charset=utf-8", 'Text/HTML; charset="UTF-8"',
    "text/html; charset=utf8", 'text/html; version=5; charset="utf-8"',
])
def test_utf8_content_type_is_not_duplicated_and_origin_headers_are_not_forwarded(content_type):
    fake = BrowserHarness()
    fake.transport.body = "Café".encode("utf-8")
    fake.transport.headers = {
        "Content-Type": content_type, "Set-Cookie": "session=private",
        "Content-Security-Policy": "default-src *", "Refresh": "0;url=https://evil.com/",
    }
    asyncio.run(fake.fetcher.load(URL))
    headers = fake.runs[0].routes[0].fulfill.await_args.kwargs["headers"]
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert set(headers) == {"content-type", "cache-control", "x-content-type-options", "referrer-policy"}


@pytest.mark.parametrize("content_type", [
    "text/html; charset=iso-8859-1", "text/html; charset=utf-16", "text/html; charset=us-ascii",
    "text/html; charset=", "text/html; charset=utf-8; charset=utf-8",
    "text/html; charset=utf-8; charset=iso-8859-1",
])
def test_declared_non_utf8_or_duplicate_charsets_are_not_relabelled_even_for_ascii(content_type):
    fake = BrowserHarness()
    fake.transport.headers = {"Content-Type": content_type}
    fake.transport.body = b"ASCII bytes are valid UTF-8 but do not authorize relabelling"
    with pytest.raises(BrowserError):
        asyncio.run(fake.fetcher.load(URL))
    fake.runs[0].routes[0].fulfill.assert_not_awaited()
    fake.assert_routes_closed()
    fake.assert_cleanup()

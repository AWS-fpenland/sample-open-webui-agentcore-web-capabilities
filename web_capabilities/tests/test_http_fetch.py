"""Offline security tests: every OS DNS/connect entry point fails closed."""

import asyncio
import socket
import ssl
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from multidict import CIMultiDict

from web_capabilities.http_fetch import (
    ALLOWED_CONTENT_TYPES, AiohttpPinnedTransport, FetchBudget, FetchError,
    FetchLimitError, FetchPolicyError, PinnedTarget, PublicHTTPSFetcher,
    _PinnedResolver, resolve_public_host,
)


PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:4700:4700::1111"
URL = "https://example.com/page"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must never resolve or connect to the network")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


class FakeContent:
    def __init__(self, chunks=(), before_read=None):
        self.chunks = deque(chunks)
        self.before_read = before_read
        self.read_sizes = []
        self.delivered = 0

    async def read(self, size):
        self.read_sizes.append(size)
        if self.before_read is not None:
            before_read, self.before_read = self.before_read, None
            await before_read()
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
            chunk = chunk[:size]
        self.delivered += len(chunk)
        return chunk


def response(*, status=200, headers=None, chunks=(b"hello",), before_read=None):
    return SimpleNamespace(
        status=status,
        headers=CIMultiDict({"Content-Type": "text/html; charset=utf-8"} if headers is None else headers),
        content=FakeContent(chunks, before_read),
    )


class FakeTransport:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []
        self.closed = 0

    @asynccontextmanager
    async def open(self, target, method, timeout):
        self.calls.append(SimpleNamespace(target=target, method=method, timeout=timeout))
        current = self.responses.popleft()
        try:
            yield current
        finally:
            self.closed += 1


def setup(*responses, **options):
    resolver = options.pop("resolver", AsyncMock(return_value=[PUBLIC_IPV4]))
    transport = FakeTransport(responses or (response(),))
    fetcher = PublicHTTPSFetcher(
        allowed_hosts=("example.com", "assets.example.com"), resolver=resolver,
        transport=transport, **options,
    )
    return SimpleNamespace(fetcher=fetcher, resolver=resolver, transport=transport)


@pytest.mark.parametrize("url", [
    "http://example.com/", "https://example.com:80/", "https://example.com:444/",
    "https://example.com:0443/", "https://example.com:/", "https://user@example.com/",
    "https://user:password@example.com/", "https://@example.com/", "https://example.com@evil.com/",
    "https://example.com.evil.com/", "https://sub.example.com/", "https://example.com./",
    "https://127.0.0.1/", "https://[::1]/", "https://[2606:4700:4700::1111]/",
    "https://2130706433/", "https://0x7f000001/", "https://127.1/",
    "https://0x7f.0x0.0x0.0x1/", "https://example.com\\@evil.com/",
    " https://example.com/", "https://example.com/\r\nX:1", "https://example.com/\tfoo",
    "https://example.com/\x7f", "https://example.com/%00", "https://example.com/%0a",
    "https://example.com/%1F", "https://example.com/%7f", "https://example.com/%5cfoo",
    "https://example.com/%zz", "https://example.com/%", "https://example.com/é",
    "https://exаmple.com/", "https://%65xample.com/", "https:///example.com/",
    "https:example.com/", "//example.com/", "https://[bad/", "",
    "https://example.com/" + "a" * 8192,
])
def test_url_rejected_before_dns_or_transport(url):
    fake = setup()
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(url, budget=FetchBudget()))
    fake.resolver.assert_not_awaited()
    assert fake.transport.calls == []


@pytest.mark.parametrize("hosts", [[], ["*.example.com"], ["example.com."], ["127.0.0.1"],
                                       ["0x7f.0x0.0x0.0x1"], ["localhost"], ["éxample.com"],
                                       ["example.com:443"], "example.com"])
def test_invalid_exact_host_configuration(hosts):
    with pytest.raises((ValueError, FetchPolicyError)):
        PublicHTTPSFetcher(allowed_hosts=hosts)


@pytest.mark.parametrize("options", [{"max_response_bytes": 0}, {"max_response_bytes": True},
    {"total_timeout": 0}, {"total_timeout": float("nan")}, {"total_timeout": float("inf")},
    {"max_redirects": 6}, {"max_redirects": -1}, {"max_redirects": True}])
def test_invalid_fetch_limits(options):
    with pytest.raises(ValueError):
        setup(**options)


@pytest.mark.parametrize("options", [{"max_requests": 0}, {"max_requests": True},
    {"max_bytes": -1}, {"max_bytes": 1.5}, {"total_timeout": float("nan")},
    {"total_timeout": float("inf")}, {"total_timeout": 0}])
def test_invalid_page_limits(options):
    with pytest.raises(ValueError):
        FetchBudget(**options)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "get"])
def test_only_get_and_head(method):
    fake = setup()
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget(), method=method))
    fake.resolver.assert_not_awaited()


@pytest.mark.parametrize("option", ["headers", "cookies", "auth", "proxy"])
def test_caller_credentials_and_headers_are_not_api_options(option):
    fake = setup()
    with pytest.raises(TypeError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget(), **{option: {"secret": "value"}}))
    fake.resolver.assert_not_awaited()


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254",
    "100.100.100.200", "0.0.0.0", "192.0.0.9", "192.88.99.1", "192.0.2.1",
    "198.18.0.1", "198.51.100.1", "203.0.113.1", "224.0.0.1", "240.0.0.1",
    "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "ff0e::1",
    "2001:db8::1", "3fff::1", "::ffff:93.184.216.34", "64:ff9b::7f00:1",
    "64:ff9b:1::1", "2002:7f00:1::", "2001::1", "2606:4700:4700::1111%eth0",
    "not-an-ip", "127.1", 123,
])
def test_every_dns_answer_must_be_public(address):
    fake = setup(resolver=AsyncMock(return_value=[PUBLIC_IPV4, address]))
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))
    assert fake.transport.calls == []


@pytest.mark.parametrize("answers", [[], None, PUBLIC_IPV4, [PUBLIC_IPV4] * 65])
def test_missing_or_excessive_dns_answers(answers):
    fake = setup(resolver=AsyncMock(return_value=answers))
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))
    assert not fake.transport.calls


def test_structured_result_and_exact_pinned_snapshot():
    fake = setup(resolver=AsyncMock(return_value=[PUBLIC_IPV4, PUBLIC_IPV6, PUBLIC_IPV4]))
    budget = FetchBudget()
    result = asyncio.run(fake.fetcher.fetch(
        "HTTPS://EXAMPLE.COM:443/%2F?q=%2f#fragment", budget=budget,
    ))
    assert result.body == b"hello"
    assert result.content_type == "text/html; charset=utf-8"
    assert result.final_url == "https://example.com/%2F?q=%2f"
    assert result.status == 200
    assert result.redirect_chain == () and result.location is None
    assert fake.transport.calls[0].target.addresses == (PUBLIC_IPV4, PUBLIC_IPV6)
    assert fake.transport.calls[0].target.host == "example.com"
    assert fake.transport.calls[0].method == "GET"
    assert budget.requests_used == 1 and budget.bytes_used == 5
    assert budget.request_count == 1 and budget.total_bytes == 5
    assert fake.transport.closed == 1


@pytest.mark.parametrize("content_type", sorted(ALLOWED_CONTENT_TYPES))
def test_explicit_mime_allowlist(content_type):
    fake = setup(response(headers={"Content-Type": content_type}))
    assert asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget())).content_type == content_type


def test_fulfillment_result_excludes_all_other_upstream_headers():
    fake = setup(response(headers={
        "Content-Type": "text/html", "Set-Cookie": "session=secret",
        "Content-Security-Policy": "default-src *", "Refresh": "0; url=https://evil.com/",
        "WWW-Authenticate": "Basic", "X-Secret": "sensitive",
    }))
    result = asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))
    assert vars(result) == dict(body=b"hello", content_type="text/html", final_url=URL,
                                status=200, redirect_chain=(), location=None)


@pytest.mark.parametrize("disposition", ["inline", "INLINE; filename=jquery.js", 'inline; filename="main.css"'])
def test_inline_assets_are_read_without_forwarding_filename_metadata(disposition):
    fake = setup(response(headers={"Content-Type": "application/javascript; charset=utf-8",
                                   "Content-Disposition": disposition}))
    result = asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))
    assert result.body == b"hello"
    assert set(vars(result)) == {"body", "content_type", "final_url", "status", "redirect_chain", "location"}


@pytest.mark.parametrize("headers", [
    {}, {"Content-Type": "application/octet-stream"}, {"Content-Type": "image/png"},
    {"Content-Type": "application/pdf"}, {"Content-Type": "application/problem+json"},
    {"Content-Type": "multipart/byteranges"}, {"Content-Type": "text/html, text/plain"},
    {"Content-Type": "text/html", "Content-Disposition": "attachment; filename=x.html"},
    {"Content-Type": "text/html", "Content-Disposition": "inline, attachment"},
    {"Content-Type": "text/html", "Content-Disposition": "attachment"},
    {"Content-Type": "text/html", "Content-Disposition": "unknown; filename=x.html"},
    {"Content-Type": "text/html", "Content-Disposition": ""},
    [("Content-Type", "text/html"), ("Content-Disposition", "inline"), ("Content-Disposition", "attachment")],
    {"Content-Type": "text/html", "Content-Encoding": "gzip"},
    {"Content-Type": "text/html", "Content-Encoding": "br"},
    {"Content-Type": "text/html", "Content-Encoding": "identity, gzip"},
    {"Content-Type": "text/html", "Content-Encoding": ""},
    [("Content-Type", "text/html"), ("content-type", "application/json")],
    [("Content-Type", "text/html"), ("Content-Encoding", "identity"), ("Content-Encoding", "identity")],
    {"Content-Type": "text/html\r\nSet-Cookie: evil"},
    {"Content-Type": "text/html", "Content-Length": "-1"},
    {"Content-Type": "text/html", "Content-Length": "1, 2"},
    {"Content-Type": "text/html", "Content-Length": "9" * 30},
    [("Content-Type", "text/html"), ("Content-Length", "1"), ("Content-Length", "1")],
    {"Content-Type": "text/html", "Transfer-Encoding": "gzip, chunked"},
    {"Content-Type": "text/html", "Transfer-Encoding": "chunked", "Content-Length": "1"},
])
def test_bad_metadata_rejected_without_body_read(headers):
    current = response(headers=headers)
    fake = setup(current)
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))
    assert current.content.read_sizes == [] and fake.transport.closed == 1


def test_declared_oversize_rejected_without_body_read():
    current = response(headers={"Content-Type": "text/plain", "Content-Length": "6"})
    fake = setup(current, max_response_bytes=5)
    with pytest.raises(FetchLimitError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))
    assert current.content.delivered == 0


@pytest.mark.parametrize("declared", [None, "1"])
def test_stream_limit_does_not_trust_content_length(declared):
    headers = {"Content-Type": "text/plain"}
    if declared:
        headers["Content-Length"] = declared
    current = response(headers=headers, chunks=[b"abc", b"d" * 1000000])
    fake = setup(current, max_response_bytes=5)
    budget = FetchBudget()
    with pytest.raises(FetchLimitError):
        asyncio.run(fake.fetcher.fetch(URL, budget=budget))
    assert current.content.delivered == 6
    assert current.content.read_sizes == [6, 3]
    assert budget.bytes_used == 6 and fake.transport.closed == 1


def test_exact_size_succeeds_and_head_never_reads():
    first = response(headers={"Content-Type": "text/plain", "Content-Encoding": "identity"})
    second = response()
    fake = setup(first, second, max_response_bytes=5)
    budget = FetchBudget(max_bytes=5)

    async def run():
        assert (await fake.fetcher.fetch(URL, budget=budget)).body == b"hello"
        assert (await fake.fetcher.fetch(URL, budget=budget, method="HEAD")).body == b""

    asyncio.run(run())
    assert budget.bytes_used == 5 and budget.requests_used == 2
    assert first.content.read_sizes == [6, 1]
    assert second.content.read_sizes == []


@pytest.mark.parametrize("status", [101, 206, 300, 304, 305, 399, 600])
def test_unsupported_status(status):
    fake = setup(response(status=status))
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_repin_each_hop_and_preserve_get_or_head(status):
    first = response(status=status, headers={"Location": "https://assets.example.com/a"})
    second = response(status=302, headers={"Location": "../b"})
    fake = setup(first, second, response(), resolver=AsyncMock(side_effect=[
        [PUBLIC_IPV4], [PUBLIC_IPV6], ["1.1.1.1"],
    ]))
    budget = FetchBudget()
    result = asyncio.run(fake.fetcher.fetch(URL, budget=budget, method="HEAD"))
    assert result.final_url == "https://assets.example.com/b"
    assert [hop.status for hop in result.redirect_chain] == [status, 302]
    assert result.redirect_chain[0].source_url == URL
    assert result.redirect_chain[1].source_url == "https://assets.example.com/a"
    assert [call.target.addresses for call in fake.transport.calls] == [
        (PUBLIC_IPV4,), (PUBLIC_IPV6,), ("1.1.1.1",),
    ]
    assert all(call.method == "HEAD" for call in fake.transport.calls)
    assert first.content.delivered == second.content.delivered == 0
    assert budget.requests_used == 3 and fake.transport.closed == 3


@pytest.mark.parametrize("follow", [True, False])
@pytest.mark.parametrize("location", [
    "http://example.com/", "https://evil.com/", "https://127.0.0.1/",
    "https://example.com:444/", "https://user@example.com/", "\\evil.com",
    "\thttps://example.com/", "/%0a", "///evil.com/", "https:relative", "https://[bad/", "",
])
def test_redirect_denied_before_next_connection(location, follow):
    fake = setup(response(status=302, headers={"Location": location}))
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget(), follow_redirects=follow))
    assert len(fake.transport.calls) == 1 and fake.transport.closed == 1


@pytest.mark.parametrize("headers", [{}, [("Location", "/a"), ("Location", "/b")]])
def test_missing_or_duplicate_redirect_location(headers):
    fake = setup(response(status=302, headers=headers))
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))


@pytest.mark.parametrize("follow", [True, False])
def test_rebinding_even_on_same_host_redirect_is_denied(follow):
    fake = setup(response(status=302, headers={"Location": "/private"}),
                 resolver=AsyncMock(side_effect=[[PUBLIC_IPV4], ["127.0.0.1"]]))
    with pytest.raises(FetchPolicyError):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget(), follow_redirects=follow))
    assert fake.resolver.await_count == 2 and len(fake.transport.calls) == 1


def test_non_following_returns_only_validated_redirect_metadata():
    first = response(status=302, headers={"Location": "//assets.example.com:443/new#fragment",
                                         "Set-Cookie": "secret=1", "Refresh": "0;url=http://evil.com"})
    fake = setup(first, response(), resolver=AsyncMock(side_effect=[
        [PUBLIC_IPV4], [PUBLIC_IPV6], [PUBLIC_IPV4],
    ]))
    budget = FetchBudget()

    async def run():
        result = await fake.fetcher.fetch(URL, budget=budget, follow_redirects=False)
        assert result.status == 302 and result.final_url == URL
        assert result.body == b"" and result.content_type is None
        assert result.location == "https://assets.example.com/new"
        assert result.redirect_chain[0].target_url == result.location
        assert not hasattr(result, "headers")
        assert budget.requests_used == 1 and fake.resolver.await_count == 2
        await fake.fetcher.fetch(result.location, budget=budget, follow_redirects=False)

    asyncio.run(run())
    assert len(fake.transport.calls) == 2
    assert fake.transport.calls[1].target.addresses == (PUBLIC_IPV4,)
    assert first.content.read_sizes == []


def test_five_redirects_allowed_but_sixth_denied():
    redirects = [response(status=302, headers={"Location": "/next"}) for index in range(6)]
    successful = setup(*redirects[:5], response())
    result = asyncio.run(successful.fetcher.fetch(URL, budget=FetchBudget()))
    assert len(result.redirect_chain) == 5 and len(successful.transport.calls) == 6
    failing = setup(*redirects)
    with pytest.raises(FetchLimitError, match="Redirect"):
        asyncio.run(failing.fetcher.fetch(URL, budget=FetchBudget()))
    assert len(failing.transport.calls) == 6 and failing.resolver.await_count == 6


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("follow", [True, False])
def test_zero_redirect_mvp_never_returns_or_follows_redirects(status, follow):
    current = response(status=status, headers={"Location": "https://assets.example.com/next"})
    fake = setup(current, max_redirects=0)
    budget = FetchBudget()
    with pytest.raises(FetchLimitError, match="Redirect limit"):
        asyncio.run(fake.fetcher.fetch(URL, budget=budget, follow_redirects=follow))
    assert len(fake.transport.calls) == 1 and fake.resolver.await_count == 1
    assert current.content.read_sizes == [] and fake.transport.closed == 1
    assert budget.request_count == 1 and budget.total_bytes == 0


def test_shared_request_count_covers_redirects_and_failed_dns():
    fake = setup(response(status=302, headers={"Location": "/next"}))
    budget = FetchBudget(max_requests=1)
    with pytest.raises(FetchLimitError, match="request"):
        asyncio.run(fake.fetcher.fetch(URL, budget=budget))
    assert budget.requests_used == 1 and fake.resolver.await_count == 1
    failing = setup(resolver=AsyncMock(return_value=["127.0.0.1"]))
    failed_budget = FetchBudget(max_requests=1)
    with pytest.raises(FetchPolicyError):
        asyncio.run(failing.fetcher.fetch(URL, budget=failed_budget))
    assert failed_budget.requests_used == 1


def test_concurrent_request_budget_is_atomic():
    fake = setup(*[response() for index in range(3)])
    budget = FetchBudget(max_requests=3)

    async def run():
        return await asyncio.gather(*[
            fake.fetcher.fetch(URL, budget=budget) for index in range(20)
        ], return_exceptions=True)

    results = asyncio.run(run())
    assert sum(isinstance(result, FetchLimitError) for result in results) == 17
    assert budget.requests_used == 3 and len(fake.transport.calls) == 3


def test_shared_budget_allows_fanout_within_limits():
    async def run():
        barrier = asyncio.Barrier(3)
        fake = setup(*[response(before_read=barrier.wait) for index in range(3)])
        budget = FetchBudget(max_bytes=15, max_requests=3)
        results = await asyncio.gather(*[fake.fetcher.fetch(URL, budget=budget) for index in range(3)])
        assert all(result.body == b"hello" for result in results)
        assert budget.request_count == 3 and budget.total_bytes == 15
        assert fake.transport.closed == 3

    asyncio.run(run())


def test_concurrent_page_bytes_cannot_be_double_spent():
    async def run():
        barrier = asyncio.Barrier(2)
        responses = [response(chunks=[b"12345"], before_read=barrier.wait),
                     response(chunks=[b"67890"], before_read=barrier.wait)]
        fake = setup(*responses)
        budget = FetchBudget(max_bytes=7)
        results = await asyncio.gather(*[fake.fetcher.fetch(URL, budget=budget)
                                         for current in responses], return_exceptions=True)
        assert sum(isinstance(result, FetchLimitError) for result in results) == 1
        assert budget.bytes_used == 5 and budget.bytes_remaining == 0
        assert fake.transport.closed == 2
        with pytest.raises(FetchLimitError):
            await fake.fetcher.fetch(URL, budget=budget)

    asyncio.run(run())


def test_failed_response_bytes_are_not_refunded():
    fake = setup(response(chunks=[b"123456"]), response(chunks=[b"abc"]), max_response_bytes=5)
    budget = FetchBudget(max_bytes=8)

    async def run():
        with pytest.raises(FetchLimitError, match="Response"):
            await fake.fetcher.fetch(URL, budget=budget)
        assert budget.bytes_used == 6
        with pytest.raises(FetchLimitError, match="Page byte"):
            await fake.fetcher.fetch(URL, budget=budget)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["dns", "body"])
@pytest.mark.parametrize("page_deadline", [True, False])
def test_total_deadline_cancels_dns_and_streams(stage, page_deadline):
    async def run():
        cancelled = asyncio.Event()

        async def stall(*args):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        current = response(before_read=stall if stage == "body" else None)
        fake = setup(current, total_timeout=1 if page_deadline else 0.01,
                     resolver=stall if stage == "dns" else AsyncMock(return_value=[PUBLIC_IPV4]))
        budget = FetchBudget(total_timeout=0.01 if page_deadline else 1)
        with pytest.raises(FetchLimitError, match="deadline"):
            await fake.fetcher.fetch(URL, budget=budget)
        assert cancelled.is_set()
        assert fake.transport.closed == (1 if stage == "body" else 0)

    asyncio.run(run())


def test_page_deadline_starts_at_budget_creation(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr("web_capabilities.http_fetch.time.monotonic", lambda: clock.now)
    budget = FetchBudget(total_timeout=1)
    clock.now = 102.0
    fake = setup()
    with pytest.raises(FetchLimitError):
        asyncio.run(fake.fetcher.fetch(URL, budget=budget))
    fake.resolver.assert_not_awaited()


def test_external_cancellation_closes_response():
    async def run():
        started = asyncio.Event()

        async def stall():
            started.set()
            await asyncio.Future()

        fake = setup(response(before_read=stall))
        task = asyncio.create_task(fake.fetcher.fetch(URL, budget=FetchBudget()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.transport.closed == 1

    asyncio.run(run())


def test_system_resolver_collects_all_records_without_network(monkeypatch):
    async def run():
        resolver = AsyncMock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IPV4, 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC_IPV6, 443, 0, 0)),
        ])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        assert await resolve_public_host("example.com") == [PUBLIC_IPV4, PUBLIC_IPV6]
        resolver.assert_awaited_once_with("example.com", 443, family=socket.AF_UNSPEC,
                                         type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)

    asyncio.run(run())


def test_pinned_resolver_never_falls_back():
    async def run():
        resolver = _PinnedResolver(PinnedTarget(URL, "example.com", (PUBLIC_IPV4, PUBLIC_IPV6)))
        records = await resolver.resolve("example.com")
        assert [record["host"] for record in records] == [PUBLIC_IPV4, PUBLIC_IPV6]
        assert all(record["hostname"] == "example.com" and record["port"] == 443 for record in records)
        assert len(await resolver.resolve("example.com", family=socket.AF_INET6)) == 1
        for host, port in [("evil.com", 443), ("example.com", 80)]:
            with pytest.raises(FetchPolicyError):
                await resolver.resolve(host, port)
        await resolver.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError])
def test_real_aiohttp_connector_pins_addresses_and_original_tls_name(monkeypatch, failure):
    captured = SimpleNamespace(connections=[], requests=[], sessions=[])
    original_request = aiohttp.ClientSession._request
    original_session_init = aiohttp.ClientSession.__init__

    def session_init(session, *args, **kwargs):
        captured.sessions.append(kwargs)
        original_session_init(session, *args, **kwargs)

    async def request(session, method, url, **kwargs):
        captured.requests.append((method, url, kwargs))
        assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        assert session.trust_env is False
        assert session._retry_connection is False
        assert session.headers["Accept-Encoding"] == "identity"
        assert not {"Cookie", "Authorization", "Proxy-Authorization"}.intersection(session.headers)
        return await original_request(session, method, url, **kwargs)

    async def fake_connect(connector, *args, **kwargs):
        captured.connections.append(kwargs)
        raise failure("offline stop before socket creation")

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", session_init)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    monkeypatch.setattr(aiohttp.TCPConnector, "_wrap_create_connection", fake_connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@127.0.0.1:8080")
    resolver = AsyncMock(return_value=[PUBLIC_IPV4, PUBLIC_IPV6])
    fetcher = PublicHTTPSFetcher(allowed_hosts=["example.com"], resolver=resolver,
                                 transport=AiohttpPinnedTransport())
    with pytest.raises(FetchError, match="HTTPS fetch failed"):
        asyncio.run(fetcher.fetch(URL, budget=FetchBudget()))
    assert resolver.await_count == 1 and len(captured.connections) == 1
    connection = captured.connections[0]
    assert {record[4][0] for record in connection["addr_infos"]} == {PUBLIC_IPV4, PUBLIC_IPV6}
    assert connection["server_hostname"] == "example.com"
    assert connection["ssl"].check_hostname is True
    assert connection["ssl"].verify_mode == ssl.CERT_REQUIRED
    method, url, options = captured.requests[0]
    assert method == "GET" and str(url) == URL
    assert options["allow_redirects"] is False
    assert options["auth"] is None and options["proxy"] is None
    assert captured.sessions[0]["auto_decompress"] is False
    assert captured.sessions[0]["max_headers"] == 64
    assert captured.sessions[0]["connector"].closed


@pytest.mark.parametrize("fail", [False, True])
def test_default_transport_closes_fresh_sessions_and_responses(monkeypatch, fail):
    sessions = []
    current = response()
    current.close = Mock()

    @asynccontextmanager
    async def request(session, method, url, **kwargs):
        sessions.append(session)
        yield current

    monkeypatch.setattr(aiohttp.ClientSession, "request", request)

    async def run():
        transport = AiohttpPinnedTransport()
        target = PinnedTarget(URL, "example.com", (PUBLIC_IPV4,))
        for index in range(2):
            try:
                async with transport.open(target, "GET", 1) as received:
                    assert received is current
                    if fail:
                        raise FetchPolicyError("test body failure")
            except FetchPolicyError:
                assert fail
        assert sessions[0] is not sessions[1]
        assert all(session.closed for session in sessions)

    asyncio.run(run())
    assert current.close.call_count == 2


def test_dns_errors_are_opaque():
    fake = setup(resolver=AsyncMock(side_effect=socket.gaierror("sensitive detail")))
    with pytest.raises(FetchError, match="^HTTPS fetch failed$"):
        asyncio.run(fake.fetcher.fetch(URL, budget=FetchBudget()))

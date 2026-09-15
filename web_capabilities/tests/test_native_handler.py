"""Offline native Function URL contracts, not proof of deployed IAM/egress."""

import asyncio
import base64
import hashlib
import json
import socket
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from multidict import CIMultiDict

from web_capabilities import native_handler as native
from web_capabilities.browser import BrowserError
from web_capabilities.gateway import GatewayRateLimitError, GatewayServiceError, GatewayTimeoutError
from web_capabilities.http_fetch import FetchError, PublicHTTPSFetcher
from web_capabilities.quota import QuotaExceeded, QuotaUnavailable
from web_capabilities.url_policy import URLPolicy


KEY = "aB3dE5gH7jK9mN2pQ4sT6vW8yZ0_-AbCdEfGhIjKlMn"
URL = "https://example.com/article"
BROWSER_URL = "https://example.com/render"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:native/service-key-Ab12Cd"
PRIVATE = "PRIVATE_CONTENT_QUERY_URL_EXCEPTION_NEVER_LOG"
CONTEXT = SimpleNamespace(aws_request_id="lambda-request-123", get_remaining_time_in_millis=lambda: 60000)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Live network/AWS forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr("boto3.client", forbidden)
    monkeypatch.setattr("boto3.Session", forbidden)


def event(path="/search", arguments=None, **changes):
    if arguments is None:
        arguments = {"query": "topic", "count": 2} if path == "/search" else {"urls": [URL]}
    return {"version": "2.0", "rawPath": path, "rawQueryString": "",
            "requestContext": {"http": {"method": "POST", "path": path}},
            "headers": {"authorization": "Bearer " + KEY, "content-type": "application/json"},
            "body": json.dumps(arguments), "isBase64Encoded": False, **changes}


class Content:
    def __init__(self, body):
        self.body = body

    async def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


class Transport:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.body = b"<title>Title</title><script>hidden</script><main>Visible text</main>"
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.status = 200
        self.failure = None

    @asynccontextmanager
    async def open(self, target, method, timeout):
        self.calls.append((target, method, timeout))
        if self.failure:
            raise self.failure
        status, headers, body = self.responses.pop(0) if self.responses else (
            self.status, self.headers, self.body)
        yield SimpleNamespace(status=status, headers=CIMultiDict(headers), content=Content(body))


class Harness:
    def __init__(self, **changes):
        self.calls = []
        self.quota = SimpleNamespace(reserve=Mock(side_effect=self.reserve))
        self.search = SimpleNamespace(search=AsyncMock(return_value=SimpleNamespace(
            records=[SimpleNamespace(link=f"https://example.com/{index}", title=None if index == 0 else "Title",
                                     snippet="Snippet", metadata={"extra": True}) for index in range(5)],
            metadata={})))
        self.browser_document = {"text": PRIVATE, "title": "Rendered", "final_url": BROWSER_URL,
                                 "truncated": False, "session_terminated": True,
                                 "aws_start_request_id": "browser-start-123", "broker_requests": 2,
                                 "broker_bytes": 128}
        self.browser = SimpleNamespace(load=AsyncMock(side_effect=lambda url: dict(self.browser_document)))
        self.transport = Transport()
        self.resolver = AsyncMock(return_value=["93.184.216.34"])
        self.fetchers = []
        self.budgets = []
        self.secret = AsyncMock(return_value=KEY)
        self.policy = URLPolicy({"example.com": {"exact": ["/render"]}})
        options = dict(quota=self.quota, search=self.search, browser=self.browser,
                       policy=self.policy, browser_urls=[BROWSER_URL], http_factory=self.http,
                       search_enabled=True, fetch_enabled=True, browser_enabled=True)
        options.update(changes)
        self.service = native.NativeService(**options)

    def reserve(self, subject, capability, **kwargs):
        self.calls.append((subject, capability, kwargs))
        return "reservation"

    def http(self, **kwargs):
        fetcher = PublicHTTPSFetcher(**kwargs, resolver=self.resolver, transport=self.transport)
        self.fetchers.append(fetcher)
        original = fetcher.fetch

        async def fetch(*args, **options):
            self.budgets.append(options["budget"])
            assert self.calls[-1][1] == "browser"
            return await original(*args, **options)

        fetcher.fetch = fetch
        return fetcher

    def invoke(self, request=None, **kwargs):
        return native.handler(event() if request is None else request, CONTEXT, service=self.service,
                              secret_provider=self.secret, **kwargs)

    def no_calls(self):
        self.quota.reserve.assert_not_called()
        self.search.search.assert_not_awaited()
        self.browser.load.assert_not_awaited()
        assert not self.transport.calls
        self.resolver.assert_not_awaited()


def body(response):
    return json.loads(response["body"])


@pytest.mark.parametrize("count,expected", [(1, 1), (2, 2), (3, 3), (4, 3), (25, 3)])
def test_search_native_projection_clamp_and_shared_admission(count, expected, capsys):
    harness = Harness()
    request = event(arguments={"query": PRIVATE, "count": count})
    request["headers"].update({"x-user-id": "attacker", "x-subject": "admin", "x-chat-id": "chat",
                               "x-request-id": PRIVATE, "x-openwebui-user-email": PRIVATE})
    response = harness.invoke(request)
    assert response["statusCode"] == 200
    records = body(response)
    assert len(records) == expected
    assert all(set(record) == {"link", "title", "snippet"} for record in records)
    assert records[0]["title"] == ""
    harness.search.search.assert_awaited_once_with(PRIVATE, expected)
    assert len(harness.calls) == 1
    assert harness.calls[0][:2] == ("native-shared-service", "search")
    assert set(harness.calls[0][2]) == {"request_id"}
    assert response["headers"]["cache-control"] == "no-store"
    log_text = capsys.readouterr().out
    assert PRIVATE not in log_text and KEY not in log_text and "attacker" not in log_text
    log = json.loads(log_text)
    assert log["event"] == "native_web_capability"
    assert log["operation"] == "search" and log["success"] is True
    assert log["aws_request_id"] == CONTEXT.aws_request_id
    assert log["request_id"] == response["headers"]["x-request-id"]


@pytest.mark.parametrize("arguments", [
    {}, [], None, {"query": "x"}, {"query": "x", "count": True}, {"query": "x", "count": 1.0},
    {"query": "x", "count": "1"}, {"query": "x", "count": 0}, {"query": "x", "count": 26},
    {"query": "", "count": 1}, {"query": "  ", "count": 1}, {"query": "x" * 201, "count": 1},
    {"query": 1, "count": 1}, {"query": "\ud800", "count": 1}, {"query": "a\nb", "count": 1},
    {"query": "x", "count": 1, "subject": "admin"}, {"query": "x", "count": 1, "urls": [URL]},
])
def test_invalid_search_never_admitted(arguments):
    harness = Harness()
    response = harness.invoke(event(body=json.dumps(arguments)))
    assert response["statusCode"] == 422
    harness.no_calls()
    harness.secret.assert_not_awaited()


@pytest.mark.parametrize("arguments", [
    {}, {"urls": []}, {"urls": [URL] * 4}, {"urls": URL}, {"urls": [None]},
    {"urls": [1]}, {"urls": [URL], "subject": "admin"}, {"urls": [URL], "query": "x"},
])
def test_invalid_load_never_admitted(arguments):
    harness = Harness()
    assert harness.invoke(event("/load", arguments))["statusCode"] == 422
    harness.no_calls()


@pytest.mark.parametrize("url", [
    "http://example.com/", "file:///etc/passwd", "https://localhost/", "https://127.0.0.1/",
    "https://[::1]/", "https://user:pass@example.com/", "https://example.com:444/",
    "https://example.com./", "https://example.com\\@evil.com/", "https://bad_host.com/",
    "https://-bad.com/", "https://example.com/%0a", "https://example.com/%zz",
    "https://example.com/has space", "https://examplé.com/", "https://service.internal/",
    "https://example.com/\n", "https://example.com:0443/", "https://2130706433/",
    "https://example.com/%5c", "https://example.com/%7f", "https://x.home.arpa/",
    "https://example.com/" + "x" * 8192,
])
def test_all_urls_prevalidated_before_any_batch_charge(url):
    harness = Harness()
    response = harness.invoke(event("/load", {"urls": [URL, url]}))
    assert response["statusCode"] in {413, 422}
    harness.no_calls()


@pytest.mark.parametrize("changes,status", [
    ({"version": "1.0"}, 422), ({"requestContext": None}, 422),
    ({"rawPath": "/search/"}, 404), ({"rawPath": []}, 404),
    ({"requestContext": {"http": {"method": "GET", "path": "/search"}}}, 405),
    ({"requestContext": {"http": {"method": "POST", "path": "/load"}}}, 422),
    ({"rawQueryString": "query=secret"}, 422), ({"headers": None}, 401),
    ({"body": None}, 422), ({"body": "{}" + " " * 8192}, 413),
    ({"body": "é" * 4097}, 413), ({"body": "\ud800"}, 422),
    ({"body": "not json"}, 422), ({"body": '[1,2]'}, 422),
    ({"body": '{"query":"x","query":"y","count":1}'}, 422),
    ({"body": '{"query":"x","count":NaN}'}, 422),
    ({"body": '[' * 1500}, 422), ({"isBase64Encoded": "true"}, 422),
    ({"isBase64Encoded": True, "body": "!!!!"}, 422),
    ({"isBase64Encoded": True, "body": "é"}, 422),
    ({"isBase64Encoded": True, "body": base64.b64encode(b" " * 8193).decode()}, 413),
    ({"isBase64Encoded": True, "body": base64.b64encode(b"\xff").decode()}, 422),
])
def test_http_envelope_fails_closed(changes, status):
    harness = Harness()
    response = harness.invoke(event(**changes))
    assert response["statusCode"] == status
    assert "request_id" in body(response)
    harness.no_calls()
    harness.secret.assert_not_awaited()
    if status == 405:
        assert response["headers"]["allow"] == "POST"


@pytest.mark.parametrize("invocation", [None, [], "bad", 1])
def test_non_object_events(invocation):
    harness = Harness()
    response = native.handler(invocation, CONTEXT, service=harness.service, secret_provider=harness.secret)
    assert response["statusCode"] == 422
    harness.no_calls()


def test_exact_size_and_valid_base64_are_supported():
    harness = Harness()
    raw = json.dumps({"query": "x", "count": 1}).encode()
    raw += b" " * (8192 - len(raw))
    for encoded in (False, True):
        request = event(body=base64.b64encode(raw).decode() if encoded else raw.decode(),
                        isBase64Encoded=encoded)
        assert harness.invoke(request)["statusCode"] == 200
    assert len(harness.calls) == 2


@pytest.mark.parametrize("authorization", [None, "", "Basic " + KEY, "Bearer short", "Bearer " + KEY + " ",
                                             "Bearer " + KEY + ",Bearer " + KEY, "Bearer " + "z" * 44,
                                             "Bearer " + "é" * 44])
def test_bad_auth_never_charges_or_calls(authorization):
    harness = Harness()
    headers = {} if authorization is None else {"authorization": authorization}
    response = harness.invoke(event(headers=headers))
    assert response["statusCode"] == 401
    assert response["headers"]["www-authenticate"] == "Bearer"
    harness.no_calls()


def test_duplicate_auth_and_content_type_are_rejected():
    harness = Harness()
    for extra, status in [({"Authorization": "Bearer " + KEY}, 401),
                          ({"Content-Type": "application/json"}, 422),
                          ({"content-type": "text/plain"}, 422),
                          ({"content-encoding": "gzip"}, 422)]:
        request = event()
        request["headers"].update(extra)
        assert harness.invoke(request)["statusCode"] == status
    harness.no_calls()


def test_constant_time_auth_and_no_client_identity(monkeypatch):
    compare = Mock(wraps=native.hmac.compare_digest)
    monkeypatch.setattr(native.hmac, "compare_digest", compare)
    harness = Harness()
    request = event()
    request["headers"] = {"Authorization": "Bearer " + KEY, "subject": None, "x-user-id": {"admin": True}}
    assert harness.invoke(request)["statusCode"] == 200
    compare.assert_called_once_with(KEY.encode(), KEY.encode())
    assert harness.calls[0][0] == native.SUBJECT


@pytest.mark.parametrize("key", [None, "", "a" * 64, "short", "z" * 257, json.dumps({"key": KEY}), KEY + "\n"])
def test_bad_secret_fails_closed(key):
    harness = Harness()
    harness.secret.return_value = key
    assert harness.invoke()["statusCode"] == 503
    harness.no_calls()


def test_secret_failure_is_safe(capsys):
    harness = Harness()
    harness.secret.side_effect = RuntimeError(PRIVATE)
    assert harness.invoke()["statusCode"] == 503
    harness.no_calls()
    assert PRIVATE not in capsys.readouterr().out


def test_scoped_plain_secret_and_no_caching():
    client = SimpleNamespace(get_secret_value=Mock(return_value={"SecretString": KEY}))
    provider = native.ScopedSecret(ARN, "us-east-1", client=client)
    assert asyncio.run(provider()) == KEY
    assert asyncio.run(provider()) == KEY
    assert client.get_secret_value.call_count == 2
    client.get_secret_value.assert_called_with(SecretId=ARN)


@pytest.mark.parametrize("arn,region", [("secret-name", "us-east-1"), (ARN + "*", "us-east-1"),
                                       (ARN, "eu-west-1"), ("", "us-east-1"),
                                       (ARN.replace("secretsmanager", "ssm"), "us-east-1")])
def test_secret_requires_exact_region_scoped_arn(arn, region):
    with pytest.raises(native.NativeError) as raised:
        native.ScopedSecret(arn, region)
    assert raised.value.status == 503


@pytest.mark.parametrize("response", [{"SecretBinary": b"key"}, {"SecretString": KEY, "SecretBinary": b"key"},
                                     {"SecretString": json.dumps({"key": KEY})}, {}, {"SecretString": "a" * 64}])
def test_secret_binary_json_or_weak_values_denied(response):
    provider = native.ScopedSecret(ARN, "us-east-1", client=SimpleNamespace(
        get_secret_value=Mock(return_value=response)))
    with pytest.raises(native.NativeError):
        asyncio.run(provider())


@pytest.mark.parametrize("error,status", [(QuotaExceeded(PRIVATE), 429), (QuotaUnavailable(PRIVATE), 503)])
@pytest.mark.parametrize("path", ["/search", "/load"])
def test_quota_failure_has_no_downstream_or_refund(path, error, status, capsys):
    harness = Harness()
    harness.quota.reserve.side_effect = error
    response = harness.invoke(event(path))
    assert response["statusCode"] == status
    harness.quota.reserve.assert_called_once()
    harness.search.search.assert_not_awaited()
    harness.browser.load.assert_not_awaited()
    assert not harness.transport.calls
    assert PRIVATE not in capsys.readouterr().out + response["body"]


@pytest.mark.parametrize("error,status", [(GatewayServiceError(PRIVATE), 502), (GatewayTimeoutError(PRIVATE), 504),
                                         (GatewayRateLimitError(PRIVATE), 429), (RuntimeError(PRIVATE), 502)])
def test_search_failure_is_one_nonrefundable_attempt(error, status, capsys):
    harness = Harness()
    harness.search.search.side_effect = error
    response = harness.invoke()
    assert response["statusCode"] == status
    assert len(harness.calls) == 1
    harness.search.search.assert_awaited_once()
    harness.browser.load.assert_not_awaited()
    assert PRIVATE not in capsys.readouterr().out + response["body"]


def test_attribution_review_rejected_without_results():
    harness = Harness()
    harness.search.search.return_value.metadata["requires_attribution_review"] = True
    response = harness.invoke()
    assert response["statusCode"] == 502 and body(response)["code"] == "attribution_review"
    assert len(harness.calls) == 1 and "link" not in body(response)


@pytest.mark.parametrize("options,path", [({"search_enabled": False}, "/search"),
                                         ({"fetch_enabled": False}, "/load"),
                                         ({"search": None}, "/search")])
def test_disabled_flags_fail_before_quota(options, path):
    harness = Harness(**options)
    assert harness.invoke(event(path))["statusCode"] == 503
    harness.no_calls()


def test_constructor_defaults_are_disabled():
    quota = SimpleNamespace(reserve=Mock())
    service = native.NativeService(quota=quota)
    for path in ("/search", "/load"):
        response = native.handler(event(path), CONTEXT, service=service, secret_provider=AsyncMock(return_value=KEY))
        assert response["statusCode"] == 503
    quota.reserve.assert_not_called()


def test_static_html_native_document_and_fresh_single_host_fetcher(capsys):
    harness = Harness()
    response = harness.invoke(event("/load", {"urls": [URL, "https://second.org/page"]}))
    assert response["statusCode"] == 200
    documents = body(response)
    assert documents[0] == {"page_content": "Visible text", "metadata": {
        "source": URL, "requested_url": URL, "title": "Title", "source_capability": "bounded-https",
        "truncated": False}}
    assert harness.fetchers[0].allowed_hosts == {"example.com"}
    assert harness.fetchers[1].allowed_hosts == {"second.org"}
    assert all(fetcher.max_redirects == 2 and fetcher.max_response_bytes == native.MEBIBYTE
               and fetcher.total_timeout <= 12 for fetcher in harness.fetchers)
    assert len(harness.calls) == 2 and all(call[:2] == (native.SUBJECT, "browser") for call in harness.calls)
    assert harness.transport.calls[0][0].addresses == ("93.184.216.34",)
    assert all(call[1] == "GET" for call in harness.transport.calls)
    harness.search.search.assert_not_awaited()
    harness.browser.load.assert_not_awaited()
    log = json.loads(capsys.readouterr().out)
    assert len(log["reads"]) == 2
    assert all(proof["success"] and proof["broker_requests"] == 1 for proof in log["reads"])
    assert URL not in json.dumps(log)


@pytest.mark.parametrize("content_type", ["text/plain", "text/html"])
def test_document_text_is_bounded_20k(content_type):
    harness = Harness()
    harness.transport.body = b"z" * 21000
    harness.transport.headers = {"Content-Type": content_type}
    document = body(harness.invoke(event("/load")))[0]
    assert len(document["page_content"]) == 20000
    assert document["metadata"]["truncated"] is True


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["93.184.216.34", "10.1.2.3"],
                                      ["169.254.169.254"], ["::1"], ["fd00::1"], ["224.0.0.1"],
                                      ["2002:0808:0808::1"], ["100.64.1.1"], ["not-an-ip"]])
def test_dns_all_answers_global_no_connect_or_browser_fallback(addresses):
    harness = Harness()
    harness.resolver.return_value = addresses
    assert harness.invoke(event("/load"))["statusCode"] == 422
    assert len(harness.calls) == 1 and harness.calls[0][1] == "browser"
    assert not harness.transport.calls
    harness.browser.load.assert_not_awaited()
    harness.search.search.assert_not_awaited()


def test_two_same_host_redirects_are_pinned_and_one_read_attempt():
    harness = Harness()
    harness.resolver.side_effect = [["93.184.216.34"], ["8.8.8.8"], ["1.1.1.1"]]
    harness.transport.responses = [(302, {"Location": "/next"}, b""),
                                   (307, {"Location": "https://example.com/final"}, b"")]
    response = harness.invoke(event("/load"))
    assert response["statusCode"] == 200
    assert body(response)[0]["metadata"]["source"] == "https://example.com/final"
    assert [call[0].addresses for call in harness.transport.calls] == [
        ("93.184.216.34",), ("8.8.8.8",), ("1.1.1.1",)]
    assert harness.budgets[0].requests_used == 3
    assert len(harness.calls) == 1


@pytest.mark.parametrize("location,status", [("https://other.org/", 422), ("http://example.com/", 422),
                                            ("https://127.0.0.1/", 422), ("/loop", 504)])
def test_redirects_never_cross_host_retry_or_fallback(location, status):
    harness = Harness()
    harness.transport.status = 302
    harness.transport.headers = {"Location": location}
    response = harness.invoke(event("/load"))
    assert response["statusCode"] == status
    assert len(harness.transport.calls) == (3 if location == "/loop" else 1)
    assert len(harness.calls) == 1
    harness.browser.load.assert_not_awaited()


def test_redirect_dns_rebinding_is_rejected_before_second_connect():
    harness = Harness()
    harness.transport.responses = [(302, {"Location": "/next"}, b"")]
    harness.resolver.side_effect = [["93.184.216.34"], ["93.184.216.34", "127.0.0.1"]]
    assert harness.invoke(event("/load"))["statusCode"] == 422
    assert len(harness.transport.calls) == 1 and len(harness.calls) == 1


@pytest.mark.parametrize("headers,payload,status", [
    ({"Content-Type": "application/json"}, b"{}", 502),
    ({"Content-Type": "application/javascript"}, b"code", 502),
    ({"Content-Type": "application/pdf"}, b"pdf", 422),
    ({"Content-Type": "text/html; charset=iso-8859-1"}, b"html", 502),
    ({"Content-Type": "text/html"}, b"\xff", 502),
    ({"Content-Type": "text/html", "Content-Encoding": "gzip"}, b"data", 422),
    ({"Content-Type": "text/plain", "Content-Disposition": "attachment"}, b"data", 422),
    ({"Content-Type": "text/plain"}, b"x" * (native.MEBIBYTE + 1), 504),
    ({"Content-Type": "text/plain", "Content-Length": str(native.MEBIBYTE + 1)}, b"", 504),
])
def test_bad_or_oversized_documents_never_become_success(headers, payload, status):
    harness = Harness()
    harness.transport.headers, harness.transport.body = headers, payload
    response = harness.invoke(event("/load"))
    assert response["statusCode"] == status
    assert "page_content" not in response["body"]
    assert len(harness.calls) == 1
    harness.browser.load.assert_not_awaited()


def test_entire_batch_error_is_honest_and_stops_later_reads():
    harness = Harness()
    harness.transport.responses = [(200, {"Content-Type": "text/plain"}, PRIVATE.encode()),
                                   (404, {"Content-Type": "text/html"}, b"Not found")]
    response = harness.invoke(event("/load", {"urls": [URL, URL, URL]}))
    assert response["statusCode"] == 502
    assert PRIVATE not in response["body"] and "page_content" not in response["body"]
    assert len(harness.calls) == 2 and len(harness.transport.calls) == 2
    harness.search.search.assert_not_awaited()


def test_transport_failure_no_retry_or_browser_fallback(capsys):
    harness = Harness()
    harness.transport.failure = FetchError(PRIVATE)
    response = harness.invoke(event("/load"))
    assert response["statusCode"] == 502 and len(harness.transport.calls) == 1
    assert len(harness.calls) == 1
    harness.browser.load.assert_not_awaited()
    assert PRIVATE not in capsys.readouterr().out + response["body"]


def test_exact_browser_route_is_the_only_browser_entry(capsys):
    harness = Harness()
    response = harness.invoke(event("/load", {"urls": [BROWSER_URL]}))
    assert response["statusCode"] == 200
    document = body(response)[0]
    assert document["metadata"]["source_capability"] == "agentcore-browser-brokered"
    assert document["metadata"]["session_terminated"] is True
    assert document["page_content"] == PRIVATE
    harness.browser.load.assert_awaited_once_with(BROWSER_URL)
    assert not harness.transport.calls
    assert len(harness.calls) == 1 and harness.calls[0][1] == "browser"
    log = json.loads(capsys.readouterr().out)
    assert log["reads"][0] == {"operation": "browser", "success": True, "error": None,
                               "browser_start_request_id": "browser-start-123", "session_terminated": True,
                               "broker_requests": 2, "broker_bytes": 128}
    assert PRIVATE not in json.dumps(log) and BROWSER_URL not in json.dumps(log)


@pytest.mark.parametrize("url", [BROWSER_URL + "/", BROWSER_URL + "?x=1", BROWSER_URL + "#fragment",
                                "https://example.com:443/render", "https://EXAMPLE.com/render", URL])
def test_browser_route_variants_remain_static(url):
    harness = Harness()
    assert harness.invoke(event("/load", {"urls": [url]}))["statusCode"] == 200
    harness.browser.load.assert_not_awaited()
    assert len(harness.transport.calls) == 1


@pytest.mark.parametrize("options", [{"browser_enabled": False}, {"browser": None}])
def test_disabled_browser_batch_preflight_no_static_fallback(options):
    harness = Harness(**options)
    assert harness.invoke(event("/load", {"urls": [URL, BROWSER_URL]}))["statusCode"] == 503
    harness.no_calls()


def test_browser_failure_no_static_fallback_and_no_search_charge():
    harness = Harness()
    harness.browser.load.side_effect = BrowserError(PRIVATE)
    response = harness.invoke(event("/load", {"urls": [BROWSER_URL]}))
    assert response["statusCode"] == 502
    assert not harness.transport.calls and len(harness.calls) == 1
    assert harness.calls[0][1] == "browser"
    harness.search.search.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"session_terminated": False}, {"final_url": "https://other.org/"},
                                    {"text": "x" * 20001}, {"truncated": "false"}, {"title": "x" * 2001}])
def test_browser_proof_and_document_validation(changes):
    harness = Harness()
    harness.browser_document.update(changes)
    assert harness.invoke(event("/load", {"urls": [BROWSER_URL]}))["statusCode"] == 502
    assert not harness.transport.calls and len(harness.calls) == 1


def test_request_deadline_shared_by_sequential_reads():
    now = time.monotonic()
    harness = Harness(clock=lambda: now)
    original = harness.transport.open

    @asynccontextmanager
    async def advancing(*args, **kwargs):
        nonlocal now
        async with original(*args, **kwargs) as response:
            now += 30
            yield response

    harness.transport.open = advancing
    response = harness.invoke(event("/load", {"urls": [URL, URL, URL]}))
    assert response["statusCode"] == 504
    assert len(harness.calls) == 2 and len(harness.transport.calls) == 2


def test_handler_deadline_includes_auth(monkeypatch):
    monkeypatch.setattr(native, "REQUEST_TIMEOUT", 0.01)
    harness = Harness()

    async def stalled():
        await asyncio.sleep(1)

    harness.secret.side_effect = stalled
    started = time.monotonic()
    assert harness.invoke()["statusCode"] == 504
    assert time.monotonic() - started < 0.5
    harness.no_calls()


def test_handler_deadline_includes_upstream_no_refund(monkeypatch):
    monkeypatch.setattr(native, "REQUEST_TIMEOUT", 0.01)
    harness = Harness()

    async def stalled(*args):
        await asyncio.sleep(1)

    harness.search.search.side_effect = stalled
    assert harness.invoke()["statusCode"] == 504
    assert len(harness.calls) == 1


def test_browser_requires_remaining_cleanup_budget_before_reserve():
    harness = Harness(clock=lambda: time.monotonic() + 25)
    assert harness.invoke(event("/load", {"urls": [BROWSER_URL]}))["statusCode"] == 504
    harness.no_calls()


def test_context_remaining_time_caps_whole_request():
    harness = Harness()
    context = SimpleNamespace(aws_request_id="request", get_remaining_time_in_millis=lambda: 500)
    response = native.handler(event(), context, service=harness.service, secret_provider=harness.secret)
    assert response["statusCode"] == 504
    harness.no_calls()


def environment(**changes):
    return {"AGENTCORE_WEB_REGION": "us-east-1", "AGENTCORE_WEB_QUOTA_TABLE": "shared-web-quota",
            "AGENTCORE_WEB_GATEWAY_URL": "https://sample.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
            "AGENTCORE_WEB_BROWSER_ID": "native_browser-abc123",
            "AGENTCORE_WEB_SERVICE_SECRET_ARN": ARN, **changes}


def test_build_service_reuses_fixed_limits_connector_and_defaults():
    service = native.build_service(environment())
    assert not service.search_enabled and not service.fetch_enabled and not service.browser_enabled
    assert service.quota.table_name == "shared-web-quota"
    assert service.quota.limits.user_search == 10 and service.quota.limits.user_browser == 5
    assert service.quota.limits.deployment_search == 30 and service.quota.limits.deployment_browser == 15
    service = native.build_service(environment(AGENTCORE_WEB_SEARCH_ENABLED="true"))
    assert service.search.tool_name == "web-search-tool___WebSearch"
    assert service.search.timeout == 20


def test_build_browser_requires_operator_attestation_and_exact_curated_routes():
    env = environment(AGENTCORE_WEB_FETCH_ENABLED="true", AGENTCORE_WEB_BROWSER_ENABLED="true",
                      AGENTCORE_WEB_BROWSER_ROUTES=json.dumps([BROWSER_URL]),
                      AGENTCORE_WEB_URL_POLICY=json.dumps({"example.com": {"exact": ["/render"]}}))
    with pytest.raises(native.NativeError):
        native.build_service(env)
    env["AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY"] = "true"
    service = native.build_service(env)
    assert isinstance(service.browser, native.BrokeredBrowserFetcher)
    assert service.browser.broker.max_redirects == 0
    assert service.browser_urls == {BROWSER_URL}
    for urls in ([URL], [BROWSER_URL + "#fragment"], "not-a-list", [None], []):
        with pytest.raises((ValueError, native.NativeError)):
            native.build_service({**env, "AGENTCORE_WEB_BROWSER_ROUTES": json.dumps(urls)})


def test_service_configuration_and_missing_secret_fail_before_admission(monkeypatch):
    builder = Mock(side_effect=ValueError(PRIVATE))
    monkeypatch.setattr(native, "build_service", builder)
    response = native.handler(event(), CONTEXT, environ=environment(), secret_provider=AsyncMock(return_value=KEY))
    assert response["statusCode"] == 503
    builder.assert_called_once()
    response = native.handler(event(), CONTEXT, environ={})
    assert response["statusCode"] == 503
    assert builder.call_count == 1


def test_invalid_auth_does_not_construct_service(monkeypatch):
    builder = Mock(side_effect=AssertionError("Must not construct paid service"))
    monkeypatch.setattr(native, "build_service", builder)
    response = native.handler(event(headers={"authorization": "Bearer " + "z" * 44}), CONTEXT,
                              secret_provider=AsyncMock(return_value=KEY))
    assert response["statusCode"] == 401
    builder.assert_not_called()


def test_audited_browser_failure_keeps_start_termination_and_byte_proof(monkeypatch, capsys):
    harness = Harness()
    client = SimpleNamespace(start_browser_session=Mock(return_value={
        "sessionId": "session-123", "ResponseMetadata": {"RequestId": "start-123"}}))
    browser = native._AuditedBrowser(policy=harness.policy, region="us-east-1",
                                    browser_identifier="native_browser-abc123", aws_client=client)

    async def extract(instance, url, state):
        instance._start(state)
        budget = native.FetchBudget(max_requests=3, max_bytes=1024, total_timeout=1)
        budget.reserve_request()
        budget.consume_bytes(12)
        instance.broker.proof.update(broker_requests=budget.requests_used, broker_bytes=budget.bytes_used)
        raise BrowserError(PRIVATE)

    async def cleanup(instance, state):
        assert state["session"]["sessionId"] == "session-123"

    monkeypatch.setattr(native._AuditedBrowser, "_extract", extract)
    monkeypatch.setattr(native.BrokeredBrowserFetcher, "_cleanup", cleanup)
    harness.service.browser = browser
    assert harness.invoke(event("/load", {"urls": [BROWSER_URL]}))["statusCode"] == 502
    proof = json.loads(capsys.readouterr().out)["reads"][0]
    assert proof["browser_start_request_id"] == "start-123"
    assert proof["session_terminated"] is True and proof["success"] is False
    assert proof["broker_requests"] == 1 and proof["broker_bytes"] == 12
    client.start_browser_session.assert_called_once()


def test_proof_broker_records_budget_even_on_exception():
    budget = native.FetchBudget(max_requests=3, max_bytes=100, total_timeout=1)
    budget.reserve_request()
    budget.consume_bytes(7)
    proof = {}
    broker = native._ProofBroker(SimpleNamespace(fetch=AsyncMock(side_effect=FetchError(PRIVATE))), proof)
    with pytest.raises(FetchError):
        asyncio.run(broker.fetch(URL, budget=budget))
    assert proof == {"broker_requests": 1, "broker_bytes": 7}


def test_proof_fields_cannot_inject_urls_tokens_or_log_lines(capsys):
    harness = Harness()
    harness.browser_document.update(aws_start_request_id="https://secret.example/" + PRIVATE,
                                     broker_requests=PRIVATE, broker_bytes=KEY)
    assert harness.invoke(event("/load", {"urls": [BROWSER_URL]}))["statusCode"] == 200
    log = capsys.readouterr().out
    assert PRIVATE not in log and KEY not in log
    proof = json.loads(log)["reads"][0]
    assert proof["browser_start_request_id"] is None
    assert proof["broker_requests"] is None and proof["broker_bytes"] is None


def test_three_reads_are_sequential_and_each_consumes_a_fresh_attempt():
    harness = Harness()
    original = harness.transport.open
    order = []

    @asynccontextmanager
    async def ordered(*args, **kwargs):
        order.append("start")
        assert order.count("start") == order.count("end") + 1
        async with original(*args, **kwargs) as response:
            await asyncio.sleep(0)
            yield response
        order.append("end")

    harness.transport.open = ordered
    response = harness.invoke(event("/load", {"urls": [URL, URL, URL]}))
    assert response["statusCode"] == 200 and len(body(response)) == 3
    assert order == ["start", "end"] * 3
    assert len(harness.calls) == 3
    assert len({call[2]["request_id"] for call in harness.calls}) == 1


def test_real_quota_store_uses_existing_table_shared_subject_and_fresh_tokens():
    client = SimpleNamespace(transact_write_items=Mock())
    harness = Harness(quota=native.QuotaStore(table_name="shared-web-quota", region="us-east-1", client=client))
    assert harness.invoke()["statusCode"] == 200
    assert harness.invoke()["statusCode"] == 200
    assert harness.invoke(event("/load", {"urls": [BROWSER_URL]}))["statusCode"] == 200
    subject_hash = hashlib.sha256(json.dumps([native.SUBJECT], separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()
    transactions = [call.kwargs for call in client.transact_write_items.call_args_list]
    assert len({transaction["ClientRequestToken"] for transaction in transactions}) == 3
    for index, transaction in enumerate(transactions):
        updates = [item["Update"] for item in transaction["TransactItems"]]
        assert len(updates) == 2
        assert all(update["TableName"] == "shared-web-quota" for update in updates)
        capability = "search" if index < 2 else "browser"
        assert updates[0]["Key"]["pk"]["S"].endswith(f"#{capability}#user#{subject_hash}")
        assert updates[1]["Key"]["pk"]["S"].endswith(f"#{capability}#deployment")
        assert updates[0]["ExpressionAttributeValues"][":limit"] == {"N": "10" if index < 2 else "5"}
        assert updates[1]["ExpressionAttributeValues"][":limit"] == {"N": "30" if index < 2 else "15"}


def test_audited_browser_cleanup_failure_never_claims_termination(monkeypatch, capsys):
    harness = Harness()
    client = SimpleNamespace(start_browser_session=Mock(return_value={
        "sessionId": "session-123", "ResponseMetadata": {"RequestId": "start-123"}}))
    browser = native._AuditedBrowser(policy=harness.policy, region="us-east-1",
                                    browser_identifier="native_browser-abc123", aws_client=client)

    async def extract(instance, url, state):
        instance._start(state)
        return dict(harness.browser_document)

    async def cleanup(instance, state):
        raise BrowserError(PRIVATE)

    monkeypatch.setattr(native._AuditedBrowser, "_extract", extract)
    monkeypatch.setattr(native.BrokeredBrowserFetcher, "_cleanup", cleanup)
    harness.service.browser = browser
    response = harness.invoke(event("/load", {"urls": [BROWSER_URL]}))
    assert response["statusCode"] == 502 and "page_content" not in response["body"]
    log_text = capsys.readouterr().out
    assert PRIVATE not in log_text
    proof = json.loads(log_text)["reads"][0]
    assert proof["browser_start_request_id"] == "start-123"
    assert proof["session_terminated"] is False and proof["success"] is False


def test_browser_deadline_reserves_twenty_seconds_for_existing_cleanup(monkeypatch):
    harness = Harness()
    browser = native._AuditedBrowser(policy=harness.policy, region="us-east-1",
                                    browser_identifier="native_browser-abc123")
    deadlines = []

    async def load(instance, url):
        deadlines.append(instance.deadline)
        return dict(harness.browser_document)

    monkeypatch.setattr(native._AuditedBrowser, "load", load)
    harness.service.browser = browser
    context = SimpleNamespace(aws_request_id="request", get_remaining_time_in_millis=lambda: 35000)
    response = native.handler(event("/load", {"urls": [BROWSER_URL]}), context,
                              service=harness.service, secret_provider=harness.secret)
    assert response["statusCode"] == 200
    assert 20 < deadlines[0] < 34
    assert browser.deadline == 45


@pytest.mark.parametrize("status", [201, 204, 206, 400, 401, 403, 429, 500, 503])
def test_non_200_static_status_cannot_fabricate_source_document(status):
    harness = Harness()
    harness.transport.status = status
    response = harness.invoke(event("/load"))
    assert response["statusCode"] in {422, 502}
    assert "page_content" not in response["body"]
    assert len(harness.calls) == 1
    harness.browser.load.assert_not_awaited()


def test_secrets_client_has_bounded_timeouts_and_no_sdk_retries(monkeypatch):
    client = SimpleNamespace(get_secret_value=Mock(return_value={"SecretString": KEY}))
    factory = Mock(return_value=client)
    monkeypatch.setattr("boto3.client", factory)
    provider = native.ScopedSecret(ARN, "us-east-1")
    assert asyncio.run(provider()) == KEY
    assert factory.call_args.args == ("secretsmanager",)
    assert factory.call_args.kwargs["region_name"] == "us-east-1"
    config = factory.call_args.kwargs["config"]
    assert config.retries == {"total_max_attempts": 1}
    assert config.connect_timeout == 2 and config.read_timeout == 3
    client.get_secret_value.assert_called_once_with(SecretId=ARN)

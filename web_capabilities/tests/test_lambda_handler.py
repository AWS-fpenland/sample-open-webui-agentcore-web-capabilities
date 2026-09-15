"""Offline admission/adapter mocks, not proof of IAM invocation origin trust.

The deployed IAM boundary must restrict who can attest an OWUI subject. These
tests exercise exact subject authorization only after that trusted boundary.
"""

import asyncio
import hashlib
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError
from multidict import CIMultiDict

from web_capabilities import lambda_handler
from web_capabilities.http_fetch import FetchError
from web_capabilities.lambda_handler import CanaryService
from web_capabilities.quota import QuotaExceeded, QuotaUnavailable
from web_capabilities.url_policy import URLPolicy


REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"
SUBJECT = "trusted-owui-subject"
URL = "https://example.com/docs/page"
MEBIBYTE = 1024 * 1024
SOURCE_SECRET = "PRIVATE_SOURCE_CONTENT_DO_NOT_LOG"
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


def invocation(operation="browser", **changes):
    arguments = {"query": "public topic", "count": 2} if operation == "search" else {"url": URL}
    return {"operation": operation, "arguments": arguments, "subject": SUBJECT, "role": "user",
            "request_id": REQUEST_ID, "chat_id": "chat-123", **changes}


class MemoryContent:
    def __init__(self, body):
        self.body = body
        self.offset = 0

    async def read(self, size):
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class MemoryTransport:
    def __init__(self):
        self.body = b"<title>Title</title><main>Visible text</main>"
        self.content_type = "text/html"
        self.status = 200
        self.location = None
        self.calls = []

    @asynccontextmanager
    async def open(self, target, method, timeout):
        self.calls.append(target)
        headers = {"Content-Type": self.content_type}
        if self.location is not None:
            headers["Location"] = self.location
        yield SimpleNamespace(status=self.status, headers=CIMultiDict(headers), content=MemoryContent(self.body))


class ServiceHarness:
    def __init__(self, **options):
        self.events = []
        self.transport = MemoryTransport()
        self.document = {"title": "Title", "text": SOURCE_SECRET, "requested_url": URL,
                         "final_url": URL, "session_terminated": True}
        self.search_result = SimpleNamespace(records=[], metadata={})

        def reserve(*args, **kwargs):
            self.events.append("quota.reserve")
            return "reserved"

        async def browser_load(url):
            self.events.append("browser.load")
            return dict(self.document)

        async def search(query, count):
            self.events.append("search.search")
            return self.search_result

        self.quota = SimpleNamespace(reserve=Mock(side_effect=reserve))
        self.browser = SimpleNamespace(load=AsyncMock(side_effect=browser_load))
        self.search = SimpleNamespace(search=AsyncMock(side_effect=search))
        configuration = dict(subjects=[SUBJECT], policy=URLPolicy(RULES), quota=self.quota,
                             browser=self.browser, search=self.search, search_enabled=True, fetch_enabled=True)
        configuration.update(options)
        self.service = CanaryService(**configuration)
        self.service.http.resolver = AsyncMock(return_value=["93.184.216.34"])
        self.service.http.transport = self.transport
        original_fetch = self.service.http.fetch

        async def static_fetch(*args, **kwargs):
            self.events.append("http.fetch")
            return await original_fetch(*args, **kwargs)

        self.service.http.fetch = AsyncMock(side_effect=static_fetch)

    def assert_no_paid_calls(self):
        self.search.search.assert_not_awaited()
        self.browser.load.assert_not_awaited()
        self.service.http.fetch.assert_not_awaited()
        assert self.transport.calls == []

    def install(self, monkeypatch):
        factory = Mock(return_value=self.service)
        monkeypatch.setattr(lambda_handler, "build_service", factory)
        return factory


@pytest.mark.parametrize("operation", ["search", "browser", "static"])
@pytest.mark.parametrize("subject", [None, "", "other", SUBJECT.upper(), SUBJECT + "-suffix", " " + SUBJECT])
def test_exact_subject_authorization_precedes_quota_and_paid_calls(operation, subject):
    fake = ServiceHarness()
    result = asyncio.run(fake.service.execute(invocation(operation, subject=subject)))
    assert "not authorized" in result["error"]
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("role", [None, "", "owner", "User", "operator"])
def test_unapproved_roles_cannot_admit_authorized_subject(role):
    fake = ServiceHarness()
    result = asyncio.run(fake.service.execute(invocation(role=role)))
    assert "not authorized" in result["error"]
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("field", ["subject", "role"])
@pytest.mark.parametrize("value", [[], {}, 123, True])
def test_non_string_identity_fields_return_unauthorized_without_hash_errors(field, value):
    fake = ServiceHarness()
    result = asyncio.run(fake.service.execute(invocation(**{field: value})))
    assert "not authorized" in result["error"]
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("operation", [[], {}, None, 123])
def test_non_string_operations_raise_validation_errors_before_quota(operation):
    fake = ServiceHarness()
    with pytest.raises(ValueError, match="Invalid operation"):
        asyncio.run(fake.service.execute(invocation(operation)))
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("role", ["user", "admin"])
@pytest.mark.parametrize("operation, paid_event", [("search", "search.search"), ("browser", "browser.load"), ("static", "http.fetch")])
def test_quota_is_reserved_once_before_mock_paid_boundary(role, operation, paid_event):
    fake = ServiceHarness()
    result = asyncio.run(fake.service.execute(invocation(operation, role=role)))
    assert "error" not in result
    assert fake.events == ["quota.reserve", paid_event]
    fake.quota.reserve.assert_called_once_with(
        SUBJECT, "search" if operation == "search" else "browser", chat_id="chat-123",
        request_id=REQUEST_ID,
    )


def test_repeated_correlation_does_not_reuse_quota_admission():
    fake = ServiceHarness()
    database = SimpleNamespace(transact_write_items=Mock(return_value={}))
    fake.service.quota = lambda_handler.QuotaStore(table_name="synthetic-quota", region="us-east-1", client=database)
    asyncio.run(fake.service.execute(invocation("search")))
    asyncio.run(fake.service.execute(invocation("search")))
    tokens = [call.kwargs["ClientRequestToken"] for call in database.transact_write_items.call_args_list]
    assert len(tokens) == 2 and tokens[0] != tokens[1]
    assert fake.search.search.await_count == 2


@pytest.mark.parametrize("operation", ["search", "browser", "static"])
@pytest.mark.parametrize("failure", [QuotaExceeded, QuotaUnavailable])
def test_quota_failures_prevent_every_paid_call(operation, failure):
    fake = ServiceHarness()
    fake.quota.reserve.side_effect = failure("sensitive quota detail")
    with pytest.raises(failure):
        asyncio.run(fake.service.execute(invocation(operation)))
    fake.quota.reserve.assert_called_once()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("operation", ["search", "browser", "static"])
def test_operations_default_disabled_without_admission(operation):
    fake = ServiceHarness(search_enabled=False, fetch_enabled=False)
    result = asyncio.run(fake.service.execute(invocation(operation)))
    assert "disabled" in result["error"]
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("operation", ["search", "browser"])
def test_missing_provider_fails_closed_even_with_flag_enabled(operation):
    fake = ServiceHarness(**{operation: None})
    result = asyncio.run(fake.service.execute(invocation(operation)))
    assert "disabled" in result["error"]
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("operation", ["browser", "static"])
@pytest.mark.parametrize("url", [
    "https://example.com/admin", "https://example.com/docs/page?q=secret", "https://example.com/docs/page?",
    "https://example.com/docs/../admin", "https://example.com/docs/%2e%2e/admin", "https://evil.com/",
    "https://127.0.0.1/", "https://user@example.com/docs/page", "http://example.com/docs/page",
])
def test_static_and_browser_apply_identical_url_policy_before_quota(operation, url):
    fake = ServiceHarness()
    with pytest.raises(ValueError):
        asyncio.run(fake.service.execute(invocation(operation, arguments={"url": url})))
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("arguments", [
    {}, {"query": "topic"}, {"query": "topic", "count": 2, "extra": 1},
    {"query": None, "count": 1}, {"query": " ", "count": 1}, {"query": "a" * 201, "count": 1},
    {"query": "topic", "count": True}, {"query": "topic", "count": 0},
    {"query": "topic", "count": 4}, {"query": "topic", "count": 1.0},
])
def test_invalid_search_arguments_rejected_before_quota(arguments):
    fake = ServiceHarness()
    with pytest.raises(ValueError):
        asyncio.run(fake.service.execute(invocation("search", arguments=arguments)))
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("padding", [196, 1000])
def test_padding_cannot_bypass_search_argument_length_before_quota(padding):
    fake = ServiceHarness()
    with pytest.raises(ValueError):
        asyncio.run(fake.service.execute(invocation("search", arguments={"query": " " * padding + "topic", "count": 1})))
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


def test_search_raw_argument_length_allows_exactly_two_hundred_characters():
    fake = ServiceHarness()
    query = " " * 195 + "topic"
    result = asyncio.run(fake.service.execute(invocation("search", arguments={"query": query, "count": 1})))
    assert "error" not in result
    fake.quota.reserve.assert_called_once()
    fake.search.search.assert_awaited_once_with(query, 1)


@pytest.mark.parametrize("operation", ["static", "browser"])
@pytest.mark.parametrize("arguments", [{}, {"url": URL, "extra": "ignored"}, {"url": 123}, {"url": "a" * 2049}])
def test_invalid_reading_arguments_rejected_before_quota(operation, arguments):
    fake = ServiceHarness()
    with pytest.raises(ValueError):
        asyncio.run(fake.service.execute(invocation(operation, arguments=arguments)))
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("url", ["https://example.com/", "https://quotes.toscrape.com/page/1/",
                                "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"])
def test_static_accepts_the_same_curated_public_endpoints(url):
    fake = ServiceHarness()
    result = asyncio.run(fake.service.execute(invocation("static", arguments={"url": url})))
    assert result["document"]["final_url"] == url
    assert fake.transport.calls[0].url == url


def test_static_uses_one_request_one_mebibyte_and_reports_truthful_extraction():
    fake = ServiceHarness()
    fake.transport.body = b"<title>Title</title><script>secret()</script><main>Visible</main>"
    result = asyncio.run(fake.service.execute(invocation("static")))
    document = result["document"]
    assert document["text"] == "Visible" and document["title"] == "Title"
    assert document["extraction"] == "html-text-no-javascript"
    assert document["source_capability"] == "bounded-https"
    assert document["broker_requests"] == 1 and document["broker_bytes"] == len(fake.transport.body)
    assert "session_terminated" not in document
    assert fake.service.http.max_redirects == 0 and fake.service.http.max_response_bytes == MEBIBYTE
    arguments = fake.service.http.fetch.await_args.kwargs
    assert arguments["follow_redirects"] is False
    assert arguments["budget"].request_count == 1
    assert arguments["budget"].bytes_remaining == MEBIBYTE - len(fake.transport.body)
    fake.browser.load.assert_not_awaited()


def test_static_budget_has_a_single_total_deadline(monkeypatch):
    factory = Mock(wraps=lambda_handler.FetchBudget)
    monkeypatch.setattr(lambda_handler, "FetchBudget", factory)
    fake = ServiceHarness()
    asyncio.run(fake.service.execute(invocation("static")))
    factory.assert_called_once_with(max_requests=1, max_bytes=MEBIBYTE, total_timeout=12)


@pytest.mark.parametrize("content_type", ["text/plain", "text/html"])
def test_static_extraction_text_is_bounded(content_type):
    fake = ServiceHarness()
    fake.transport.content_type = content_type
    fake.transport.body = b"a" * 20001
    document = asyncio.run(fake.service.execute(invocation("static")))["document"]
    assert len(document["text"]) == 20000 and document["truncated"] is True
    assert document["broker_bytes"] == 20001


@pytest.mark.parametrize("content_type", [
    "text/plain; charset=utf-8", "text/html; charset=UTF-8", 'Text/Plain; charset="UTF-8"',
    "text/plain; charset=utf8", 'text/html; version=5; charset="utf-8"',
])
def test_static_accepts_valid_utf8_mime_parameters(content_type):
    fake = ServiceHarness()
    fake.transport.content_type = content_type
    fake.transport.body = "Café".encode("utf-8")
    document = asyncio.run(fake.service.execute(invocation("static")))["document"]
    assert document["text"] == "Café"


@pytest.mark.parametrize("content_type", [
    "text/plain; charset=iso-8859-1", "text/html; charset=utf-16", "text/plain; charset=us-ascii",
    "text/html; charset=", "text/html; charset=utf-8; charset=utf-8",
    "text/html; charset=utf-8; charset=iso-8859-1",
])
def test_static_rejects_declared_non_utf8_or_duplicate_charsets_even_for_ascii(content_type):
    fake = ServiceHarness()
    fake.transport.content_type = content_type
    fake.transport.body = b"ASCII bytes are valid UTF-8 but do not authorize relabelling"
    with pytest.raises(ValueError):
        asyncio.run(fake.service.execute(invocation("static")))
    fake.browser.load.assert_not_awaited()


@pytest.mark.parametrize("content_type", ["text/plain", "text/html", "text/plain; charset=iso-8859-1"])
def test_static_invalid_utf8_never_replaced_or_claimed_successful(content_type):
    fake = ServiceHarness()
    fake.transport.content_type = content_type
    fake.transport.body = b"private\xffcontent"
    with pytest.raises(ValueError):
        asyncio.run(fake.service.execute(invocation("static")))
    fake.browser.load.assert_not_awaited()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_static_redirects_fail_without_fallback_or_second_request(status):
    fake = ServiceHarness()
    fake.transport.status = status
    fake.transport.location = "https://example.com/docs/other"
    with pytest.raises(FetchError):
        asyncio.run(fake.service.execute(invocation("static")))
    assert len(fake.transport.calls) == 1
    fake.browser.load.assert_not_awaited()
    fake.search.search.assert_not_awaited()


def test_static_response_limit_rejects_before_document_extraction():
    fake = ServiceHarness()
    fake.transport.body = b"a" * (MEBIBYTE + 1)
    with pytest.raises(FetchError):
        asyncio.run(fake.service.execute(invocation("static")))
    fake.browser.load.assert_not_awaited()


def test_attribution_review_never_returns_unreviewed_search_records():
    fake = ServiceHarness()
    fake.search_result.metadata = {"requires_attribution_review": True}
    result = asyncio.run(fake.service.execute(invocation("search")))
    assert "operator review" in result["error"] and "records" not in result
    fake.quota.reserve.assert_called_once()


@pytest.mark.parametrize("event", [None, [], "string", 123, True, {},
    invocation(request_id="not-a-uuid"), invocation(arguments=[]), invocation(operation="unknown"),
    invocation(extra="unexpected"), invocation(role=[]), invocation(subject=[]),
    invocation(subject={"invalid": "subject"}), invocation(operation=[]), invocation(operation={}),
    invocation(request_id=[SOURCE_SECRET]), invocation(subject="\ud800"),
], ids=["null", "array", "string", "number", "boolean", "missing-fields", "invalid-request-id",
        "list-arguments", "unknown-operation", "extra-field", "list-role", "list-subject",
        "object-subject", "list-operation", "object-operation", "list-request-id", "surrogate-subject"])
def test_handler_malformed_json_payloads_return_opaque_errors_without_logging_crash(event, monkeypatch, capsys):
    fake = ServiceHarness()
    fake.install(monkeypatch)
    result = lambda_handler.handler(event, SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert isinstance(result, dict) and "error" in result
    logged = json.loads(capsys.readouterr().out)
    assert logged["success"] is False and logged["event"] == "web_capability"
    assert SOURCE_SECRET not in json.dumps(logged)
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


def test_handler_oversized_invocation_rejected_before_build_or_admission(monkeypatch, capsys):
    fake = ServiceHarness()
    factory = fake.install(monkeypatch)
    event = invocation("search", arguments={"query": SOURCE_SECRET * 300, "count": 1})
    result = lambda_handler.handler(event, SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert "error" in result
    factory.assert_not_called()
    fake.quota.reserve.assert_not_called()
    assert SOURCE_SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("subject", [[], {}, "", "a" * 257, "\ud800"])
def test_unsafe_subjects_have_no_log_hash_and_never_reach_quota(subject, monkeypatch, capsys):
    fake = ServiceHarness()
    fake.install(monkeypatch)
    result = lambda_handler.handler(invocation(subject=subject), SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert "error" in result
    logged = json.loads(capsys.readouterr().out)
    assert logged["subject_hash"] is None and logged["success"] is False
    fake.quota.reserve.assert_not_called()
    fake.assert_no_paid_calls()


@pytest.mark.parametrize("operation", ["browser", "static", "search"])
def test_handler_logs_metadata_only_never_queries_urls_or_source_content(operation, monkeypatch, capsys):
    fake = ServiceHarness()
    fake.transport.body = SOURCE_SECRET.encode()
    fake.install(monkeypatch)
    event = invocation(operation)
    if operation == "search":
        event["arguments"]["query"] = SOURCE_SECRET
    result = lambda_handler.handler(event, SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert "error" not in result
    output = capsys.readouterr().out
    assert SOURCE_SECRET not in output and URL not in output and SUBJECT not in output
    logged = json.loads(output)
    required = {
        "event": "web_capability", "request_id": REQUEST_ID,
        "subject_hash": hashlib.sha256(SUBJECT.encode()).hexdigest(), "operation": operation,
        "success": True, "aws_request_id": "mock-lambda-request", "error_type": None,
    }
    document = result.get("document", {})
    provenance = {
        "browser_start_request_id": document.get("aws_start_request_id"),
        "session_terminated": document.get("session_terminated"),
        "broker_requests": document.get("broker_requests"), "broker_bytes": document.get("broker_bytes"),
    }
    assert required.items() <= logged.items()
    assert set(logged) <= set(required) | set(provenance)
    for name, value in provenance.items():
        if name in logged:
            assert logged[name] == value


def test_invalid_correlation_cannot_smuggle_source_content_into_logs(monkeypatch, capsys):
    fake = ServiceHarness()
    fake.install(monkeypatch)
    forged_id = "SOURCE_SECRET_NOT_A_UUID".ljust(36, "!")
    result = lambda_handler.handler(invocation(request_id=forged_id), SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert "error" in result
    output = capsys.readouterr().out
    assert forged_id not in output
    assert json.loads(output)["request_id"] is None
    fake.quota.reserve.assert_not_called()


@pytest.mark.parametrize("failure, message", [(QuotaExceeded, "quota exceeded"), (QuotaUnavailable, "quota service unavailable"),
                                            (RuntimeError, "unavailable or rejected")])
def test_handler_errors_do_not_log_exception_content_or_retry(failure, message, monkeypatch, capsys):
    fake = ServiceHarness()
    fake.quota.reserve.side_effect = failure(SOURCE_SECRET)
    fake.install(monkeypatch)
    result = lambda_handler.handler(invocation(), SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert message in result["error"]
    output = capsys.readouterr().out
    assert SOURCE_SECRET not in output and SOURCE_SECRET not in json.dumps(result)
    logged = json.loads(output)
    assert logged["error_type"] == failure.__name__ and logged["success"] is False
    fake.quota.reserve.assert_called_once()
    fake.assert_no_paid_calls()


def test_handler_aws_exception_logs_class_name_without_private_message_or_response(monkeypatch, capsys):
    fake = ServiceHarness()
    fake.browser.load.side_effect = ClientError({
        "Error": {"Code": "AccessDeniedException", "Message": SOURCE_SECRET},
        "ResponseMetadata": {"RequestId": "private-upstream-request"},
    }, "StartBrowserSession")
    fake.install(monkeypatch)
    result = lambda_handler.handler(invocation(), SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert "error" in result
    output = capsys.readouterr().out
    logged = json.loads(output)
    assert logged["error_type"] == "ClientError" and logged["success"] is False
    for private_value in (SOURCE_SECRET, "private-upstream-request", "AccessDeniedException", "StartBrowserSession"):
        assert private_value not in output and private_value not in json.dumps(result)
    fake.quota.reserve.assert_called_once()
    fake.browser.load.assert_awaited_once()
    fake.service.http.fetch.assert_not_awaited()
    fake.search.search.assert_not_awaited()


def test_handler_total_timeout_cancels_mock_work_without_fallback(monkeypatch, capsys):
    fake = ServiceHarness()
    fake.install(monkeypatch)
    observed = []
    cancelled = []
    original_wait_for = asyncio.wait_for

    async def stall(url):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    async def shortened_wait_for(awaitable, timeout):
        observed.append(timeout)
        return await original_wait_for(awaitable, timeout=0.01)

    fake.browser.load.side_effect = stall
    monkeypatch.setattr(lambda_handler.asyncio, "wait_for", shortened_wait_for)
    result = lambda_handler.handler(invocation(), SimpleNamespace(aws_request_id="mock-lambda-request"))
    assert "error" in result and observed == [50] and cancelled == [True]
    fake.quota.reserve.assert_called_once()
    fake.service.http.fetch.assert_not_awaited()
    fake.search.search.assert_not_awaited()
    assert json.loads(capsys.readouterr().out)["success"] is False


@pytest.fixture
def configured_environment(monkeypatch):
    for name in ["AGENTCORE_WEB_SEARCH_ENABLED", "AGENTCORE_WEB_FETCH_ENABLED", "AGENTCORE_WEB_BROWSER_ENABLED"]:
        monkeypatch.delenv(name, raising=False)
    settings = {
        "AGENTCORE_WEB_REGION": "us-east-1", "AGENTCORE_WEB_URL_POLICY": json.dumps(RULES),
        "AGENTCORE_WEB_SUBJECTS": json.dumps([SUBJECT]), "AGENTCORE_WEB_QUOTA_TABLE": "mock-quota-table",
        "AGENTCORE_WEB_GATEWAY_URL": "https://mock.gateway.invalid/mcp", "AGENTCORE_WEB_BROWSER_ID": "CustomBrowser-123",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    constructors = SimpleNamespace(quota=Mock(), search=Mock(), browser=Mock())
    monkeypatch.setattr(lambda_handler, "QuotaStore", constructors.quota)
    monkeypatch.setattr(lambda_handler, "GatewaySearchClient", constructors.search)
    monkeypatch.setattr(lambda_handler, "BrokeredBrowserFetcher", constructors.browser)
    return constructors


@pytest.mark.parametrize("flag", [None, "false", "False", "TRUE", "True", "1", ""])
def test_build_service_operations_require_explicit_lowercase_true(flag, configured_environment, monkeypatch):
    if flag is not None:
        monkeypatch.setenv("AGENTCORE_WEB_SEARCH_ENABLED", flag)
        monkeypatch.setenv("AGENTCORE_WEB_FETCH_ENABLED", flag)
        monkeypatch.setenv("AGENTCORE_WEB_BROWSER_ENABLED", flag)
    service = lambda_handler.build_service()
    assert service.search_enabled is False and service.fetch_enabled is False
    assert service.search is None and service.browser is None
    configured_environment.search.assert_not_called()
    configured_environment.browser.assert_not_called()


@pytest.mark.parametrize("operation", ["search", "fetch"])
def test_build_service_enables_only_the_requested_operation(operation, configured_environment, monkeypatch):
    monkeypatch.setenv("AGENTCORE_WEB_" + operation.upper() + "_ENABLED", "true")
    service = lambda_handler.build_service()
    assert service.search_enabled is (operation == "search")
    assert service.fetch_enabled is (operation == "fetch")
    assert configured_environment.search.call_count == (1 if operation == "search" else 0)
    configured_environment.browser.assert_not_called()
    assert service.browser is None


@pytest.mark.parametrize("fetch_enabled", [False, True])
@pytest.mark.parametrize("browser_enabled", [False, True])
def test_browser_construction_requires_both_fetch_and_browser_flags(
    fetch_enabled, browser_enabled, configured_environment, monkeypatch,
):
    monkeypatch.setenv("AGENTCORE_WEB_FETCH_ENABLED", "true" if fetch_enabled else "false")
    monkeypatch.setenv("AGENTCORE_WEB_BROWSER_ENABLED", "true" if browser_enabled else "false")
    service = lambda_handler.build_service()
    expected_browser = fetch_enabled and browser_enabled
    assert configured_environment.browser.call_count == int(expected_browser)
    assert service.fetch_enabled is fetch_enabled
    if expected_browser:
        assert service.browser is configured_environment.browser.return_value
    else:
        assert service.browser is None
        result = asyncio.run(service.execute(invocation("browser")))
        assert "disabled" in result["error"]
        configured_environment.quota.return_value.reserve.assert_not_called()


@pytest.mark.parametrize("browser_flag", [None, "false", "False", "TRUE", "True", "1", ""])
def test_static_remains_enabled_when_independent_browser_flag_is_not_true(
    browser_flag, configured_environment, monkeypatch,
):
    monkeypatch.setenv("AGENTCORE_WEB_FETCH_ENABLED", "true")
    if browser_flag is not None:
        monkeypatch.setenv("AGENTCORE_WEB_BROWSER_ENABLED", browser_flag)
    service = lambda_handler.build_service()
    service.http.resolver = AsyncMock(return_value=["93.184.216.34"])
    service.http.transport = MemoryTransport()
    result = asyncio.run(service.execute(invocation("static")))
    assert result["document"]["source_capability"] == "bounded-https"
    configured_environment.quota.return_value.reserve.assert_called_once()
    configured_environment.browser.assert_not_called()
    assert service.browser is None and service.fetch_enabled is True
    configured_environment.quota.return_value.reserve.reset_mock()
    result = asyncio.run(service.execute(invocation("browser")))
    assert "disabled" in result["error"]
    configured_environment.quota.return_value.reserve.assert_not_called()


@pytest.mark.parametrize("region", ["", "us-west-2", "US-EAST-1"])
def test_build_service_rejects_unapproved_region_before_constructors(region, configured_environment, monkeypatch):
    monkeypatch.setenv("AGENTCORE_WEB_REGION", region)
    with pytest.raises(ValueError):
        lambda_handler.build_service()
    configured_environment.quota.assert_not_called()
    configured_environment.search.assert_not_called()
    configured_environment.browser.assert_not_called()

"""Shared-service Function URL v2 adapter, deliberately without caller identity.

Deploy with a 60-second Lambda timeout. The HTTP budget is 50 seconds including
authentication, admission and browser cleanup. Use the existing deployment quota
table, Gateway connector and AGENTCORE_WEB_{REGION,SEARCH_ENABLED,FETCH_ENABLED,
BROWSER_ENABLED,QUOTA_TABLE,GATEWAY_URL,BROWSER_ID,URL_POLICY} configuration.
All capability flags default off. Browser additionally requires
AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY=true (an operator attestation of the
dedicated no-egress deployment, not enforcement) and exact URL strings in the
JSON list AGENTCORE_WEB_BROWSER_ROUTES. There is never a browser fallback.

AGENTCORE_WEB_SERVICE_SECRET_ARN must identify one Secrets Manager
secret in the explicit region; IAM must grant GetSecretValue on only that ARN.
SecretString is a plain, randomly generated URL-safe bearer key, not JSON: use
secrets.token_urlsafe(32). Keys must be 32..256 characters with at least sixteen
distinct characters. Secrets are not cached, logged, or accepted from the body.

Tests inject NativeService dependencies and an async secret_provider; neither
test seam is accepted from an HTTP event. Failed batches return only an error,
but preceding attempts remain charged. Static reads use the existing browser
quota bucket. Client user/subject/chat/correlation headers are entirely ignored.
"""

import asyncio
import base64
import binascii
import copy
import hmac
import json
import os
import re
import time
import uuid
from urllib.parse import urlsplit

from web_capabilities.brokered_browser import BrokeredBrowserFetcher
from web_capabilities.browser import BrowserError
from web_capabilities.documents import DocumentParser, utf8_content_type
from web_capabilities.gateway import (
    GatewayError, GatewayRateLimitError, GatewaySearchClient, GatewayTimeoutError,
)
from web_capabilities.http_fetch import (
    FetchBudget, FetchError, FetchLimitError, FetchPolicyError, PublicHTTPSFetcher,
    _canonical_url, _host,
)
from web_capabilities.quota import QuotaExceeded, QuotaLimits, QuotaStore, QuotaUnavailable
from web_capabilities.url_policy import URLPolicy


SUBJECT = "native-shared-service"
REQUEST_TIMEOUT = 50
MAX_BODY_BYTES = 8192
MAX_TEXT = 20000
MEBIBYTE = 1024 * 1024
_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")
_MESSAGES = {
    401: "Service authentication required",
    404: "Endpoint not found",
    405: "Only POST is supported",
    413: "Request exceeds 8192 bytes",
    422: "Invalid request or unsupported public URL",
    429: "Web capability attempt quota or rate limit exceeded",
    502: "Upstream document or search failed; no fallback attempted",
    503: "Web capability unavailable or disabled",
    504: "Web capability deadline or resource budget exhausted",
}


class NativeError(RuntimeError):
    def __init__(self, status, code):
        super().__init__(_MESSAGES[status])
        self.status, self.code = status, code


def _valid_key(value):
    return (isinstance(value, str) and _KEY_PATTERN.fullmatch(value) is not None
            and len(set(value)) >= 16)


class ScopedSecret:
    def __init__(self, arn, region, *, client=None):
        match = re.fullmatch(
            r"arn:aws:secretsmanager:([a-z]{2}(?:-[a-z]+)+-\d):\d{12}:secret:"
            r"[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}", arn or "")
        if match is None or match[1] != region:
            raise NativeError(503, "secret_configuration")
        self.arn, self.region, self.client = arn, region, client

    def _read(self):
        try:
            client = self.client
            if client is None:
                import boto3
                from botocore.config import Config

                client = boto3.client("secretsmanager", region_name=self.region, config=Config(
                    connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))
            response = client.get_secret_value(SecretId=self.arn)
            key = response.get("SecretString")
            if "SecretBinary" in response or not _valid_key(key):
                raise ValueError
            return key
        except Exception:
            raise NativeError(503, "secret_unavailable") from None

    async def __call__(self):
        return await asyncio.to_thread(self._read)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError


def _json(value):
    return json.loads(value, object_pairs_hook=_json_object, parse_constant=_invalid_constant)


def _header(headers, name):
    values = [value for key, value in headers.items()
              if isinstance(key, str) and key.lower() == name]
    if len(values) > 1 or (values and not isinstance(values[0], str)):
        raise NativeError(401 if name == "authorization" else 422, "ambiguous_header")
    return values[0] if values else None


def _request(event):
    if not isinstance(event, dict) or event.get("version") != "2.0":
        raise NativeError(422, "event_format")
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, dict) else None
    if not isinstance(http, dict):
        raise NativeError(422, "event_format")
    path = event.get("rawPath")
    if not isinstance(path, str) or path not in {"/search", "/load"}:
        raise NativeError(404, "path")
    if http.get("method") != "POST":
        raise NativeError(405, "method")
    if http.get("path") != path or event.get("rawQueryString", "") != "":
        raise NativeError(422, "path_format")
    headers = event.get("headers")
    if not isinstance(headers, dict):
        raise NativeError(401, "authorization")
    authorization = _header(headers, "authorization")
    if (not isinstance(authorization, str)
            or not re.fullmatch(r"Bearer [A-Za-z0-9_-]{32,256}", authorization)):
        raise NativeError(401, "authorization")
    content_type = _header(headers, "content-type")
    if content_type is not None and not re.fullmatch(
            r"application/json(?:\s*;\s*charset=utf-8)?", content_type, re.IGNORECASE):
        raise NativeError(422, "content_type")
    if _header(headers, "content-encoding") is not None:
        raise NativeError(422, "content_encoding")
    body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if not isinstance(body, str) or type(encoded) is not bool:
        raise NativeError(422, "body_format")
    if len(body) > (4 * ((MAX_BODY_BYTES + 2) // 3) if encoded else MAX_BODY_BYTES):
        raise NativeError(413, "request_size")
    try:
        raw = base64.b64decode(body, validate=True) if encoded else body.encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise NativeError(413, "request_size")
        if encoded and base64.b64encode(raw).decode("ascii") != body:
            raise ValueError
        arguments = _json(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        raise NativeError(422, "body_format") from None
    operation = path[1:]
    _arguments(operation, arguments)
    return operation, arguments, authorization[7:]


def _static_url(url):
    try:
        if not isinstance(url, str):
            raise ValueError
        host = _host(urlsplit(url).hostname)
        if (host.endswith(".home.arpa") or host.split(".")[-1] in {
                "localhost", "local", "internal", "lan", "home", "onion", "invalid", "test"}):
            raise ValueError
        return _canonical_url(url, frozenset({host})), host
    except (ValueError, TypeError, FetchPolicyError):
        raise NativeError(422, "url_policy") from None


def _arguments(operation, arguments):
    if not isinstance(arguments, dict):
        raise NativeError(422, "arguments")
    if operation == "search":
        if (set(arguments) != {"query", "count"}
                or not isinstance(arguments["query"], str)
                or not 1 <= len(arguments["query"]) <= 200 or not arguments["query"].strip()
                or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
                       for character in arguments["query"])
                or type(arguments["count"]) is not int or not 1 <= arguments["count"] <= 25):
            raise NativeError(422, "search_arguments")
    elif operation == "load":
        if (set(arguments) != {"urls"} or not isinstance(arguments["urls"], list)
                or not 1 <= len(arguments["urls"]) <= 3):
            raise NativeError(422, "load_arguments")
        for url in arguments["urls"]:
            _static_url(url)
    else:
        raise NativeError(404, "operation")


class _ProofBroker:
    def __init__(self, broker, proof):
        self.broker, self.proof = broker, proof

    async def fetch(self, *args, **kwargs):
        try:
            return await self.broker.fetch(*args, **kwargs)
        finally:
            budget = kwargs["budget"]
            self.proof.update(broker_requests=budget.requests_used, broker_bytes=budget.bytes_used)


class _AuditedBrowser(BrokeredBrowserFetcher):
    def _start(self, state):
        try:
            return super()._start(state)
        finally:
            if "session" in state:
                self.proof["browser_start_request_id"] = state["session"].get(
                    "ResponseMetadata", {}).get("RequestId")
                self.proof["session_terminated"] = False

    async def _cleanup(self, state):
        await super()._cleanup(state)
        if "session" in state:
            self.proof["session_terminated"] = True


class NativeService:
    def __init__(self, *, quota, search=None, browser=None, browser_urls=(), policy=None,
                 http_factory=PublicHTTPSFetcher, search_enabled=False,
                 fetch_enabled=False, browser_enabled=False, clock=time.monotonic):
        if (not isinstance(browser_urls, (list, tuple)) or len(browser_urls) > 20
                or any(not isinstance(url, str) for url in browser_urls)):
            raise ValueError("Invalid exact browser routes")
        for url in browser_urls:
            _static_url(url)
            if policy is None or policy.validate(url) != url:
                raise ValueError("Browser routes must be canonical curated URLs")
        self.quota, self.search, self.browser = quota, search, browser
        self.browser_urls, self.policy = frozenset(browser_urls), policy
        self.http_factory, self.clock = http_factory, clock
        self.search_enabled = search_enabled is True
        self.fetch_enabled = fetch_enabled is True
        self.browser_enabled = browser_enabled is True

    def _remaining(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise NativeError(504, "request_deadline")
        return remaining

    async def execute(self, operation, arguments, *, request_id, deadline, proofs=None):
        _arguments(operation, arguments)
        proofs = proofs if proofs is not None else []
        self._remaining(deadline)
        if operation == "search":
            if not self.search_enabled or self.search is None:
                raise NativeError(503, "search_disabled")
            await asyncio.to_thread(self.quota.reserve, SUBJECT, "search", request_id=request_id)
            self._remaining(deadline)
            count = min(arguments["count"], 3)
            result = await self.search.search(arguments["query"], count)
            self._remaining(deadline)
            if result.metadata.get("requires_attribution_review"):
                raise NativeError(502, "attribution_review")
            return [{"link": record.link, "title": record.title or "", "snippet": record.snippet}
                    for record in result.records[:count]]
        if not self.fetch_enabled:
            raise NativeError(503, "fetch_disabled")
        routes = [(url, _static_url(url), url in self.browser_urls) for url in arguments["urls"]]
        if any(use_browser for _, _, use_browser in routes) and (
                not self.browser_enabled or self.browser is None):
            raise NativeError(503, "browser_disabled")
        documents = []
        for url, (canonical, host), use_browser in routes:
            proof = {"operation": "browser" if use_browser else "static", "success": False,
                     "browser_start_request_id": None, "session_terminated": None,
                     "broker_requests": 0, "broker_bytes": 0}
            proofs.append(proof)
            try:
                remaining = self._remaining(deadline)
                if use_browser and remaining <= 26:
                    raise NativeError(504, "browser_cleanup_budget")
                http = None if use_browser else self.http_factory(
                    allowed_hosts=(host,), max_redirects=2, max_response_bytes=MEBIBYTE,
                    total_timeout=min(12, remaining))
                await asyncio.to_thread(self.quota.reserve, SUBJECT, "browser", request_id=request_id)
                remaining = self._remaining(deadline)
                if use_browser:
                    browser = copy.copy(self.browser)
                    browser.deadline = min(45, remaining - 0.25)
                    if browser.deadline <= 20:
                        raise NativeError(504, "browser_cleanup_budget")
                    if isinstance(browser, _AuditedBrowser):
                        browser.proof = proof
                        browser.broker = _ProofBroker(browser.broker, proof)
                    document = await browser.load(url)
                    for source, target in (("aws_start_request_id", "browser_start_request_id"),
                                           ("session_terminated", "session_terminated"),
                                           ("broker_requests", "broker_requests"),
                                           ("broker_bytes", "broker_bytes")):
                        if source in document:
                            proof[target] = document[source]
                    if document.get("session_terminated") is not True:
                        raise NativeError(502, "browser_termination")
                else:
                    budget = FetchBudget(max_requests=3, max_bytes=MEBIBYTE,
                                         total_timeout=min(12, remaining))
                    try:
                        response = await http.fetch(canonical, budget=budget, follow_redirects=True)
                    finally:
                        proof.update(broker_requests=budget.requests_used, broker_bytes=budget.bytes_used)
                    document = self._static_document(url, host, response)
                self._remaining(deadline)
                documents.append(self._document(document, url, use_browser))
                proof["success"] = True
            except BaseException as error:
                proof["error"] = _failure(error)[1]
                raise
        return documents

    @staticmethod
    def _static_document(url, host, response):
        try:
            final_url = _canonical_url(response.final_url, frozenset({host}))
            content_type = utf8_content_type(response.content_type)
            if (response.status != 200 or content_type not in {"text/html", "text/plain"}
                    or len(response.body) > MEBIBYTE):
                raise ValueError
            text = response.body.decode("utf-8", errors="strict")
            if content_type == "text/html":
                parser = DocumentParser(final_url, max_text=MAX_TEXT)
                parser.feed(text)
                parser.close()
                return parser.document(url)
            return {"text": text[:MAX_TEXT], "title": None, "final_url": final_url,
                    "truncated": len(text) > MAX_TEXT, "extraction": "plain-text"}
        except (ValueError, TypeError, FetchPolicyError):
            raise NativeError(502, "document_format") from None

    def _document(self, document, requested_url, browser):
        text, title = document.get("text"), document.get("title")
        final_url = document.get("final_url")
        if (not isinstance(text, str) or len(text) > MAX_TEXT
                or (title is not None and (not isinstance(title, str) or len(title) > 2000))
                or type(document.get("truncated")) is not bool):
            raise NativeError(502, "document_format")
        if browser and (final_url != requested_url or self.policy.validate(final_url) != final_url):
            raise NativeError(502, "browser_document_policy")
        metadata = {"source": final_url, "title": title or "", "requested_url": requested_url,
                    "source_capability": "agentcore-browser-brokered" if browser else "bounded-https",
                    "truncated": document["truncated"]}
        if browser:
            metadata["session_terminated"] = True
        return {"page_content": text, "metadata": metadata}


def build_service(environ=None):
    environ = os.environ if environ is None else environ
    region = environ.get("AGENTCORE_WEB_REGION", "")
    if region not in {"us-east-1", "eu-west-1", "ap-northeast-1"}:
        raise NativeError(503, "region_configuration")
    search_enabled = environ.get("AGENTCORE_WEB_SEARCH_ENABLED") == "true"
    fetch_enabled = environ.get("AGENTCORE_WEB_FETCH_ENABLED") == "true"
    browser_enabled = environ.get("AGENTCORE_WEB_BROWSER_ENABLED") == "true"
    browser_urls = _json(environ.get("AGENTCORE_WEB_BROWSER_ROUTES", "[]"))
    policy = URLPolicy(_json(environ.get("AGENTCORE_WEB_URL_POLICY", "{}"))) if browser_urls else None
    browser = None
    if fetch_enabled and browser_enabled:
        if (not browser_urls or environ.get("AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY") != "true"):
            raise NativeError(503, "browser_configuration")
        browser = _AuditedBrowser(policy=policy, region=region,
                                 browser_identifier=environ.get("AGENTCORE_WEB_BROWSER_ID", ""),
                                 max_text=MAX_TEXT, deadline=45)
    return NativeService(
        quota=QuotaStore(table_name=environ.get("AGENTCORE_WEB_QUOTA_TABLE", ""), region=region,
                         limits=QuotaLimits(user_search=10, user_browser=5,
                                            deployment_search=30, deployment_browser=15)),
        search=GatewaySearchClient(region=region, gateway_url=environ.get("AGENTCORE_WEB_GATEWAY_URL", ""),
                                   tool_name="web-search-tool___WebSearch") if search_enabled else None,
        browser=browser, browser_urls=browser_urls, policy=policy,
        search_enabled=search_enabled, fetch_enabled=fetch_enabled, browser_enabled=browser_enabled,
    )


def _failure(error):
    if isinstance(error, NativeError):
        return error.status, error.code
    if isinstance(error, (QuotaExceeded, GatewayRateLimitError)):
        return 429, "admission_denied"
    if isinstance(error, QuotaUnavailable):
        return 503, "quota_unavailable"
    if isinstance(error, (TimeoutError, asyncio.CancelledError, GatewayTimeoutError, FetchLimitError)):
        return 504, "deadline_or_budget"
    if isinstance(error, FetchPolicyError):
        return 422, "fetch_policy"
    if isinstance(error, (GatewayError, FetchError, BrowserError)):
        return 502, "upstream_failure"
    return 502, "capability_failure"


def _safe_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


def _safe_proof(proof):
    result = {key: proof.get(key) for key in ("operation", "success", "error")}
    result["browser_start_request_id"] = _safe_id(proof.get("browser_start_request_id"))
    terminated = proof.get("session_terminated")
    result["session_terminated"] = terminated if type(terminated) is bool else None
    for key in ("broker_requests", "broker_bytes"):
        value = proof.get(key)
        result[key] = value if type(value) is int and 0 <= value <= 16 * MEBIBYTE else None
    return result


async def handle_http(event, context=None, *, service=None, secret_provider=None, environ=None):
    environ = os.environ if environ is None else environ
    request_id = str(uuid.uuid4())
    operation, error_code, proofs = None, None, []
    timeout = REQUEST_TIMEOUT
    if context is not None and callable(getattr(context, "get_remaining_time_in_millis", None)):
        timeout = min(timeout, max(0, context.get_remaining_time_in_millis() / 1000 - 1))
    deadline = time.monotonic() + timeout
    try:
        async with asyncio.timeout(timeout):
            operation, arguments, token = _request(event)
            if secret_provider is None:
                secret_provider = ScopedSecret(environ.get("AGENTCORE_WEB_SERVICE_SECRET_ARN", ""),
                                               environ.get("AGENTCORE_WEB_REGION", ""))
            try:
                key = await secret_provider()
            except NativeError:
                raise
            except Exception:
                raise NativeError(503, "secret_unavailable") from None
            if not _valid_key(key):
                raise NativeError(503, "secret_unavailable")
            if not hmac.compare_digest(token.encode("ascii"), key.encode("ascii")):
                raise NativeError(401, "authorization")
            if service is None:
                try:
                    service = build_service(environ)
                except Exception:
                    raise NativeError(503, "service_configuration") from None
            result = await service.execute(operation, arguments, request_id=request_id,
                                           deadline=deadline, proofs=proofs)
            status = 200
    except Exception as error:
        status, error_code = _failure(error)
        result = {"error": _MESSAGES[status], "code": error_code, "request_id": request_id}
    log = {"event": "native_web_capability", "operation": operation, "request_id": request_id,
           "aws_request_id": _safe_id(getattr(context, "aws_request_id", None)),
           "success": status == 200, "error": error_code, "status_code": status,
           "reads": [_safe_proof(proof) for proof in proofs]}
    print(json.dumps(log, separators=(",", ":")))
    headers = {"content-type": "application/json", "cache-control": "no-store",
               "x-content-type-options": "nosniff", "x-request-id": request_id}
    if status == 401:
        headers["www-authenticate"] = "Bearer"
    if status == 405:
        headers["allow"] = "POST"
    return {"statusCode": status, "headers": headers, "isBase64Encoded": False,
            "body": json.dumps(result, ensure_ascii=True, separators=(",", ":"))}


def handler(event, context, *, service=None, secret_provider=None, environ=None):
    """Lambda entry point; do not join potentially late OS DNS worker threads."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(handle_http(event, context, service=service,
                                                  secret_provider=secret_provider, environ=environ))
    finally:
        loop.close()

"""Private IAM-invoked, application-attested canary adapter; no HTTP endpoint.

Only the trusted Open WebUI task and authorized operators may invoke this
function. Subjects are injected by server-side Tools, not independently verified
Cognito tokens. Authorize the exact configured OWUI subjects before admission.
"""

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import asdict

from web_capabilities.brokered_browser import BrokeredBrowserFetcher
from web_capabilities.documents import DocumentParser, utf8_content_type
from web_capabilities.gateway import GatewaySearchClient
from web_capabilities.http_fetch import FetchBudget, PublicHTTPSFetcher
from web_capabilities.quota import QuotaExceeded, QuotaStore, QuotaUnavailable
from web_capabilities.url_policy import URLPolicy


class CanaryService:
    def __init__(self, *, subjects, policy, quota, search=None, browser=None,
                 http=None, search_enabled=False, fetch_enabled=False):
        if not isinstance(subjects, (list, tuple)) or not 1 <= len(subjects) <= 10 or any(
            not isinstance(subject, str) or not 1 <= len(subject) <= 256 for subject in subjects
        ):
            raise ValueError("Configure exact canary subject authorization")
        self.subjects = frozenset(subjects)
        self.policy, self.quota = policy, quota
        self.search, self.browser = search, browser
        self.http = http or PublicHTTPSFetcher(allowed_hosts=policy.hosts, max_redirects=0,
                                              max_response_bytes=1024 * 1024, total_timeout=10)
        self.search_enabled, self.fetch_enabled = search_enabled, fetch_enabled

    async def execute(self, event):
        if not isinstance(event, dict) or set(event) - {"operation", "arguments", "subject", "role", "request_id", "chat_id"}:
            raise ValueError("Invalid invocation")
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError("Invalid request correlation")
        if (not isinstance(event.get("subject"), str) or event["subject"] not in self.subjects
                or not isinstance(event.get("role"), str) or event["role"] not in {"user", "admin"}):
            return {"request_id": request_id, "error": "User is not authorized for this canary"}
        operation, arguments = event.get("operation"), event.get("arguments")
        if not isinstance(operation, str) or operation not in {"search", "browser", "static"} or not isinstance(arguments, dict):
            raise ValueError("Invalid operation")
        if operation == "search":
            if not self.search_enabled or self.search is None:
                return {"request_id": request_id, "error": "Managed search is disabled"}
            if (set(arguments) != {"query", "count"} or not isinstance(arguments["query"], str)
                    or not 1 <= len(arguments["query"]) <= 200 or not arguments["query"].strip() or type(arguments["count"]) is not int
                    or not 1 <= arguments["count"] <= 3):
                raise ValueError("Invalid search arguments")
        else:
            if not self.fetch_enabled or (operation == "browser" and self.browser is None):
                return {"request_id": request_id, "error": "Public page reading is disabled"}
            if set(arguments) != {"url"}:
                raise ValueError("Invalid reading arguments")
            canonical_url = self.policy.validate(arguments["url"])
        await asyncio.to_thread(self.quota.reserve, event["subject"], "search" if operation == "search" else "browser",
                                chat_id=event.get("chat_id"), request_id=request_id)
        if operation == "search":
            result = await self.search.search(arguments["query"], arguments["count"])
            if result.metadata.get("requires_attribution_review"):
                return {"request_id": request_id, "error": "Search attribution requires operator review"}
            records = [asdict(record) for record in result.records[:arguments["count"]]]
            return {"request_id": request_id, "records": records}
        if operation == "browser":
            document = await self.browser.load(arguments["url"])
        else:
            budget = FetchBudget(max_requests=1, max_bytes=1024 * 1024, total_timeout=12)
            response = await self.http.fetch(canonical_url, budget=budget, follow_redirects=False)
            content_type = utf8_content_type(response.content_type)
            if response.status != 200 or response.final_url != canonical_url or content_type not in {"text/html", "text/plain"}:
                raise ValueError("Unsupported direct document")
            content = response.body.decode("utf-8", errors="strict")
            if content_type == "text/plain":
                document = {"title": None, "requested_url": arguments["url"], "final_url": canonical_url,
                            "text": content[:20000], "truncated": len(content) > 20000, "links": [],
                            "source_capability": "bounded-https", "extraction": "utf8-plain-text"}
            else:
                parser = DocumentParser(canonical_url)
                parser.feed(content)
                parser.close()
                document = parser.document(arguments["url"])
            document["broker_bytes"] = budget.bytes_used
            document["broker_requests"] = budget.requests_used
        return {"request_id": request_id, "document": document}


def build_service():
    region = os.environ.get("AGENTCORE_WEB_REGION", "")
    if region not in {"us-east-1", "eu-west-1", "ap-northeast-1"}:
        raise ValueError("Explicit supported region required")
    policy = URLPolicy(json.loads(os.environ.get("AGENTCORE_WEB_URL_POLICY", "{}")))
    search_enabled = os.environ.get("AGENTCORE_WEB_SEARCH_ENABLED") == "true"
    fetch_enabled = os.environ.get("AGENTCORE_WEB_FETCH_ENABLED") == "true"
    browser_enabled = os.environ.get("AGENTCORE_WEB_BROWSER_ENABLED") == "true"
    return CanaryService(
        subjects=json.loads(os.environ.get("AGENTCORE_WEB_SUBJECTS", "[]")), policy=policy,
        quota=QuotaStore(table_name=os.environ.get("AGENTCORE_WEB_QUOTA_TABLE", ""), region=region),
        search=GatewaySearchClient(region=region, gateway_url=os.environ.get("AGENTCORE_WEB_GATEWAY_URL", ""),
                                   tool_name="web-search-tool___WebSearch") if search_enabled else None,
        browser=BrokeredBrowserFetcher(policy=policy, region=region,
            browser_identifier=os.environ.get("AGENTCORE_WEB_BROWSER_ID", "")) if fetch_enabled and browser_enabled else None,
        search_enabled=search_enabled, fetch_enabled=fetch_enabled,
    )


def handler(event, context):
    request_id = None
    try:
        candidate = event.get("request_id") if isinstance(event, dict) else None
        if isinstance(candidate, str) and str(uuid.UUID(candidate)) == candidate:
            request_id = candidate
    except (ValueError, TypeError, AttributeError):
        pass
    result = {"request_id": request_id, "error": "Web capability unavailable or rejected; no fallback attempted"}
    error_type = None
    try:
        if len(json.dumps(event).encode()) > 8192:
            raise ValueError("Invocation too large")
        result = asyncio.run(asyncio.wait_for(build_service().execute(event), timeout=50))
    except QuotaExceeded:
        error_type = "QuotaExceeded"
        result["error"] = "Daily or chat web capability quota exceeded"
    except QuotaUnavailable:
        error_type = "QuotaUnavailable"
        result["error"] = "Web capability quota service unavailable"
    except Exception as error:
        error_type = type(error).__name__
    subject = event.get("subject") if isinstance(event, dict) else None
    operation = event.get("operation") if isinstance(event, dict) else None
    subject_hash = None
    try:
        if isinstance(subject, str) and 1 <= len(subject) <= 256:
            subject_hash = hashlib.sha256(subject.encode()).hexdigest()
    except UnicodeError:
        pass
    document = result.get("document", {})
    print(json.dumps({"event": "web_capability", "request_id": request_id,
        "subject_hash": subject_hash,
        "operation": operation if isinstance(operation, str) and operation in {"search", "browser", "static"} else None,
        "success": "error" not in result, "aws_request_id": context.aws_request_id,
        "error_type": error_type,
        "browser_start_request_id": document.get("aws_start_request_id"),
        "session_terminated": document.get("session_terminated"),
        "broker_requests": document.get("broker_requests"), "broker_bytes": document.get("broker_bytes")}))
    return result

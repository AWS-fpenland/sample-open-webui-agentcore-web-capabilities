import asyncio
import time

import httpx
import jwt
import pytest

from web_capabilities.app import Settings, create_app
from web_capabilities.auth import Authenticator
from web_capabilities.search import SearchRecord, SearchResponse
from web_capabilities.browser import BrowserError, BrowserPolicyError
from web_capabilities.gateway import GatewayRateLimitError, GatewayTimeoutError


SERVICE = "synthetic-service-" * 3
SECRET = "synthetic-identity-" * 3


def headers(subject="synthetic-one"):
    now = int(time.time())
    token = jwt.encode({"sub": subject, "role": "user", "iss": "open-webui", "iat": now, "exp": now + 300}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + SERVICE, "X-OpenWebUI-User-Jwt": token}


def request(app, path="/search", **kwargs):
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://adapter") as client:
            return await client.post(path, **kwargs)

    return asyncio.run(invoke())


class Search:
    async def search(self, query, count):
        assert query == "synthetic"
        return SearchResponse((SearchRecord("https://example.com/", None, "snippet", {}, {}),), 0, {})


def build(**kwargs):
    return create_app(Authenticator(SERVICE, SECRET), search_client=Search(), **kwargs)


def test_off_by_default_and_plain_user_headers_not_trusted():
    app = build()
    assert request(app, headers=headers(), json={"query": "synthetic"}).status_code == 503
    assert request(app, headers={"Authorization": "Bearer " + SERVICE, "X-OpenWebUI-User-Id": "admin"}, json={}).status_code == 401


def test_native_search_bare_array_contract():
    response = request(build(settings=Settings(search_enabled=True)), headers=headers(), json={"query": "synthetic", "count": 3})
    assert response.status_code == 200
    assert response.json() == [{"link": "https://example.com/", "title": None, "snippet": "snippet"}]


def test_validation_does_not_echo_content_and_body_is_bounded():
    app = build(settings=Settings(search_enabled=True))
    response = request(app, headers=headers(), json={"query": "sensitive", "count": True})
    assert response.status_code == 422 and "sensitive" not in response.text
    response = request(app, headers=headers(), content=b"x" * 9000)
    assert response.status_code == 413


def test_subject_rate_limits_are_isolated():
    app = build(settings=Settings(search_enabled=True, requests_per_minute=1))
    assert request(app, headers=headers(), json={"query": "synthetic"}).status_code == 200
    assert request(app, headers=headers(), json={"query": "synthetic"}).status_code == 429
    assert request(app, headers=headers("synthetic-two"), json={"query": "synthetic"}).status_code == 200


def test_browser_is_explicit_and_not_global_loader():
    app = build()
    assert request(app, "/browser", headers=headers(), json={"url": "https://example.com"}).status_code == 503
    assert request(app, "/loader", headers=headers(), json={"urls": ["https://example.com"]}).status_code == 404


def test_upstream_errors_are_opaque():
    class Broken:
        async def search(self, query, count):
            raise RuntimeError("secret token and upstream document")

    app = create_app(Authenticator(SERVICE, SECRET), search_client=Broken(), settings=Settings(search_enabled=True))
    response = request(app, headers=headers(), json={"query": "synthetic"})
    assert response.status_code == 502
    assert "secret" not in response.text


@pytest.mark.parametrize("error,status", [(GatewayRateLimitError("private"), 429), (GatewayTimeoutError("private"), 504)])
def test_gateway_error_classification(error, status):
    class Broken:
        async def search(self, query, count):
            raise error

    app = create_app(Authenticator(SERVICE, SECRET), search_client=Broken(), settings=Settings(search_enabled=True))
    response = request(app, headers=headers(), json={"query": "synthetic"})
    assert response.status_code == status and "private" not in response.text


@pytest.mark.parametrize("error,status", [(BrowserPolicyError("private"), 400), (BrowserError("private"), 502)])
def test_browser_denial_is_distinct_from_service_failure(error, status):
    class Broken:
        async def load(self, url):
            raise error

    app = create_app(Authenticator(SERVICE, SECRET), browser_loader=Broken(), settings=Settings(browser_enabled=True))
    response = request(app, "/browser", headers=headers(), json={"url": "https://example.com"})
    assert response.status_code == status and "private" not in response.text


def test_unknown_attribution_is_not_silently_projected_to_native_results():
    class NeedsReview:
        async def search(self, query, count):
            return SearchResponse((), 0, {"requires_attribution_review": True})

    app = create_app(Authenticator(SERVICE, SECRET), search_client=NeedsReview(), settings=Settings(search_enabled=True))
    response = request(app, headers=headers(), json={"query": "synthetic"})
    assert response.status_code == 502
    assert "attribution requires review" in response.text

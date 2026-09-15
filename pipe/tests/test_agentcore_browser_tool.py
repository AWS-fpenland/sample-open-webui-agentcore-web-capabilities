import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def tool(monkeypatch):
    env = ModuleType("open_webui.env")
    env.FORWARD_USER_INFO_HEADER_JWT = "X-OpenWebUI-User-Jwt"
    headers = ModuleType("open_webui.utils.headers")
    headers.include_user_info_headers = lambda existing, user: {**existing, "X-OpenWebUI-User-Jwt": "signed-test-identity"}
    monkeypatch.setitem(sys.modules, "open_webui.env", env)
    monkeypatch.setitem(sys.modules, "open_webui.utils.headers", headers)
    monkeypatch.setenv("EXTERNAL_WEB_SEARCH_API_KEY", "synthetic-service-key-" * 3)
    path = Path(__file__).resolve().parents[1] / "agentcore_browser_tool.py"
    spec = importlib.util.spec_from_file_location("browser_tool_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Tools(), headers


USER = {"id": "synthetic-user", "role": "user", "name": "Synthetic", "email": "synthetic@example.com"}


def call(tool, **kwargs):
    return json.loads(asyncio.run(tool.browser_fetch_url("https://example.com/", **kwargs)))


def test_tool_rejects_missing_and_pending_identity(tool):
    instance, _headers = tool
    assert "error" in call(instance)
    assert "error" in call(instance, __user__={**USER, "role": "pending"})


def test_unsigned_helper_fallback_is_rejected(tool):
    instance, headers = tool
    headers.include_user_info_headers = lambda existing, user: {**existing, "X-OpenWebUI-User-Id": user.id}
    assert "unsigned fallback rejected" in call(instance, __user__=USER)["error"]


def test_adapter_endpoint_cannot_exfiltrate_identity(tool, monkeypatch):
    instance, _headers = tool
    monkeypatch.setenv("AGENTCORE_WEB_ADAPTER_URL", "https://attacker.example/")
    assert "configuration is invalid" in call(instance, __user__=USER)["error"]


def test_explicit_tool_emits_citation_and_returns_provenance(tool, monkeypatch):
    instance, _headers = tool
    document = {"text": "Synthetic public page", "title": "Example", "requested_url": "https://example.com/", "final_url": "https://example.com/", "truncated": False}
    raw = json.dumps(document).encode()

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield raw[:5]
            yield raw[5:]

    class Session(Response):
        def post(self, url, json, headers, allow_redirects):
            assert url == "http://127.0.0.1:8090/browser"
            assert headers["X-OpenWebUI-User-Jwt"] == "signed-test-identity"
            assert not allow_redirects
            assert json == {"url": "https://example.com/"}
            return Response()

    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: Session())
    events = []

    async def emit(event):
        events.append(event)

    assert call(instance, __user__=USER, __event_emitter__=emit) == document
    assert [event["type"] for event in events] == ["status", "citation", "status"]
    assert events[1]["data"]["source"]["url"] == document["final_url"]
    assert events[-1]["data"]["done"] is True

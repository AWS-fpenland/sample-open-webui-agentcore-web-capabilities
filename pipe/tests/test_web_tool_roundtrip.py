"""Offline protocol evidence, not proof of model or live native-tool support."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def pipe():
    path = Path(__file__).resolve().parents[1] / "gateway_anthropic_pipe.py"
    spec = importlib.util.spec_from_file_location("web_tool_pipe_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Pipe()


def test_search_and_fetch_tool_results_keep_call_ids(pipe):
    calls = [
        {
            "id": "search-call",
            "type": "function",
            "function": {
                "name": "search_web",
                "arguments": json.dumps({"query": "synthetic public documentation"}),
            },
        },
        {
            "id": "fetch-call",
            "type": "function",
            "function": {
                "name": "fetch_url",
                "arguments": json.dumps({"url": "https://example.com/"}),
            },
        },
    ]
    body = {
        "messages": [
            {"role": "user", "content": "Synthetic web tool contract test"},
            {"role": "assistant", "content": "", "tool_calls": calls},
            {"role": "tool", "tool_call_id": "fetch-call", "content": "Page text"},
            {"role": "tool", "tool_call_id": "search-call", "content": "[]"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Find public pages",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ],
    }
    payload = pipe._to_messages_payload("anthropic.synthetic", body)
    blocks = payload["messages"][1]["content"]
    assert [block["id"] for block in blocks] == ["search-call", "fetch-call"]
    assert blocks[0]["input"] == {"query": "synthetic public documentation"}
    results = [message["content"][0] for message in payload["messages"][2:]]
    assert [result["tool_use_id"] for result in results] == ["fetch-call", "search-call"]
    assert results[1]["content"] == "[]"
    assert payload["tools"][0]["input_schema"] == body["tools"][0]["function"]["parameters"]


def test_messages_response_preserves_multiple_tool_calls(pipe):
    response = pipe._openai_response(
        {
            "id": "synthetic-response",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "call-one", "name": "search_web", "input": {"query": "one"}},
                {"type": "tool_use", "id": "call-two", "name": "search_web", "input": {"query": "two"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        "anthropic.synthetic",
    )
    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert [call["id"] for call in choice["message"]["tool_calls"]] == ["call-one", "call-two"]
    assert json.loads(choice["message"]["tool_calls"][1]["function"]["arguments"]) == {"query": "two"}
    assert response["usage"]["total_tokens"] == 30


def test_missing_oauth_does_not_enable_shared_iam_fallback(pipe):
    assert pipe.valves.SIGV4_FALLBACK is False


def test_citation_text_is_not_rewritten_by_messages_translation(pipe):
    text = "Synthetic answer [1](https://example.com/)."
    response = pipe._openai_response(
        {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"},
        "anthropic.synthetic",
    )
    assert response["choices"][0]["message"]["content"] == text
    assert response["choices"][0]["finish_reason"] == "stop"


def test_missing_session_rejects_before_http_call(pipe, monkeypatch):
    def unexpected_session(*args, **kwargs):
        pytest.fail("Missing OAuth must not start an HTTP session")

    monkeypatch.setattr("aiohttp.ClientSession", unexpected_session)
    with pytest.raises(Exception, match="requires the logged-in"):
        asyncio.run(pipe.pipe({"model": "gateway_anthropic.anthropic.synthetic", "messages": []}))


def test_streamed_search_arguments_and_cleanup(pipe, monkeypatch):
    events = [
        {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "search-id", "name": "search_web"}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{\"query\":"}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "\"synthetic\"}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    ]

    class Response:
        status = 200

        @property
        def content(self):
            async def lines():
                for event in events:
                    yield ("data: " + json.dumps(event) + "\n").encode()

            return lines()

    class Session:
        closed = False

        async def post(self, url, data, headers):
            assert url == "https://gateway.example/inference/v1/messages"
            assert headers["Authorization"] == "Bearer synthetic-test-token"
            return Response()

        async def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: session)

    async def bearer(*args):
        return "synthetic-test-token"

    monkeypatch.setattr(pipe, "_user_bearer", bearer)
    pipe.valves.GATEWAY_INFERENCE_URL = "https://gateway.example/inference"

    async def collect():
        stream = await pipe.pipe(
            {"model": "gateway_anthropic.anthropic.synthetic", "messages": [], "stream": True},
            __request__=object(),
        )
        return [chunk async for chunk in stream]

    chunks = asyncio.run(collect())
    calls = [
        call
        for chunk in chunks
        for choice in chunk["choices"]
        for call in choice["delta"].get("tool_calls", [])
    ]
    assert calls[0]["id"] == "search-id"
    assert {call["index"] for call in calls} == {0}
    assert json.loads("".join(call["function"].get("arguments", "") for call in calls)) == {"query": "synthetic"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert session.closed

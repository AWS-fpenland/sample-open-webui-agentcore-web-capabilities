# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live capability probes per catalogue entry per lane.

Each probe returns a ``ProbeResult`` with pass/fail, latency (ms; TTFT for streaming), token usage, the raw
error code/message on failure, and lane-specific detail. Probes are deliberately tiny (≤ 1.5K output tokens)
so a full sweep over ~130 entries costs a few dollars; every call's usage is recorded so the sweep's own
cost is reported from the same Price List rates the picker shows.

Lanes: ``converse`` (boto3 bedrock-runtime), ``mantle_chat`` (POST /v1/chat/completions),
``mantle_responses`` (POST /v1/responses), ``mantle_messages`` (POST /anthropic/v1/messages). Mantle calls use a
short-term Bedrock API key minted from the caller's IAM credentials (``aws-bedrock-token-generator``) — the same
keyless path the runtime uses; no static keys anywhere.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

PROBES = ("plain", "system", "stream", "tools", "json", "long", "reasoning", "temperature")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_TOOL = {
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}
_LONG_PROMPT = (
    "Write a 600-word essay on the history of the printing press with five section headings "
    "(use Markdown ## headings). Do not stop early."
)


@dataclass
class ProbeResult:
    probe: str
    ok: bool
    ms: int
    ttft_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None
    error_message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        m = _JSON_RE.search(t)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def reasoning_fields(family: str, lane: str) -> dict[str, Any] | None:
    """Provider-specific reasoning control for the reasoning probe (None = no documented control → skipped)."""
    if lane == "converse":
        return {
            "claude": {"thinking": {"type": "enabled", "budget_tokens": 1024}},
            "gpt": {"reasoning_effort": "low"},
            "gpt-oss": {"reasoning_effort": "low"},
            "nemotron": {"reasoning_effort": "low"},
            "grok": {"reasoning_effort": "low"},
            "nova": {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"}},
            "qwen": {"reasoning_effort": "low"},
            "glm": {"reasoning_effort": "low"},
            "kimi": {"reasoning_effort": "low"},
            "deepseek": {"reasoning_effort": "low"},
            "minimax": {"reasoning_effort": "low"},
            "gemma": {"reasoning_effort": "low"},
            "mistral": {"reasoning_effort": "low"},
            "llama": None,
            "palmyra": None,
        }.get(family)
    if lane in ("mantle_chat", "mantle_responses"):
        return {"reasoning_effort": "low"} if lane == "mantle_chat" else {"reasoning": {"effort": "low"}}
    if lane == "mantle_messages":
        return {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    return None


# --------------------------------------------------------------------------------------- converse lane --


class ConverseProber:
    def __init__(self, region: str, session=None):
        import boto3
        from botocore.config import Config

        self._client = (session or boto3.Session()).client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=180, connect_timeout=10, retries={"mode": "standard", "max_attempts": 2}),
        )

    @staticmethod
    def _text(resp: dict) -> tuple[str, bool]:
        blocks = ((resp.get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks if "text" in b)
        reasoning = any("reasoningContent" in b for b in blocks)
        return text, reasoning

    def _call(
        self,
        model_id: str,
        messages: list,
        *,
        system: str | None = None,
        max_tokens: int = 256,
        tool_config: dict | None = None,
        extra: dict | None = None,
    ) -> dict:
        kw: dict[str, Any] = {"modelId": model_id, "messages": messages, "inferenceConfig": {"maxTokens": max_tokens}}
        if system:
            kw["system"] = [{"text": system}]
        if tool_config:
            kw["toolConfig"] = tool_config
        if extra:
            kw["additionalModelRequestFields"] = extra
        return self._client.converse(**kw)

    def _wrap(self, probe: str, fn: Callable[[], ProbeResult]) -> ProbeResult:
        t0 = time.monotonic()
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — the raw error IS the evidence
            resp = getattr(e, "response", None) or {}
            err = resp.get("Error") or {}
            return ProbeResult(
                probe,
                False,
                int((time.monotonic() - t0) * 1000),
                error_code=err.get("Code") or e.__class__.__name__,
                error_message=str(err.get("Message") or e)[:400],
            )

    def plain(self, model_id: str) -> ProbeResult:
        def run():
            t0 = time.monotonic()
            r = self._call(model_id, [{"role": "user", "content": [{"text": "Reply with exactly the word OK."}]}])
            text, reasoning = self._text(r)
            u = r.get("usage", {})
            return ProbeResult(
                "plain",
                "ok" in text.lower(),
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"text": text[:80], "stop": r.get("stopReason"), "reasoning_blocks": reasoning},
            )

        return self._wrap("plain", run)

    def system(self, model_id: str) -> ProbeResult:
        def run():
            t0 = time.monotonic()
            r = self._call(
                model_id,
                [{"role": "user", "content": [{"text": "Say hello in one short sentence."}]}],
                system="You are a pirate. Every reply MUST begin with the word 'Arr'.",
            )
            text, _ = self._text(r)
            u = r.get("usage", {})
            return ProbeResult(
                "system",
                text.strip().lower().startswith("arr"),
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"text": text[:80]},
            )

        return self._wrap("system", run)

    def stream(self, model_id: str) -> ProbeResult:
        def run():
            t0 = time.monotonic()
            r = self._client.converse_stream(
                modelId=model_id,
                inferenceConfig={"maxTokens": 256},
                messages=[{"role": "user", "content": [{"text": "Count from 1 to 10 separated by spaces."}]}],
            )
            ttft = None
            chunks = 0
            text = ""
            usage = {}
            for ev in r["stream"]:
                if "contentBlockDelta" in ev:
                    d = ev["contentBlockDelta"].get("delta", {})
                    if "text" in d:
                        if ttft is None:
                            ttft = int((time.monotonic() - t0) * 1000)
                        chunks += 1
                        text += d["text"]
                if "metadata" in ev:
                    usage = ev["metadata"].get("usage", {})
            return ProbeResult(
                "stream",
                chunks >= 2 and "10" in text,
                int((time.monotonic() - t0) * 1000),
                ttft_ms=ttft,
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                detail={"chunks": chunks},
            )

        return self._wrap("stream", run)

    def tools(self, model_id: str) -> ProbeResult:
        def run():
            t0 = time.monotonic()
            tc = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": _TOOL["name"],
                            "description": _TOOL["description"],
                            "inputSchema": {"json": _TOOL["schema"]},
                        }
                    }
                ]
            }
            r = self._call(
                model_id,
                [{"role": "user", "content": [{"text": "What is the weather in Paris right now? Use the tool."}]}],
                tool_config=tc,
                max_tokens=512,
            )
            blocks = ((r.get("output") or {}).get("message") or {}).get("content") or []
            calls = [b["toolUse"] for b in blocks if "toolUse" in b]
            ok = any(c.get("name") == "get_weather" and "paris" in json.dumps(c.get("input", {})).lower() for c in calls)
            u = r.get("usage", {})
            return ProbeResult(
                "tools",
                ok,
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"stop": r.get("stopReason"), "calls": [c.get("name") for c in calls]},
            )

        return self._wrap("tools", run)

    def json(self, model_id: str) -> ProbeResult:
        def run():
            t0 = time.monotonic()
            r = self._call(
                model_id,
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": "Return ONLY a JSON object (no prose, no code fence) "
                                'with keys "city" (string) and "population" (integer) for Paris.'
                            }
                        ],
                    }
                ],
                max_tokens=400,
            )
            text, _ = self._text(r)
            obj = _extract_json(text)
            ok = bool(obj) and "city" in obj and isinstance(obj.get("population"), int)
            u = r.get("usage", {})
            return ProbeResult(
                "json",
                ok,
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"text": text[:120]},
            )

        return self._wrap("json", run)

    def long(self, model_id: str) -> ProbeResult:
        def run():
            t0 = time.monotonic()
            r = self._call(model_id, [{"role": "user", "content": [{"text": _LONG_PROMPT}]}], max_tokens=1500)
            text, _ = self._text(r)
            words = len(text.split())
            u = r.get("usage", {})
            return ProbeResult(
                "long",
                words >= 350 and text.count("##") >= 3,
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"words": words, "headings": text.count("##"), "stop": r.get("stopReason")},
            )

        return self._wrap("long", run)

    def temperature(self, model_id: str) -> ProbeResult:
        """Does the model accept a sampling `temperature`? (Claude Sonnet 5 on Mantle rejects it — parameter × model matters.)"""

        def run():
            t0 = time.monotonic()
            r = self._client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "Reply with exactly the word OK."}]}],
                inferenceConfig={"maxTokens": 64, "temperature": 0.2},
            )
            text, _ = self._text(r)
            u = r.get("usage", {})
            return ProbeResult(
                "temperature",
                "ok" in text.lower(),
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"temperature": 0.2},
            )

        return self._wrap("temperature", run)

    def reasoning(self, model_id: str, family: str) -> ProbeResult:
        fields = reasoning_fields(family, "converse")
        if fields is None:
            return ProbeResult("reasoning", False, 0, error_code="not_applicable", detail={"skipped": True})

        def run():
            t0 = time.monotonic()
            r = self._call(
                model_id,
                [{"role": "user", "content": [{"text": "What is 17 * 23? Think step by step, then answer."}]}],
                max_tokens=2048,
                extra=fields,
            )
            text, reasoning = self._text(r)
            u = r.get("usage", {})
            return ProbeResult(
                "reasoning",
                "391" in text,
                int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("inputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                detail={"fields": fields, "reasoning_blocks": reasoning, "text": text[-60:]},
            )

        return self._wrap("reasoning", run)


# ----------------------------------------------------------------------------------------- mantle lanes --


class MantleProber:
    def __init__(self, region: str, token: str, timeout: float = 180.0):
        import httpx

        self.base = f"https://bedrock-mantle.{region}.api.aws"
        self._h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10))

    @staticmethod
    def mint_token(region: str) -> str:
        from aws_bedrock_token_generator import provide_token

        return provide_token(region=region)

    def _post(self, path: str, body: dict, headers: dict | None = None, stream: bool = False):
        h = {**self._h, **(headers or {})}
        if stream:
            return self._client.stream("POST", self.base + path, headers=h, json=body)
        return self._client.post(self.base + path, headers=h, json=body)

    @staticmethod
    def _fail(probe: str, t0: float, r) -> ProbeResult:
        try:
            err = r.json().get("error") or {}
        except Exception:  # noqa: BLE001
            err = {}
        return ProbeResult(
            probe,
            False,
            int((time.monotonic() - t0) * 1000),
            error_code=f"http_{r.status_code}:{err.get('code') or err.get('type') or ''}",
            error_message=(err.get("message") or r.text)[:400],
        )

    # ---- chat/completions
    def chat(self, probe: str, model_id: str, family: str) -> ProbeResult:
        t0 = time.monotonic()
        try:
            if probe == "stream":
                body = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "Count from 1 to 10 separated by spaces."}],
                    "max_tokens": 256,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                ttft = None
                chunks = 0
                text = ""
                usage = {}
                with self._post("/v1/chat/completions", body, stream=True) as r:
                    if r.status_code != 200:
                        r.read()
                        return self._fail(probe, t0, r)
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("usage"):
                            usage = obj["usage"]
                        for ch in obj.get("choices") or []:
                            d = (ch.get("delta") or {}).get("content")
                            if d:
                                if ttft is None:
                                    ttft = int((time.monotonic() - t0) * 1000)
                                chunks += 1
                                text += d
                return ProbeResult(
                    probe,
                    chunks >= 2 and "10" in text,
                    int((time.monotonic() - t0) * 1000),
                    ttft_ms=ttft,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    detail={"chunks": chunks},
                )
            body: dict[str, Any] = {"model": model_id, "max_tokens": 256}
            if probe == "plain":
                body["messages"] = [{"role": "user", "content": "Reply with exactly the word OK."}]
            elif probe == "system":
                body["messages"] = [
                    {"role": "system", "content": "You are a pirate. Every reply MUST begin with the word 'Arr'."},
                    {"role": "user", "content": "Say hello in one short sentence."},
                ]
            elif probe == "tools":
                body["messages"] = [{"role": "user", "content": "What is the weather in Paris right now? Use the tool."}]
                body["tools"] = [
                    {
                        "type": "function",
                        "function": {"name": _TOOL["name"], "description": _TOOL["description"], "parameters": _TOOL["schema"]},
                    }
                ]
                body["max_tokens"] = 512
            elif probe == "json":
                body["messages"] = [
                    {
                        "role": "user",
                        "content": 'Return ONLY a JSON object with keys "city" (string) and "population" (integer) for Paris.',
                    }
                ]
                body["response_format"] = {"type": "json_object"}
                body["max_tokens"] = 400
            elif probe == "long":
                body["messages"] = [{"role": "user", "content": _LONG_PROMPT}]
                body["max_tokens"] = 1500
            elif probe == "reasoning":
                body["messages"] = [{"role": "user", "content": "What is 17 * 23? Think step by step, then answer."}]
                body["max_tokens"] = 2048
                body.update(reasoning_fields(family, "mantle_chat") or {})
            elif probe == "temperature":
                body["messages"] = [{"role": "user", "content": "Reply with exactly the word OK."}]
                body["max_tokens"] = 64
                body["temperature"] = 0.2
            r = self._post("/v1/chat/completions", body)
            if r.status_code != 200 and probe == "json":
                # retry without response_format: the capability is "JSON on request", the mode is a bonus
                body.pop("response_format", None)
                r2 = self._post("/v1/chat/completions", body)
                if r2.status_code == 200:
                    r = r2
            if r.status_code != 200:
                return self._fail(probe, t0, r)
            obj = r.json()
            ch = (obj.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            text = msg.get("content") or ""
            usage = obj.get("usage") or {}
            ms = int((time.monotonic() - t0) * 1000)
            detail = {"finish": ch.get("finish_reason"), "reasoning": bool(msg.get("reasoning") or msg.get("reasoning_content"))}
            if probe in ("plain", "temperature"):
                ok = "ok" in text.lower()
            elif probe == "system":
                ok = text.strip().lower().startswith("arr")
            elif probe == "tools":
                calls = msg.get("tool_calls") or []
                ok = any(
                    (c.get("function") or {}).get("name") == "get_weather"
                    and "paris" in ((c.get("function") or {}).get("arguments") or "").lower()
                    for c in calls
                )
                detail["calls"] = [(c.get("function") or {}).get("name") for c in calls]
            elif probe == "json":
                o = _extract_json(text)
                ok = bool(o) and "city" in o and isinstance(o.get("population"), int)
                detail["json_mode_accepted"] = "response_format" in body
            elif probe == "long":
                words = len(text.split())
                ok = words >= 350 and text.count("##") >= 3
                detail.update(words=words, headings=text.count("##"))
            else:
                ok = "391" in text
            detail["text"] = text[:80]
            return ProbeResult(
                probe,
                ok,
                ms,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                detail=detail,
            )
        except Exception as e:  # noqa: BLE001
            return ProbeResult(
                probe, False, int((time.monotonic() - t0) * 1000), error_code=e.__class__.__name__, error_message=str(e)[:400]
            )

    # ---- responses
    def responses(self, probe: str, model_id: str, family: str) -> ProbeResult:
        t0 = time.monotonic()
        try:
            if probe == "stream":
                body = {
                    "model": model_id,
                    "input": "Count from 1 to 10 separated by spaces.",
                    "max_output_tokens": 256,
                    "stream": True,
                }
                ttft = None
                chunks = 0
                text = ""
                usage = {}
                with self._post("/v1/responses", body, stream=True) as r:
                    if r.status_code != 200:
                        r.read()
                        return self._fail(probe, t0, r)
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            obj = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "response.output_text.delta":
                            if ttft is None:
                                ttft = int((time.monotonic() - t0) * 1000)
                            chunks += 1
                            text += obj.get("delta", "")
                        if obj.get("type") == "response.completed":
                            usage = (obj.get("response") or {}).get("usage") or {}
                return ProbeResult(
                    probe,
                    chunks >= 2 and "10" in text,
                    int((time.monotonic() - t0) * 1000),
                    ttft_ms=ttft,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    detail={"chunks": chunks},
                )
            body: dict[str, Any] = {"model": model_id, "max_output_tokens": 256}
            if probe == "plain":
                body["input"] = "Reply with exactly the word OK."
            elif probe == "system":
                body["instructions"] = "You are a pirate. Every reply MUST begin with the word 'Arr'."
                body["input"] = "Say hello in one short sentence."
            elif probe == "tools":
                body["input"] = "What is the weather in Paris right now? Use the tool."
                body["tools"] = [
                    {
                        "type": "function",
                        "name": _TOOL["name"],
                        "description": _TOOL["description"],
                        "parameters": _TOOL["schema"],
                    }
                ]
                body["max_output_tokens"] = 512
            elif probe == "json":
                body["input"] = 'Return ONLY a JSON object with keys "city" (string) and "population" (integer) for Paris.'
                body["text"] = {"format": {"type": "json_object"}}
                body["max_output_tokens"] = 400
            elif probe == "long":
                body["input"] = _LONG_PROMPT
                body["max_output_tokens"] = 1500
            elif probe == "reasoning":
                body["input"] = "What is 17 * 23? Think step by step, then answer."
                body["max_output_tokens"] = 2048
                body.update(reasoning_fields(family, "mantle_responses") or {})
            elif probe == "temperature":
                body["input"] = "Reply with exactly the word OK."
                body["max_output_tokens"] = 64
                body["temperature"] = 0.2
            r = self._post("/v1/responses", body)
            if r.status_code != 200 and probe == "json":
                body.pop("text", None)
                r2 = self._post("/v1/responses", body)
                if r2.status_code == 200:
                    r = r2
            if r.status_code != 200:
                return self._fail(probe, t0, r)
            obj = r.json()
            text = ""
            calls = []
            for item in obj.get("output") or []:
                if item.get("type") == "message":
                    for c in item.get("content") or []:
                        if c.get("type") == "output_text":
                            text += c.get("text", "")
                elif item.get("type") == "function_call":
                    calls.append(item)
            usage = obj.get("usage") or {}
            ms = int((time.monotonic() - t0) * 1000)
            detail: dict[str, Any] = {"status": obj.get("status")}
            if probe in ("plain", "temperature"):
                ok = "ok" in text.lower()
            elif probe == "system":
                ok = text.strip().lower().startswith("arr")
            elif probe == "tools":
                ok = any(c.get("name") == "get_weather" and "paris" in (c.get("arguments") or "").lower() for c in calls)
                detail["calls"] = [c.get("name") for c in calls]
            elif probe == "json":
                o = _extract_json(text)
                ok = bool(o) and "city" in o and isinstance(o.get("population"), int)
                detail["json_mode_accepted"] = "text" in body
            elif probe == "long":
                words = len(text.split())
                ok = words >= 350 and text.count("##") >= 3
                detail.update(words=words, headings=text.count("##"))
            else:
                ok = "391" in text
            detail["text"] = text[:80]
            return ProbeResult(
                probe,
                ok,
                ms,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                detail=detail,
            )
        except Exception as e:  # noqa: BLE001
            return ProbeResult(
                probe, False, int((time.monotonic() - t0) * 1000), error_code=e.__class__.__name__, error_message=str(e)[:400]
            )

    # ---- anthropic messages
    def messages(self, probe: str, model_id: str, family: str) -> ProbeResult:
        t0 = time.monotonic()
        hdr = {"anthropic-version": "2023-06-01"}
        try:
            body: dict[str, Any] = {"model": model_id, "max_tokens": 256, "anthropic_version": "bedrock-2023-05-31"}
            if probe == "stream":
                body.update(messages=[{"role": "user", "content": "Count from 1 to 10 separated by spaces."}], stream=True)
                ttft = None
                chunks = 0
                text = ""
                usage: dict[str, Any] = {}
                with self._post("/anthropic/v1/messages", body, headers=hdr, stream=True) as r:
                    if r.status_code != 200:
                        r.read()
                        return self._fail(probe, t0, r)
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            obj = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "content_block_delta" and (obj.get("delta") or {}).get("type") == "text_delta":
                            if ttft is None:
                                ttft = int((time.monotonic() - t0) * 1000)
                            chunks += 1
                            text += obj["delta"].get("text", "")
                        if obj.get("type") == "message_start":
                            usage.update((obj.get("message") or {}).get("usage") or {})
                        if obj.get("type") == "message_delta":
                            usage.update(obj.get("usage") or {})
                return ProbeResult(
                    probe,
                    chunks >= 2 and "10" in text,
                    int((time.monotonic() - t0) * 1000),
                    ttft_ms=ttft,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    detail={"chunks": chunks},
                )
            if probe == "plain":
                body["messages"] = [{"role": "user", "content": "Reply with exactly the word OK."}]
            elif probe == "system":
                body["system"] = "You are a pirate. Every reply MUST begin with the word 'Arr'."
                body["messages"] = [{"role": "user", "content": "Say hello in one short sentence."}]
            elif probe == "tools":
                body["messages"] = [{"role": "user", "content": "What is the weather in Paris right now? Use the tool."}]
                body["tools"] = [{"name": _TOOL["name"], "description": _TOOL["description"], "input_schema": _TOOL["schema"]}]
                body["max_tokens"] = 512
            elif probe == "json":
                body["messages"] = [
                    {
                        "role": "user",
                        "content": 'Return ONLY a JSON object (no prose, no code fence) with keys "city" (string) and "population" (integer) for Paris.',  # noqa: E501
                    }
                ]
                body["max_tokens"] = 400
            elif probe == "long":
                body["messages"] = [{"role": "user", "content": _LONG_PROMPT}]
                body["max_tokens"] = 1500
            elif probe == "reasoning":
                body["messages"] = [{"role": "user", "content": "What is 17 * 23? Think step by step, then answer."}]
                body["max_tokens"] = 2048
                body.update(reasoning_fields(family, "mantle_messages") or {})
            elif probe == "temperature":
                body["messages"] = [{"role": "user", "content": "Reply with exactly the word OK."}]
                body["max_tokens"] = 64
                body["temperature"] = 0.2
            r = self._post("/anthropic/v1/messages", body, headers=hdr)
            if r.status_code != 200:
                return self._fail(probe, t0, r)
            obj = r.json()
            text = "".join(c.get("text", "") for c in obj.get("content") or [] if c.get("type") == "text")
            calls = [c for c in obj.get("content") or [] if c.get("type") == "tool_use"]
            thinking = any(c.get("type") == "thinking" for c in obj.get("content") or [])
            usage = obj.get("usage") or {}
            ms = int((time.monotonic() - t0) * 1000)
            detail: dict[str, Any] = {"stop": obj.get("stop_reason"), "thinking_blocks": thinking}
            if probe in ("plain", "temperature"):
                ok = "ok" in text.lower()
            elif probe == "system":
                ok = text.strip().lower().startswith("arr")
            elif probe == "tools":
                ok = any(c.get("name") == "get_weather" and "paris" in json.dumps(c.get("input") or {}).lower() for c in calls)
                detail["calls"] = [c.get("name") for c in calls]
            elif probe == "json":
                o = _extract_json(text)
                ok = bool(o) and "city" in o and isinstance(o.get("population"), int)
            elif probe == "long":
                words = len(text.split())
                ok = words >= 350 and text.count("##") >= 3
                detail.update(words=words, headings=text.count("##"))
            else:
                ok = "391" in text
            detail["text"] = text[:80]
            return ProbeResult(
                probe,
                ok,
                ms,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                detail=detail,
            )
        except Exception as e:  # noqa: BLE001
            return ProbeResult(
                probe, False, int((time.monotonic() - t0) * 1000), error_code=e.__class__.__name__, error_message=str(e)[:400]
            )


# --------------------------------------------------------------------------------------------- runner --


def probe_entry(
    entry: dict[str, Any], converse: ConverseProber | None, mantle: dict[str, MantleProber], probes: tuple[str, ...] = PROBES
) -> dict[str, ProbeResult]:
    lane, mid, fam = entry["lane"], entry["id"], entry.get("family", "")
    out: dict[str, ProbeResult] = {}
    if lane == "converse":
        assert converse is not None
        fns = {
            "plain": converse.plain,
            "system": converse.system,
            "stream": converse.stream,
            "tools": converse.tools,
            "json": converse.json,
            "long": converse.long,
            "reasoning": lambda m: converse.reasoning(m, fam),
            "temperature": converse.temperature,
        }
        for p in probes:
            out[p] = fns[p](mid)
            if (
                p == "plain"
                and not out[p].ok
                and out[p].error_code
                in ("AccessDeniedException", "ResourceNotFoundException", "ValidationException", "ModelNotReadyException")
            ):
                # the lane itself is closed for this id; no point spending on the remaining probes
                for q in probes:
                    if q not in out:
                        out[q] = ProbeResult(q, False, 0, error_code="skipped_after_plain_failure")
                break
        return out
    prober = mantle.get(entry["region"])
    if prober is None:
        return {p: ProbeResult(p, False, 0, error_code="no_mantle_prober_for_region") for p in probes}
    fn = {"mantle_chat": prober.chat, "mantle_responses": prober.responses, "mantle_messages": prober.messages}[lane]
    for p in probes:
        out[p] = fn(p, mid, fam)
        if (
            p == "plain"
            and not out[p].ok
            and (out[p].error_code or "").startswith(("http_400", "http_401", "http_403", "http_404"))
        ):
            for q in probes:
                if q not in out:
                    out[q] = ProbeResult(q, False, 0, error_code="skipped_after_plain_failure")
            break
    return out


def run_sweep(
    entries: list[dict[str, Any]],
    *,
    region: str,
    mantle_regions: list[str],
    workers: int = 6,
    probes: tuple[str, ...] = PROBES,
    on_done: Callable[[dict[str, Any], dict[str, ProbeResult]], None] | None = None,
    session=None,
) -> dict[str, dict[str, dict[str, Any]]]:
    converse = ConverseProber(region, session) if any(e["lane"] == "converse" for e in entries) else None
    mantle: dict[str, MantleProber] = {}
    for mr in mantle_regions:
        if any(e["lane"] != "converse" and e["region"] == mr for e in entries):
            mantle[mr] = MantleProber(mr, MantleProber.mint_token(mr))
    results: dict[str, dict[str, dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(probe_entry, e, converse, mantle, probes): e for e in entries}
        for fut in as_completed(futs):
            e = futs[fut]
            try:
                res = fut.result()
            except Exception as ex:  # noqa: BLE001
                res = {p: ProbeResult(p, False, 0, error_code=ex.__class__.__name__, error_message=str(ex)[:300]) for p in probes}
            results[f"{e['lane']}::{e['id']}"] = {k: v.to_dict() for k, v in res.items()}
            if on_done:
                on_done(e, res)
    return results

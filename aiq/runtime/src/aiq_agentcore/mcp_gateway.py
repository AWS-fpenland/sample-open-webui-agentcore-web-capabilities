# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Minimal SigV4-signed MCP (Streamable HTTP) client for an AgentCore Gateway.

The run's gateway uses AWS_IAM inbound auth, so the runtime execution role
signs each JSON-RPC POST with SigV4 (service ``bedrock-agentcore``). Only the
three calls this adapter needs are implemented: initialize, tools/list,
tools/call. Responses may be JSON or a one-event SSE stream; both are handled.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

log = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-03-26"


class GatewayError(Exception):
    pass


class GatewayMcpClient:
    def __init__(self, url: str | None = None, region: str | None = None, timeout: float = 30.0):
        self.url = (url or os.environ["AIQ_GATEWAY_URL"]).rstrip("/")
        self.region = region or os.environ.get("AIQ_REGION") or os.environ.get("AWS_REGION", "us-east-1")
        self.timeout = timeout
        self._session = boto3.Session()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _signed_headers(self, body: bytes) -> dict[str, str]:
        creds = self._session.get_credentials()
        if creds is None:
            raise GatewayError("no AWS credentials available to sign the gateway request")
        req = AWSRequest(method="POST", url=self.url, data=body,
                         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        SigV4Auth(creds.get_frozen_credentials(), "bedrock-agentcore", self.region).add_auth(req)
        return dict(req.headers)

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            last: dict[str, Any] | None = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        last = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            if last is None:
                raise GatewayError("empty SSE response from gateway")
            return last
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise GatewayError(f"non-JSON gateway response (status {resp.status_code})") from e

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params or {}}).encode()
        headers = self._signed_headers(body)
        resp = await self._client.post(self.url, content=body, headers=headers)
        if resp.status_code >= 400:
            raise GatewayError(f"gateway {method} failed: HTTP {resp.status_code} {resp.text[:300]}")
        msg = self._parse(resp)
        if "error" in msg:
            raise GatewayError(f"gateway {method} error: {json.dumps(msg['error'])[:300]}")
        return msg.get("result")

    async def initialize(self) -> dict[str, Any]:
        return await self.rpc("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION,
                                             "capabilities": {}, "clientInfo": {"name": "aiq-agentcore", "version": "0.1"}})

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):  # bounded pagination
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self.rpc("tools/list", params) or {}
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("isError"):
            raise GatewayError(f"tool {name} returned an error: {json.dumps(result.get('content'))[:300]}")
        return result


def extract_json_content(result: dict[str, Any]) -> Any:
    """MCP tool results carry content blocks; the web-search connector returns one text block of JSON."""
    for block in result.get("content", []) or []:
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    if "structuredContent" in result:
        return result["structuredContent"]
    return None

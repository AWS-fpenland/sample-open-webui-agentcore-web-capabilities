"""Pure documented-schema normalization; not a live-schema or DNS verification.

URL screening excludes non-public literals and local hostnames without resolving
DNS. Normalized links are citations, not authorization to fetch their contents.
Full tool metadata stays in memory; flagged attribution needs caller review.
This does not resolve terms compliance for a native three-field projection.
Schema: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.md
"""

import copy
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class SearchContractError(ValueError):
    """Invalid input or malformed response, never an invented empty result."""


class SearchRPCError(SearchContractError):
    """A JSON-RPC error, distinct from an MCP tool execution error."""


class SearchToolError(SearchContractError):
    """An MCP result explicitly marked isError."""


@dataclass(frozen=True)
class SearchRecord:
    link: str
    title: str | None
    snippet: str
    metadata: dict
    provenance: dict


@dataclass(frozen=True)
class SearchResponse:
    records: tuple[SearchRecord, ...]
    omitted_count: int
    metadata: dict


def validate_search_input(query: str, max_results: int = 10) -> dict:
    """Validate published limits without trimming or silently truncating input."""
    if not isinstance(query, str) or len(query) > 200:
        raise SearchContractError("query must be a string of at most 200 characters")
    if type(max_results) is not int or not 1 <= max_results <= 25:
        raise SearchContractError("maxResults must be an integer from 1 through 25")
    return {"query": query, "maxResults": max_results}


def _public_url(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii")
        if parsed.scheme not in ("http", "https") or not host or parsed.username is not None:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global and not address.is_multicast
        except ValueError:
            labels = host.split(".")
            return (
                len(host) <= 253 and len(labels) > 1 and not labels[-1].isdigit()
                and labels[-1] not in {"localhost", "local", "internal", "lan", "home", "onion", "invalid", "test", "example"}
                and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            )
    except (ValueError, UnicodeError):
        return False


def _payload(tool: dict) -> tuple[dict, str]:
    structured = tool.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        return structured, "structuredContent"
    content = tool.get("content")
    if not isinstance(content, list):
        raise SearchContractError("no supported structuredContent or text content")
    candidates = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            raise SearchContractError("content blocks must be objects")
        if block.get("type") != "text":
            continue
        if not isinstance(block.get("text"), str):
            raise SearchContractError("text content must contain a string")
        try:
            parsed = json.loads(block["text"])
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            if candidates and parsed != candidates[0][0]:
                raise SearchContractError("ambiguous search payloads in text blocks")
            if not candidates:
                candidates.append((parsed, f"content[{index}].text"))
    if not candidates:
        raise SearchContractError("no valid search payload in text content")
    return candidates[0]


def _requires_attribution_review(tool: dict, payload: dict) -> bool:
    review = "structuredContent" in tool and tool["structuredContent"] != payload
    review = review or bool(set(tool) - {"isError", "content", "structuredContent"})
    review = review or bool(set(payload) - {"id", "results"})
    for result in payload["results"]:
        if isinstance(result, dict) and set(result) - {"text", "url", "title", "publishedDate"}:
            review = True
    content = tool.get("content", [])
    if not isinstance(content, list):
        raise SearchContractError("content must be a list")
    for block in content:
        if not isinstance(block, dict):
            raise SearchContractError("content blocks must be objects")
        if set(block) - {"type", "text"}:
            review = True
        if block.get("type") != "text":
            review = True
            continue
        if not isinstance(block.get("text"), str):
            raise SearchContractError("text content must contain a string")
        try:
            if json.loads(block["text"]) != payload:
                review = True
        except (ValueError, RecursionError):
            review = True
    return review


def parse_search_response(envelope: dict) -> SearchResponse:
    """Normalize one JSON-RPC response; preserve metadata and count URL omissions."""
    if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0" or "id" not in envelope:
        raise SearchContractError("expected a JSON-RPC 2.0 response envelope")
    if type(envelope["id"]) not in (str, int, type(None)):
        raise SearchContractError("invalid JSON-RPC response id")
    if ("error" in envelope) == ("result" in envelope):
        raise SearchContractError("expected exactly one of result or error")
    if "error" in envelope:
        error = envelope["error"]
        if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
            raise SearchContractError("malformed JSON-RPC error")
        raise SearchRPCError(copy.deepcopy(error))
    tool = envelope["result"]
    if not isinstance(tool, dict) or type(tool.get("isError", False)) is not bool:
        raise SearchContractError("malformed MCP tool result")
    if tool.get("isError"):
        raise SearchToolError(copy.deepcopy(tool))
    payload, source = _payload(tool)
    review = _requires_attribution_review(tool, payload) or bool(set(envelope) - {"jsonrpc", "id", "result"})
    records, omitted = [], 0
    for index, result in enumerate(payload["results"]):
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise SearchContractError("each search result must contain text")
        if any(field in result and not isinstance(result[field], str) for field in ("url", "title", "publishedDate")):
            raise SearchContractError("optional result fields must be strings when present")
        if not _public_url(result.get("url", "")):
            omitted += 1
            continue
        records.append(SearchRecord(result["url"], result.get("title"), result["text"], copy.deepcopy(result),
                                    {"source": source, "result_index": index, "rpc_id": copy.deepcopy(envelope["id"])}))
    metadata = {"search": {key: value for key, value in payload.items() if key != "results"},
                "tool": tool, "requires_attribution_review": review,
                "envelope": {key: value for key, value in envelope.items() if key != "result"}}
    return SearchResponse(tuple(records), omitted, copy.deepcopy(metadata))

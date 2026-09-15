"""Offline documented-schema fixtures; not evidence of live connector behavior."""

import copy
import json

import pytest

from web_capabilities.search import (
    SearchContractError, SearchRPCError, SearchToolError,
    parse_search_response, validate_search_input,
)


def response(results=None, **tool_fields):
    return {"jsonrpc": "2.0", "id": "call-1", "result": {
        "structuredContent": {"results": results if results is not None else [{"text": "snippet", "url": "https://example.com/page"}]},
        **tool_fields,
    }}


def text_block(payload):
    return {"type": "text", "text": json.dumps(payload)}


def test_input_defaults_and_boundaries():
    assert validate_search_input("query") == {"query": "query", "maxResults": 10}
    assert validate_search_input("é" * 200, 25)["maxResults"] == 25
    assert validate_search_input(" query ", 1)["query"] == " query "


@pytest.mark.parametrize("query,maximum", [(None, 10), ("x" * 201, 10), ("x", True), ("x", 1.0), ("x", 0), ("x", 26)])
def test_invalid_input(query, maximum):
    with pytest.raises(SearchContractError):
        validate_search_input(query, maximum)


def test_optional_fields_metadata_provenance_and_purity():
    original = response([{"text": "snippet", "url": "https://example.com", "extra": {"rank": 2}}])
    original["result"]["structuredContent"]["id"] = "search-1"
    original["result"]["extra"] = {"version": "1.2.0"}
    original["trace"] = "trace-1"
    before = copy.deepcopy(original)
    parsed = parse_search_response(original)
    record = parsed.records[0]
    assert (record.link, record.title, record.snippet) == ("https://example.com", None, "snippet")
    assert record.provenance == {"source": "structuredContent", "result_index": 0, "rpc_id": "call-1"}
    assert parsed.metadata["search"] == {"id": "search-1"}
    assert parsed.metadata["tool"]["extra"] == {"version": "1.2.0"}
    assert parsed.metadata["envelope"]["trace"] == "trace-1"
    record.metadata["extra"]["rank"] = 9
    assert original == before


def test_structured_precedence_without_duplicate_results():
    payload = response()
    payload["result"]["content"] = [text_block(payload["result"]["structuredContent"])]
    parsed = parse_search_response(payload)
    assert len(parsed.records) == 1
    assert parsed.metadata["requires_attribution_review"] is False


def test_text_fallback_multiple_blocks_and_duplicate_payload():
    payload = {"results": [{"text": "snippet", "url": "https://example.com", "title": "Title", "publishedDate": "2026-09-15"}]}
    parsed = parse_search_response(response(structuredContent={"unexpected": True}, content=[
        {"type": "text", "text": "preamble"}, text_block(payload), text_block(payload), {"type": "image"},
    ]))
    assert len(parsed.records) == 1
    assert parsed.records[0].title == "Title"
    assert parsed.records[0].provenance["source"] == "content[1].text"
    assert parsed.metadata["requires_attribution_review"] is True
    assert parsed.metadata["tool"]["content"][0]["text"] == "preamble"
    assert parsed.metadata["tool"]["content"][-1] == {"type": "image"}


@pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "http://localhost", "http://127.0.0.1", "https://10.0.0.1", "http://[::1]", "http://224.0.0.1", "http://service.internal", "https://user:password@example.com", "https://example.com:bad", "https://example.com/ bad"])
def test_omits_ungroundable_results_without_inventing_links(url):
    record = {"text": "no grounded citation"}
    if url is not None:
        record["url"] = url
    parsed = parse_search_response(response([record]))
    assert parsed.records == () and parsed.omitted_count == 1


def test_explicit_empty_results():
    parsed = parse_search_response(response([]))
    assert parsed.records == () and parsed.omitted_count == 0


def test_mixed_results_count_omissions_and_preserve_positions():
    parsed = parse_search_response(response([{"text": "missing"}, {"text": "ok", "url": "http://example.com"}]))
    assert len(parsed.records) == parsed.omitted_count == 1
    assert parsed.records[0].provenance["result_index"] == 1


@pytest.mark.parametrize("tool", [{}, {"structuredContent": {}}, {"content": []}, {"content": [{"type": "text", "text": "not JSON"}]}, {"structuredContent": {"results": [{"url": "https://example.com"}]}}, {"structuredContent": {"results": [{"text": "ok", "title": None}]}}, {"isError": "false"}])
def test_malformed_results_are_not_empty_success(tool):
    with pytest.raises(SearchContractError):
        parse_search_response({"jsonrpc": "2.0", "id": 1, "result": tool})


def test_distinct_rpc_and_tool_errors():
    with pytest.raises(SearchRPCError):
        parse_search_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid"}})
    with pytest.raises(SearchToolError):
        parse_search_response(response(isError=True))


@pytest.mark.parametrize("envelope", [{}, {"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": True, "result": {}}, {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}}, {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "bad"}}])
def test_invalid_envelope(envelope):
    with pytest.raises(SearchContractError):
        parse_search_response(envelope)


def test_distinct_text_payloads_are_ambiguous():
    with pytest.raises(SearchContractError, match="ambiguous"):
        parse_search_response(response(structuredContent=None, content=[text_block({"results": []}), text_block({"results": [{"text": "different"}]})]))


def test_annotations_and_full_tool_metadata_are_preserved_and_copied():
    original = response()
    block = text_block(original["result"]["structuredContent"])
    block["annotations"] = {"citations": [{"url": "https://example.com/source", "label": "Attribution"}]}
    original["result"]["content"] = [block]
    original["result"]["_meta"] = {"provider": "fixture"}
    before = copy.deepcopy(original)
    parsed = parse_search_response(original)
    assert parsed.metadata["requires_attribution_review"] is True
    assert parsed.metadata["tool"] == original["result"]
    parsed.metadata["tool"]["content"][0]["annotations"]["citations"][0]["label"] = "changed"
    parsed.metadata["tool"]["structuredContent"]["results"][0]["text"] = "changed"
    assert original == before


@pytest.mark.parametrize("extra", [
    {"type": "text", "text": "Source attribution must be shown"},
    {"type": "image", "data": "fixture", "mimeType": "image/png"},
    {"type": "resource_link", "uri": "https://example.com/source", "name": "Credit"},
])
def test_additional_attribution_blocks_are_retained_and_flagged(extra):
    original = response(content=[extra])
    parsed = parse_search_response(original)
    assert parsed.metadata["requires_attribution_review"] is True
    assert parsed.metadata["tool"]["content"] == [extra]


def test_structured_text_disagreement_is_flagged_not_silently_preferred():
    original = response(content=[text_block({"results": [{"text": "different", "url": "https://other.example.com"}]})])
    parsed = parse_search_response(original)
    assert parsed.metadata["requires_attribution_review"] is True
    assert parsed.metadata["tool"] == original["result"]


def test_unsupported_structured_payload_with_text_fallback_requires_review():
    original = response(structuredContent={"attribution": "must retain"}, content=[text_block({"results": []})])
    parsed = parse_search_response(original)
    assert parsed.records == () and parsed.metadata["requires_attribution_review"] is True
    assert parsed.metadata["tool"]["structuredContent"] == {"attribution": "must retain"}


def test_equivalent_text_duplicates_without_annotations_need_no_review():
    payload = {"results": [{"text": "snippet", "url": "https://example.com"}]}
    envelope = {"jsonrpc": "2.0", "id": "call-1", "result": {"content": [text_block(payload), text_block(payload)]}}
    parsed = parse_search_response(envelope)
    assert len(parsed.records) == 1 and parsed.metadata["requires_attribution_review"] is False
    assert parsed.metadata["tool"] == envelope["result"]

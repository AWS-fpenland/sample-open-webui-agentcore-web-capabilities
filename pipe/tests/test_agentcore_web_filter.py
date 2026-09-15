"""Offline v0.11.3 hook contracts; no OWUI imports, deployment, or network calls."""

import ast
import asyncio
import copy
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


MODEL_IDS = ["agentcore-web-haiku-canary", "agentcore-web-responses-canary"]
SUBJECT = "synthetic-owui-admin-id"
TOOLKIT_ID = "agentcore_web_canary"


@pytest.fixture
def context():
    path = Path(__file__).resolve().parents[1] / "agentcore_web_filter.py"
    spec = importlib.util.spec_from_file_location("canary_filter_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    policy = module.Filter()
    policy.valves.allowed_subjects = [SUBJECT]
    policy.valves.allowed_model_ids = MODEL_IDS.copy()
    model = {"id": MODEL_IDS[0], "info": {"base_model_id": "gateway.anthropic.synthetic"}}
    metadata = {"model": model, "params": {"function_calling": "legacy"}, "files": []}
    body = {
        "model": model["id"], "stream": True,
        "messages": [{"role": "user", "content": "Find public documentation"}],
        "metadata": metadata,
    }
    request = SimpleNamespace(state=SimpleNamespace(metadata=metadata))
    user = {"id": SUBJECT, "role": "admin"}
    return SimpleNamespace(policy=policy, model=model, metadata=metadata, body=body, request=request, user=user)


def hook(context, name):
    return asyncio.run(getattr(context.policy, name)(
        body=context.body, __user__=context.user, __metadata__=context.metadata,
        __request__=context.request, __model__=context.model,
    ))


async def search_web(query: str, count: int = 3, __request__=None):
    return "[]"


async def fetch_url(url: str, render: bool = False, __request__=None):
    return "Page text"


def resolve_tools(context):
    registry = {}
    definitions = [
        ("search_web", search_web, {"query": {"type": "string"}, "count": {"type": "integer", "default": 3}}, ["query"]),
        ("fetch_url", fetch_url, {"url": {"type": "string"}, "render": {"type": "boolean", "default": False}}, ["url"]),
    ]
    for name, function, properties, required in definitions:
        registry[name] = {
            "tool_id": TOOLKIT_ID, "callable": function,
            "spec": {
                "name": name, "description": "Synthetic explicit tool",
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
            "metadata": {"citation": False, "file_handler": False},
        }
    context.metadata["tools"] = registry
    context.body["tools"] = [
        {"type": "function", "function": copy.deepcopy(tool["spec"])} for tool in registry.values()
    ]
    context.metadata["tool_ids"] = context.body.pop("tool_ids", [TOOLKIT_ID])


def ready(context):
    hook(context, "inlet")
    resolve_tools(context)


def denied(context, name="inlet"):
    with pytest.raises(ValueError):
        hook(context, name)
    assert context.request.state.agentcore_web_canary_authorized is False


@pytest.mark.parametrize("field", ["allowed_subjects", "allowed_model_ids"])
def test_empty_allowlists_deny_even_admin(context, field):
    defaults = context.policy.Valves()
    assert defaults.allowed_subjects == defaults.allowed_model_ids == []
    setattr(context.policy.valves, field, [])
    denied(context)
    assert not hasattr(context.request.state, "agentcore_web_canary_calls")


def test_filter_has_no_user_toggle_or_user_valves(context):
    assert not getattr(context.policy, "toggle", False)
    assert not hasattr(context.policy, "UserValves")
    assert not getattr(context.policy, "file_handler", False)


@pytest.mark.parametrize("user", [None, {}, {"id": "other-admin", "role": "admin"},
                                      {"id": SUBJECT, "role": "pending"},
                                      {"id": SUBJECT.upper(), "role": "admin"}])
def test_subject_is_exact_not_role_or_body_headers(context, user):
    context.user = user
    context.body["user"] = {"id": SUBJECT, "role": "admin"}
    context.request.headers = {"X-OpenWebUI-User-Id": SUBJECT}
    denied(context)


@pytest.mark.parametrize("model_id", MODEL_IDS)
@pytest.mark.parametrize("role", ["admin", "user"])
def test_inlet_sets_trusted_native_mode_and_wrapper_state(context, model_id, role):
    context.model["id"] = context.body["model"] = model_id
    context.user["role"] = role
    result = hook(context, "inlet")
    assert result is context.body
    assert result["metadata"] is context.metadata
    assert context.metadata["params"]["function_calling"] == "native"
    assert result["tool_ids"] == context.metadata["tool_ids"] == [TOOLKIT_ID]
    assert context.request.state.max_tool_call_iterations == 3
    assert context.request.state.agentcore_web_canary_calls == 0
    assert context.request.state.agentcore_web_canary_authorized is True
    assert "max_tool_call_iterations" not in context.metadata["params"]


@pytest.mark.parametrize("key,value", [
    ("stream", False), ("stream", None), ("stream", 1),
    ("features", {"web_search": True}), ("features", {"web_search": "false"}),
    ("features", ["web_search"]), ("tools", []), ("tools", None),
    ("tool_servers", [{"url": "https://example.com"}]),
    ("direct_tool_servers", ["server"]), ("direct", True),
    ("tool_ids", ["other"]), ("tool_ids", [TOOLKIT_ID, "other"]),
    ("tool_ids", [TOOLKIT_ID, TOOLKIT_ID]), ("tool_ids", TOOLKIT_ID),
    ("model", "unapproved-model"),
])
def test_inlet_rejects_unsupported_request_modes(context, key, value):
    context.body[key] = value
    denied(context)


@pytest.mark.parametrize("tool_ids", [None, [], [TOOLKIT_ID]])
def test_inlet_accepts_only_empty_or_exact_tool_selection(context, tool_ids):
    context.body["tool_ids"] = tool_ids
    hook(context, "inlet")
    assert context.body["tool_ids"] == [TOOLKIT_ID]


@pytest.mark.parametrize("key,value", [
    ("features", {"web_search": True}), ("tool_servers", ["server"]),
    ("tool_ids", ["other"]), ("direct", True),
])
def test_trusted_metadata_cannot_hide_rejected_features(context, key, value):
    context.metadata[key] = value
    denied(context)


@pytest.mark.parametrize("location", ["body", "metadata", "message"])
@pytest.mark.parametrize("field", ["files", "attachments"])
def test_attachments_rejected_in_each_input_location(context, location, field):
    container = context.body["messages"][0] if location == "message" else getattr(context, location)
    container[field] = [{"type": "url", "url": "https://example.com"}]
    denied(context)


@pytest.mark.parametrize("content_type", ["image_url", "input_audio", "file", "video_url"])
def test_nontext_content_rejected_after_possible_upstream_fetch(context, content_type):
    context.body["messages"][0]["content"] = [{"type": content_type, "text": "not plain text"}]
    denied(context)


def test_text_parts_and_pasted_urls_are_not_attachments(context):
    context.body["messages"][0]["content"] = [{"type": "text", "text": "Read https://example.com"}]
    hook(context, "inlet")


@pytest.mark.parametrize("field", ["folder_id", "folder_knowledge", "knowledge", "terminal_id"])
def test_attached_context_rejected(context, field):
    context.metadata[field] = "attached-resource"
    denied(context)


def test_body_metadata_does_not_replace_trusted_metadata(context):
    context.body["metadata"] = {"params": {"function_calling": "legacy"}}
    hook(context, "inlet")
    assert context.body["metadata"] is context.metadata
    assert context.metadata["params"]["function_calling"] == "native"


def test_mismatched_trusted_model_rejected(context):
    context.metadata["model"] = {"id": "other"}
    denied(context)


def test_missing_trusted_context_rejected(context):
    context.metadata = None
    denied(context)
    context.request = None
    with pytest.raises(ValueError, match="request state"):
        hook(context, "inlet")


def test_request_requires_successful_inlet(context):
    resolve_tools(context)
    denied(context, "request")


def test_request_accepts_local_reserved_injection_and_preserves_counter(context):
    ready(context)
    assert "__request__" in inspect.signature(search_web).parameters
    context.request.state.agentcore_web_canary_calls = 4
    context.request.state.max_tool_call_iterations = 256
    assert hook(context, "request") is context.body
    assert context.request.state.agentcore_web_canary_authorized is True
    assert context.request.state.agentcore_web_canary_calls == 4
    assert context.request.state.max_tool_call_iterations == 3
    hook(context, "request")
    assert context.request.state.agentcore_web_canary_calls == 4


def test_reentered_inlet_does_not_replenish_budget(context):
    hook(context, "inlet")
    context.request.state.agentcore_web_canary_calls = 2
    hook(context, "inlet")
    assert context.request.state.agentcore_web_canary_calls == 2


def test_followup_accepts_trusted_base_model_and_tool_messages(context):
    ready(context)
    context.body["model"] = context.model["info"]["base_model_id"]
    context.body["messages"].extend([
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-id"}]},
        {"role": "tool", "tool_call_id": "call-id", "content": "[]"},
    ])
    hook(context, "request")


@pytest.mark.parametrize("value", [-1, 5, True, "1", None])
def test_invalid_counter_revokes_authorization(context, value):
    ready(context)
    context.request.state.agentcore_web_canary_calls = value
    denied(context, "request")


@pytest.mark.parametrize("change", ["subject", "model", "native", "attachment", "web_search"])
def test_followup_rechecks_scope_and_inputs(context, change):
    ready(context)
    if change == "subject":
        context.user["id"] = "other-admin"
    elif change == "model":
        context.body["model"] = "other-model"
    elif change == "native":
        context.metadata["params"]["function_calling"] = "legacy"
    elif change == "attachment":
        context.metadata["files"] = [{"id": "file-id"}]
    else:
        context.metadata["features"] = {"web_search": True}
    denied(context, "request")


@pytest.mark.parametrize("change", ["missing", "extra", "wrong_owner", "native_fallback", "direct", "mcp", "not_callable"])
def test_registry_must_resolve_only_approved_local_callables(context, change):
    ready(context)
    registry = context.metadata["tools"]
    if change == "missing":
        registry.pop("fetch_url")
    elif change == "extra":
        registry["execute_code"] = registry["fetch_url"]
    elif change == "wrong_owner":
        registry["fetch_url"]["tool_id"] = "other_toolkit"
    elif change == "native_fallback":
        registry["fetch_url"].pop("tool_id")
    elif change == "direct":
        registry["fetch_url"]["direct"] = True
    elif change == "mcp":
        registry["fetch_url"]["type"] = "mcp"
    else:
        registry["fetch_url"]["callable"] = None
    denied(context, "request")


@pytest.mark.parametrize("change", ["extra_arg", "reserved_arg", "wrong_type", "required", "additional", "name"])
def test_registry_schema_shape_rejected_even_if_outgoing_matches(context, change):
    ready(context)
    spec = context.metadata["tools"]["fetch_url"]["spec"]
    parameters = spec["parameters"]
    if change == "extra_arg":
        parameters["properties"]["subject"] = {"type": "string"}
    elif change == "reserved_arg":
        parameters["properties"]["__request__"] = {"type": "string"}
    elif change == "wrong_type":
        parameters["properties"]["render"]["type"] = "string"
    elif change == "required":
        parameters["required"] = ["url", "render"]
    elif change == "additional":
        parameters["additionalProperties"] = True
    else:
        spec["name"] = "other_fetch"
    context.body["tools"][1]["function"] = copy.deepcopy(spec)
    denied(context, "request")


@pytest.mark.parametrize("change", ["missing", "duplicate", "renamed", "arguments", "type", "extra"])
def test_outgoing_schemas_must_match_registry(context, change):
    ready(context)
    outgoing = context.body["tools"]
    if change == "missing":
        outgoing.pop()
    elif change == "duplicate":
        outgoing[1] = copy.deepcopy(outgoing[0])
    elif change == "renamed":
        outgoing[0]["function"]["name"] = "other"
    elif change == "arguments":
        outgoing[0]["function"]["parameters"]["properties"]["count"]["type"] = "string"
    elif change == "type":
        outgoing[0]["type"] = "custom"
    else:
        outgoing[0]["server"] = "external"
    denied(context, "request")


def test_requests_do_not_share_authorization_or_budget(context):
    ready(context)
    original = context.request
    original.state.agentcore_web_canary_calls = 4
    context.request = SimpleNamespace(state=SimpleNamespace(metadata=context.metadata))
    denied(context, "request")
    assert original.state.agentcore_web_canary_calls == 4
    assert original.state.agentcore_web_canary_authorized is True


def test_pinned_hook_parameter_injection_without_importing_owui(context):
    source = Path("/tmp/open-webui-v0.11.3/backend/open_webui/utils/filter.py")
    if not source.exists():
        pytest.skip("Pinned source checkout is not available")
    syntax = ast.parse(source.read_text())
    function = next(node for node in syntax.body if isinstance(node, ast.FunctionDef) and node.name == "get_filter_params")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    extra = {
        "__user__": context.user, "__metadata__": context.metadata,
        "__request__": context.request, "__model__": context.model,
        "__event_emitter__": object(),
    }
    for name in ("inlet", "request"):
        if name == "request":
            resolve_tools(context)
        handler = getattr(context.policy, name)
        params = namespace["get_filter_params"](
            inspect.signature(handler), "agentcore_web_policy", name, context.body, extra,
        )
        assert params["__metadata__"] is context.metadata
        assert "__event_emitter__" not in params
        asyncio.run(handler(**params))
    assert context.request.state.agentcore_web_canary_authorized is True


def test_current_tool_wrapper_shares_four_call_budget_and_revocation(context, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "agentcore_web_tools.py"
    if not path.exists():
        pytest.skip("Companion toolkit has not been added yet")
    spec = importlib.util.spec_from_file_location("canary_wrapper_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = module.Tools()
    tools.valves.allowed_subjects = [SUBJECT]
    tools.valves.search_enabled = tools.valves.fetch_enabled = True
    invocations = []

    def invoke(payload):
        assert context.request.state.agentcore_web_canary_calls == len(invocations) + 1
        assert payload["subject"] == SUBJECT
        invocations.append(payload)
        return {
            "request_id": payload["request_id"], "records": [],
            "document": {"text": "Page text", "final_url": "https://example.com"},
        }

    monkeypatch.setattr(tools, "_invoke", invoke)
    ready(context)
    context.metadata["tools"]["search_web"]["callable"] = tools.search_web
    context.metadata["tools"]["fetch_url"]["callable"] = tools.fetch_url

    for call_number in range(4):
        hook(context, "request")
        arguments = {
            "__request__": context.request, "__user__": context.user,
            "__metadata__": context.metadata,
        }
        if call_number % 2 == 0:
            result = asyncio.run(tools.search_web("synthetic query", **arguments))
            assert json.loads(result) == []
        else:
            result = asyncio.run(tools.fetch_url("https://example.com", **arguments))
            assert "Page text" in result
    hook(context, "request")
    result = asyncio.run(tools.search_web("one too many", **arguments))
    assert "limit" in json.loads(result)["error"]
    assert context.request.state.agentcore_web_canary_calls == len(invocations) == 4
    context.body["tools"] = []
    denied(context, "request")
    result = asyncio.run(tools.fetch_url("https://example.com", **arguments))
    assert "policy filter" in json.loads(result)["error"]
    assert len(invocations) == 4

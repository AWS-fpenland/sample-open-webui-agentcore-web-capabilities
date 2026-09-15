"""Owned-canary configuration and rollback contracts against an offline API."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


SUBJECT = "synthetic-canary-admin"
ORIGIN = "https://owui.example.invalid"
ARN = "arn:aws:lambda:us-east-1:123456789012:function:web-canary:live"
TOKEN = "synthetic-admin-token-never-output"


class FakeAdminAPI:
    def __init__(self, module):
        self.module = module
        self.calls = []
        self.client_options = []
        self.responses = {
            "/api/version": {"version": "0.11.3"},
            "/api/v1/auths/": {"id": SUBJECT, "role": "admin"},
            "/api/v1/retrieval/config": {"web": {"ENABLE_WEB_SEARCH": False}},
            "/api/models": {"data": [{"id": base} for name, base in module.MODELS.values()]},
        }
        self.failures = {}
        for collection, identifier, filename in (
            ("tools", module.TOOL, "agentcore_web_tools.py"),
            ("functions", module.FILTER, "agentcore_web_filter.py"),
        ):
            route = f"/api/v1/{collection}/id/{identifier}"
            self.responses[route] = {
                "id": identifier, "user_id": SUBJECT, "meta": {"description": module.MARKER},
                "content": (module.ROOT / "pipe" / filename).read_text(), "is_active": True, "is_global": False,
            }
            self.responses[route + "/valves"] = {
                "search_enabled": True, "fetch_enabled": True, "browser_enabled": True,
                "custom_admin_knob": {"keep": 7},
                "allowed_subjects": [SUBJECT, "another-existing-subject"], "region": "eu-west-1",
            }

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def handle(self, request):
        assert str(request.url).startswith(ORIGIN + "/")
        assert request.headers["authorization"] == "Bearer " + TOKEN
        route = request.url.raw_path.decode()
        payload = json.loads(request.content) if request.content else None
        self.calls.append((request.method, route, payload))
        failure = self.failures.get((request.method, route))
        if failure is not None:
            return httpx.Response(failure, json={"detail": "synthetic API failure"})
        if request.method == "GET":
            if route not in self.responses:
                return httpx.Response(404, json={"detail": "not found"})
            result = copy.deepcopy(self.responses[route])
            if route == "/api/models":
                result["data"].extend(
                    {"id": model["id"]} for target, model in self.responses.items()
                    if target.startswith("/api/v1/models/model?id=") and model.get("is_active")
                )
            return httpx.Response(200, json=result)
        assert request.method == "POST"
        if route.endswith("/valves/update"):
            self.responses[route.removesuffix("/update")] = copy.deepcopy(payload)
        elif route.endswith("/create"):
            collection = route.split("/")[3]
            if collection == "models":
                target = f"/api/v1/models/model?id={payload['id']}"
            else:
                target = f"/api/v1/{collection}/id/{payload['id']}"
            self.responses[target] = {**copy.deepcopy(payload), "user_id": SUBJECT, "is_active": False}
            if collection == "models":
                self.responses[target]["is_active"] = payload["is_active"]
        elif route.endswith("/update"):
            target = route.removesuffix("/update")
            self.responses[target] = {**self.responses[target], **copy.deepcopy(payload)}
        elif route.endswith("/toggle"):
            self.responses[route.removesuffix("/toggle")]["is_active"] = True
        else:
            raise AssertionError("Unexpected write route: " + route)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def script(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/configure-web-canary.py"
    spec = importlib.util.spec_from_file_location("configure_web_canary_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = FakeAdminAPI(module)
    real_client = httpx.Client

    def client_factory(**options):
        api.client_options.append(options)
        return real_client(**options, transport=httpx.MockTransport(api.handle))

    monkeypatch.setattr(module.httpx, "Client", client_factory)
    monkeypatch.setenv("OWUI_ADMIN_TOKEN", TOKEN)
    return SimpleNamespace(module=module, api=api, monkeypatch=monkeypatch)


def run(script, *actions, **changes):
    options = {"base-url": ORIGIN, "subject": SUBJECT, "function-arn": ARN, "region": "us-east-1", **changes}
    arguments = ["configure-web-canary.py"]
    for name, value in options.items():
        arguments.extend(["--" + name, value])
    script.monkeypatch.setattr(sys, "argv", arguments + list(actions))
    return script.module.main()


def tool_route(script):
    return f"/api/v1/tools/id/{script.module.TOOL}"


def filter_route(script):
    return f"/api/v1/functions/id/{script.module.FILTER}"


def test_default_is_read_only_plan_and_never_outputs_token(script, capsys):
    before = copy.deepcopy(script.api.responses)
    run(script)
    output = capsys.readouterr().out
    plan = json.loads(output)
    assert plan["action"] == "plan"
    assert plan["existing_connections_unchanged"] is True
    assert plan["global_search_unchanged"] is True
    assert script.api.writes == []
    assert script.api.responses == before
    assert TOKEN not in output
    assert script.api.client_options == [{
        "base_url": ORIGIN, "headers": {"Authorization": "Bearer " + TOKEN},
        "timeout": 30, "follow_redirects": False, "trust_env": False,
    }]


def test_update_code_without_apply_remains_read_only(script, capsys):
    script.api.responses[tool_route(script)]["content"] = "existing reviewed source"
    run(script, "--update-code")
    assert json.loads(capsys.readouterr().out)["action"] == "plan"
    assert script.api.writes == []
    assert script.api.responses[tool_route(script)]["content"] == "existing reviewed source"


@pytest.mark.parametrize("route,value", [
    ("/api/version", {"version": "0.11.2"}),
    ("/api/v1/auths/", {"id": "other-admin", "role": "admin"}),
    ("/api/v1/auths/", {"id": SUBJECT, "role": "user"}),
    ("/api/v1/retrieval/config", {"web": {"ENABLE_WEB_SEARCH": True}}),
    ("/api/v1/retrieval/config", {"web": {}}),
    ("/api/models", {"data": []}),
])
def test_apply_preflight_rejects_wrong_version_identity_global_search_or_missing_models(script, route, value):
    script.api.responses[route] = value
    with pytest.raises(script.module.ConfigurationError):
        run(script, "--apply")
    assert script.api.writes == []


@pytest.mark.parametrize("object_type", ["tool", "filter", "model"])
@pytest.mark.parametrize("change", [{"user_id": "another-owner"}, {"meta": {"description": "admin-owned object"}}])
def test_existing_unowned_or_unmarked_objects_are_never_written(script, object_type, change):
    routes = {"tool": tool_route(script), "filter": filter_route(script),
              "model": "/api/v1/models/model?id=" + next(iter(script.module.MODELS))}
    route = routes[object_type]
    script.api.responses[route] = {
        "user_id": SUBJECT, "meta": {"description": script.module.MARKER}, **change,
    }
    before = copy.deepcopy(script.api.responses)
    with pytest.raises(script.module.ConfigurationError, match="unowned or unmarked"):
        run(script, "--apply")
    assert script.api.writes == []
    assert script.api.responses == before


def test_disable_works_without_catalog_retrieval_or_filter_and_preserves_other_valves(script, capsys):
    valves_route = tool_route(script) + "/valves"
    original = copy.deepcopy(script.api.responses[valves_route])
    for route in ("/api/models", "/api/v1/retrieval/config", filter_route(script)):
        script.api.failures[("GET", route)] = 503
    run(script, "--disable")
    assert json.loads(capsys.readouterr().out) == {
        "action": "disabled", "data_deleted": False, "existing_connections_unchanged": True,
    }
    assert script.api.responses[valves_route] == {
        **original, "search_enabled": False, "fetch_enabled": False, "browser_enabled": False,
    }
    assert script.api.writes == [("POST", valves_route + "/update", script.api.responses[valves_route])]
    assert [route for method, route, payload in script.api.calls if method == "GET"] == [
        "/api/version", "/api/v1/auths/", tool_route(script), valves_route,
    ]


def test_disable_missing_tool_is_safe_noop(script):
    del script.api.responses[tool_route(script)]
    run(script, "--disable")
    assert script.api.writes == []


@pytest.mark.parametrize("change", [{"user_id": "other-admin"}, {"meta": {"description": "not ours"}}])
def test_disable_does_not_touch_unowned_tool(script, change):
    script.api.responses[tool_route(script)].update(change)
    with pytest.raises(script.module.ConfigurationError):
        run(script, "--disable")
    assert script.api.writes == []


def test_apply_changes_only_owned_canary_routes_and_creates_private_native_models(script, capsys):
    run(script, "--apply")
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "configured"
    assert result["live_tool_invocation_verified"] is False
    permitted = {filter_route(script) + "/valves/update", tool_route(script) + "/valves/update",
                 "/api/v1/models/create"}
    assert all(method == "POST" and route in permitted for method, route, payload in script.api.writes)
    models = [payload for method, route, payload in script.api.writes if route == "/api/v1/models/create"]
    assert {model["id"] for model in models} == set(script.module.MODELS)
    for model in models:
        assert model["access_grants"] == []
        assert model["params"]["function_calling"] == "native"
        assert model["meta"]["description"] == script.module.MARKER
        assert model["meta"]["toolIds"] == [script.module.TOOL]
        assert model["meta"]["filterIds"] == [script.module.FILTER]
        assert model["meta"]["capabilities"]["web_search"] is False
    assert script.api.responses[tool_route(script) + "/valves"] == {
        "region": "us-east-1", "function_arn": ARN, "allowed_subjects": [SUBJECT],
        "search_enabled": True, "fetch_enabled": True, "browser_enabled": True,
    }


def test_apply_refreshes_serving_catalog_after_both_model_creations(script, capsys):
    run(script, "--apply")
    catalog_reads = [index for index, (method, route, payload) in enumerate(script.api.calls)
                     if method == "GET" and route == "/api/models"]
    model_writes = [index for index, (method, route, payload) in enumerate(script.api.calls)
                    if method == "POST" and route == "/api/v1/models/create"]
    assert len(catalog_reads) == len(model_writes) == 2
    assert catalog_reads[0] < min(model_writes) <= max(model_writes) < catalog_reads[1]
    assert script.api.calls[-1] == ("GET", "/api/models", None)
    assert json.loads(capsys.readouterr().out)["action"] == "configured"


@pytest.mark.parametrize("status,model_ids", [
    (200, []),
    (200, ["agentcore-web-haiku-canary"]),
    (200, ["agentcore-web-responses-canary"]),
    (503, []),
])
def test_failed_catalog_refresh_disables_tool_and_preserves_created_models(script, capsys, status, model_ids):
    original_handle = script.api.handle
    catalog_reads = 0

    def handle(request):
        nonlocal catalog_reads
        response = original_handle(request)
        if request.method == "GET" and request.url.path == "/api/models":
            catalog_reads += 1
            if catalog_reads == 2:
                return httpx.Response(status, json={"data": [{"id": model_id} for model_id in model_ids]})
        return response

    script.monkeypatch.setattr(script.api, "handle", handle)
    message = "serving catalog" if status == 200 else "HTTP 503"
    with pytest.raises(script.module.ConfigurationError, match=message):
        run(script, "--apply")
    assert catalog_reads == 2
    assert capsys.readouterr().out == ""
    assert all("/api/v1/models/model?id=" + model_id in script.api.responses for model_id in script.module.MODELS)
    valves = script.api.responses[tool_route(script) + "/valves"]
    assert all(valves[name] is False for name in ("search_enabled", "fetch_enabled", "browser_enabled"))
    assert script.api.writes[-1][1] == tool_route(script) + "/valves/update"
    assert all(method != "DELETE" for method, route, payload in script.api.calls)


def test_compatible_existing_models_keep_extra_admin_metadata_without_model_updates(script):
    run(script, "--apply")
    for model_id in script.module.MODELS:
        model = script.api.responses["/api/v1/models/model?id=" + model_id]
        model["meta"]["admin_note"] = "preserve this"
        model["params"]["temperature"] = 0.25
    before = copy.deepcopy(script.api.responses)
    script.api.calls.clear()
    run(script, "--apply")
    assert all("/models/" not in route for method, route, payload in script.api.writes)
    for model_id in script.module.MODELS:
        route = "/api/v1/models/model?id=" + model_id
        assert script.api.responses[route] == before[route]


def test_incompatible_existing_model_is_preserved_and_failure_disables_tool(script):
    run(script, "--apply")
    route = "/api/v1/models/model?id=" + next(iter(script.module.MODELS))
    script.api.responses[route]["params"]["max_tokens"] = 2048
    original = copy.deepcopy(script.api.responses[route])
    script.api.calls.clear()
    with pytest.raises(script.module.ConfigurationError, match="preserve and review"):
        run(script, "--apply")
    assert script.api.responses[route] == original
    assert all("/models/" not in target for method, target, payload in script.api.writes)
    assert script.api.responses[tool_route(script) + "/valves"]["search_enabled"] is False
    assert script.api.responses[tool_route(script) + "/valves"]["fetch_enabled"] is False
    assert script.api.responses[tool_route(script) + "/valves"]["browser_enabled"] is False


@pytest.mark.parametrize("object_type", ["tool", "filter"])
def test_source_change_requires_explicit_update_and_failure_rolls_back(script, object_type):
    route = tool_route(script) if object_type == "tool" else filter_route(script)
    script.api.responses[route]["content"] = "operator-reviewed existing source"
    with pytest.raises(script.module.ConfigurationError, match="--update-code"):
        run(script, "--apply")
    assert script.api.responses[route]["content"] == "operator-reviewed existing source"
    assert not any(target == route + "/update" for method, target, payload in script.api.writes)
    assert script.api.responses[tool_route(script) + "/valves"]["search_enabled"] is False


def test_update_code_is_explicit_and_source_forms_do_not_embed_valves(script):
    for route in (tool_route(script), filter_route(script)):
        script.api.responses[route]["content"] = "old source"
    run(script, "--apply", "--update-code")
    updates = [(route, payload) for method, route, payload in script.api.writes
               if route in {tool_route(script) + "/update", filter_route(script) + "/update"}]
    assert len(updates) == 2
    for route, payload in updates:
        assert "valves" not in payload
        assert payload["meta"] == {"description": script.module.MARKER}
        assert payload["content"] != "old source"


def test_failed_model_creation_rolls_back_without_deleting_or_touching_other_objects(script):
    script.api.failures[("POST", "/api/v1/models/create")] = 500
    original = copy.deepcopy(script.api.responses)
    with pytest.raises(script.module.ConfigurationError, match="HTTP 500"):
        run(script, "--apply")
    assert script.api.writes[-1][1] == tool_route(script) + "/valves/update"
    assert script.api.writes[-1][2]["search_enabled"] is False
    assert script.api.writes[-1][2]["fetch_enabled"] is False
    assert script.api.writes[-1][2]["browser_enabled"] is False
    assert script.api.responses["/api/v1/retrieval/config"] == original["/api/v1/retrieval/config"]
    assert script.api.responses["/api/models"] == original["/api/models"]
    assert all(method != "DELETE" for method, route, payload in script.api.calls)


def test_newly_created_tool_is_rediscovered_and_disabled_on_apply_failure(script):
    del script.api.responses[tool_route(script)]
    del script.api.responses[filter_route(script)]
    script.api.failures[("POST", "/api/v1/models/create")] = 500
    with pytest.raises(script.module.ConfigurationError, match="HTTP 500"):
        run(script, "--apply")
    created = [(route, payload) for method, route, payload in script.api.writes
               if route in {"/api/v1/tools/create", "/api/v1/functions/create"}]
    assert len(created) == 2
    assert all(payload["meta"]["description"] == script.module.MARKER for route, payload in created)
    assert next(payload for route, payload in created if route == "/api/v1/tools/create")["access_grants"] == []
    assert script.api.responses[filter_route(script)]["is_active"] is True
    final_valves = script.api.responses[tool_route(script) + "/valves"]
    assert final_valves["search_enabled"] is False
    assert final_valves["fetch_enabled"] is False
    assert final_valves["browser_enabled"] is False
    assert ("GET", tool_route(script), None) in script.api.calls[-3:]
    assert all(method != "DELETE" for method, route, payload in script.api.calls)


@pytest.mark.parametrize("origin", ["http://owui.example.invalid", ORIGIN + "/admin", ORIGIN + "?token=secret",
                                    ORIGIN + "#fragment", "https://admin:secret@owui.example.invalid",
                                    "https://:secret@owui.example.invalid"])
def test_cli_rejects_non_origin_or_credential_bearing_base_url_before_network(script, origin):
    with pytest.raises(script.module.ConfigurationError):
        run(script, **{"base-url": origin})
    assert script.api.client_options == []
    assert script.api.calls == []


def test_missing_environment_token_and_conflicting_actions_never_open_client(script):
    script.monkeypatch.delenv("OWUI_ADMIN_TOKEN")
    with pytest.raises(script.module.ConfigurationError, match="OWUI_ADMIN_TOKEN"):
        run(script)
    with pytest.raises(SystemExit) as error:
        run(script, "--apply", "--disable")
    assert error.value.code == 2
    assert script.api.client_options == []


@pytest.mark.parametrize("status", [302, 403, 500])
def test_api_error_is_bounded_and_never_writes_or_exposes_token(script, status):
    script.api.failures[("GET", "/api/version")] = status
    with pytest.raises(script.module.ConfigurationError) as error:
        run(script, "--apply")
    assert f"HTTP {status}" in str(error.value)
    assert TOKEN not in str(error.value)
    assert script.api.writes == []

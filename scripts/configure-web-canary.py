"""Configure only owned canary objects through pinned Open WebUI admin APIs.

Read OWUI_ADMIN_TOKEN from the environment, never command-line credentials.
Default is a read-only plan. --apply installs; --disable closes Tool feature
valves without deleting chats or changing existing model connections/config.
Existing objects must carry this script's marker and belong to the authenticated
operator. Source updates additionally require --update-code. No global filter,
native search setting, role, existing model, or database schema is modified.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx


MARKER = "Managed AgentCore explicit web canary; not native-toggle integration"
TOOL = "agentcore_web_canary"
FILTER = "agentcore_web_canary_policy"
ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "agentcore-web-haiku-canary": ("AgentCore web canary (Haiku)", "gateway_anthropic.anthropic.claude-haiku-4-5"),
    "agentcore-web-responses-canary": ("AgentCore web canary (Responses)", "gwr.openai.gpt-6-astra"),
}


class ConfigurationError(RuntimeError):
    pass


class CanaryConfigurator:
    def __init__(self, client, *, subject, function_arn, region, update_code=False, responses_model=None):
        self.client = client
        self.subject, self.function_arn, self.region = subject, function_arn, region
        self.update_code = update_code
        self.model_definitions = dict(MODELS)
        if responses_model:
            if not isinstance(responses_model, str) or not 1 <= len(responses_model) <= 256:
                raise ConfigurationError("Invalid explicitly selected Responses model")
            self.model_definitions["agentcore-web-responses-canary"] = ("AgentCore web canary (Responses)", responses_model)

    def request(self, method, route, payload=None, *, optional=False):
        response = self.client.request(method, route, json=payload)
        if optional and response.status_code in {401, 404}:
            return None
        if response.status_code != 200 or "application/json" not in response.headers.get("content-type", ""):
            raise ConfigurationError(f"Canary API failed: {method} {route.split('?')[0]} HTTP {response.status_code}")
        return response.json()

    def verify(self, enforce_configuration=True):
        version = self.request("GET", "/api/version")
        if version.get("version") != "0.11.3":
            raise ConfigurationError("This canary requires separately tested Open WebUI v0.11.3")
        own = self.request("GET", "/api/v1/auths/")
        if own.get("id") != self.subject or own.get("role") != "admin":
            raise ConfigurationError("Expected authorized synthetic admin identity does not match")
        self.owner = own["id"]
        self.catalog = set()
        if enforce_configuration:
            retrieval = self.request("GET", "/api/v1/retrieval/config")
            if retrieval.get("web", {}).get("ENABLE_WEB_SEARCH") is not False:
                raise ConfigurationError("Keep global native web search disabled for this explicit canary")
            catalog = self.request("GET", "/api/models")
            self.catalog = {model["id"] for model in catalog.get("data", [])}

    def owned(self, item):
        if item and (item.get("user_id") != self.owner or item.get("meta", {}).get("description") != MARKER):
            raise ConfigurationError("Refusing to overwrite an existing unowned or unmarked object")
        return item

    def inspect(self, include_models=True):
        self.verify(enforce_configuration=include_models)
        self.tool = self.owned(self.request("GET", f"/api/v1/tools/id/{TOOL}", optional=True))
        if not include_models:
            return {"action": "disable-plan"}
        self.filter = self.owned(self.request("GET", f"/api/v1/functions/id/{FILTER}", optional=True))
        self.models = {}
        for model_id, (_name, base_id) in self.model_definitions.items():
            if base_id not in self.catalog:
                raise ConfigurationError("Required baseline model lane is unavailable; do not silently enable connections")
            item = self.request("GET", f"/api/v1/models/model?id={model_id}", optional=True)
            self.models[model_id] = self.owned(item)
        return {"action": "plan", "tool": "update" if self.tool else "create",
                "filter": "update" if self.filter else "create", "models": list(self.model_definitions),
                "existing_connections_unchanged": True, "global_search_unchanged": True}

    def _upsert_code(self, collection, identifier, name, content, existing):
        form = {"id": identifier, "name": name, "content": content, "meta": {"description": MARKER}}
        if collection == "tools":
            form["access_grants"] = []
        if existing:
            if existing.get("content") != content:
                if not self.update_code:
                    raise ConfigurationError("Source differs; inspect it and explicitly use --update-code")
                self.request("POST", f"/api/v1/{collection}/id/{identifier}/update", form)
        else:
            self.request("POST", f"/api/v1/{collection}/create", form)

    def disable(self):
        if self.tool:
            valves = self.request("GET", f"/api/v1/tools/id/{TOOL}/valves") or {}
            self.request("POST", f"/api/v1/tools/id/{TOOL}/valves/update",
                         {**valves, "search_enabled": False, "fetch_enabled": False, "browser_enabled": False})
        return {"action": "disabled", "data_deleted": False, "existing_connections_unchanged": True}

    def apply(self):
        self._upsert_code("functions", FILTER, "AgentCore canary policy",
                          (ROOT / "pipe/agentcore_web_filter.py").read_text(), self.filter)
        self.request("POST", f"/api/v1/functions/id/{FILTER}/valves/update", {
            "allowed_subjects": [self.subject], "allowed_model_ids": list(self.model_definitions), "priority": 1000,
        })
        actual_filter = self.request("GET", f"/api/v1/functions/id/{FILTER}")
        if actual_filter.get("is_global"):
            raise ConfigurationError("Canary policy must not be a global filter")
        if not actual_filter.get("is_active"):
            self.request("POST", f"/api/v1/functions/id/{FILTER}/toggle")
        self._upsert_code("tools", TOOL, "AgentCore public web canary",
                          (ROOT / "pipe/agentcore_web_tools.py").read_text(), self.tool)
        self.request("POST", f"/api/v1/tools/id/{TOOL}/valves/update", {
            "region": self.region, "function_arn": self.function_arn, "allowed_subjects": [self.subject],
            "search_enabled": True, "fetch_enabled": True, "browser_enabled": True,
        })
        for model_id, (name, base_id) in self.model_definitions.items():
            existing = self.models[model_id]
            form = {"id": model_id, "base_model_id": base_id, "name": name, "is_active": True,
                "access_grants": [],
                "params": {"function_calling": "native", "max_tokens": 1024},
                "meta": {"description": MARKER, "toolIds": [TOOL], "filterIds": [FILTER], "knowledge": [],
                    "capabilities": {"builtin_tools": False, "web_search": False, "file_upload": False,
                                     "file_context": False, "citations": True}}}
            if existing:
                compatible = all(existing.get(key) == value for key, value in form.items() if key not in {"meta", "params"})
                for group in ("meta", "params"):
                    compatible = compatible and all(existing.get(group, {}).get(key) == value for key, value in form[group].items())
                if not compatible:
                    raise ConfigurationError("Existing canary model settings differ; preserve and review admin edits")
            else:
                self.request("POST", "/api/v1/models/create", form)
        return {"action": "configured", "models": list(self.model_definitions), "existing_connections_unchanged": True,
                "global_search_unchanged": True, "live_tool_invocation_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--function-arn", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--responses-model", help="Explicit already-enabled Responses-lane model ID; never enables a connection")
    parser.add_argument("--update-code", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--disable", action="store_true")
    arguments = parser.parse_args()
    parsed = urlsplit(arguments.base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ConfigurationError("Expected the verified HTTPS application origin")
    token = os.environ.get("OWUI_ADMIN_TOKEN", "")
    if not token:
        raise ConfigurationError("Provide the authorized operator token through OWUI_ADMIN_TOKEN")
    with httpx.Client(base_url=arguments.base_url.rstrip("/"), headers={"Authorization": "Bearer " + token},
                      timeout=30, follow_redirects=False, trust_env=False) as client:
        configurator = CanaryConfigurator(client, subject=arguments.subject, function_arn=arguments.function_arn,
                                          region=arguments.region, update_code=arguments.update_code,
                                          responses_model=arguments.responses_model)
        result = configurator.inspect(include_models=not arguments.disable)
        try:
            if arguments.disable:
                result = configurator.disable()
            elif arguments.apply:
                result = configurator.apply()
        except Exception:
            configurator.tool = configurator.owned(configurator.request("GET", f"/api/v1/tools/id/{TOOL}", optional=True))
            configurator.disable()
            raise
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ConfigurationError as error:
        raise SystemExit(str(error)) from None

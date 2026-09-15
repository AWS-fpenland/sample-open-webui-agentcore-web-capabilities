"""
title: AgentCore explicit web canary policy
description: Mandatory model-attached policy for allowlisted explicit Python web tools.
version: 0.1.0

This non-toggleable Filter targets Open WebUI v0.11.3's streaming native loop,
not its native Web Search toggle or external search provider. Both allowlists
default empty. Attach the active Filter only to reviewed canary workspace models.

This is a functional, text-only canary, not global URL or network governance.
Authenticated retrieval routes run outside these hooks. Remote image URLs,
including images restored from saved chats, may be fetched before inlet runs.
Rejecting attachments here cannot undo those requests or prevent direct retrieval.

The Tool wrapper must require request.state.agentcore_web_canary_authorized,
reject agentcore_web_canary_calls >= 4, and increment that counter before awaiting
work. Only inlet initializes the counter; request follow-ups never replenish it.
The separate request.state.max_tool_call_iterations = 3 limits native loop rounds,
not individual calls, and is a pinned request-state override, not a model setting.
Lambda must independently authorize subjects and enforce durable quotas.
"""

from pydantic import BaseModel, ConfigDict, Field


TOOLKIT_ID = "agentcore_web_canary"
TOOL_ARGUMENTS = {
    "search_web": {"query": "string", "count": "integer"},
    "fetch_url": {"url": "string", "render": "boolean"},
}
TOOL_REQUIRED = {"search_web": ["query"], "fetch_url": ["url"]}


class Filter:
    class Valves(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        allowed_subjects: list[str] = Field(default_factory=list)
        allowed_model_ids: list[str] = Field(default_factory=list)
        priority: int = Field(default=1000)

    def __init__(self):
        self.valves = self.Valves()

    def _state(self, request):
        if request is None or not hasattr(request, "state"):
            raise ValueError("Canary requires trusted request state")
        request.state.agentcore_web_canary_authorized = False
        return request.state

    def _scope(self, user, metadata, model):
        if (
            not isinstance(user, dict)
            or not isinstance(user.get("id"), str)
            or not user["id"]
            or user["id"] not in self.valves.allowed_subjects
            or user.get("role") not in ("user", "admin")
        ):
            raise ValueError("Canary subject is not authorized")
        if not isinstance(metadata, dict) or not isinstance(model, dict):
            raise ValueError("Canary requires trusted model metadata")
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id not in self.valves.allowed_model_ids:
            raise ValueError("Canary model is not authorized")
        if not isinstance(metadata.get("model"), dict) or metadata["model"].get("id") != model_id:
            raise ValueError("Canary model metadata does not match")
        return {"subject": user["id"], "model_id": model_id}

    def _inputs(self, body, metadata):
        if not isinstance(body, dict) or body.get("stream") is not True:
            raise ValueError("Canary requires streaming")
        for container in (body, metadata, body.get("metadata", {})):
            if not isinstance(container, dict):
                raise ValueError("Invalid canary metadata")
            features = container.get("features") or {}
            if not isinstance(features, dict) or features.get("web_search"):
                raise ValueError("Native Web Search is not allowed in this canary")
            if container.get("tool_servers") or container.get("direct_tool_servers") or container.get("direct"):
                raise ValueError("External or direct tool servers are not allowed")
            tool_ids = container.get("tool_ids")
            if tool_ids is not None and (
                not isinstance(tool_ids, list) or tool_ids not in ([], [TOOLKIT_ID])
            ):
                raise ValueError("Only the canary toolkit may be selected")
        self._attachments(body)
        self._attachments(metadata)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Canary requires text messages")
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Invalid canary message")
            content = message.get("content")
            if isinstance(content, list):
                if any(
                    not isinstance(part, dict)
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                    for part in content
                ):
                    raise ValueError("Canary accepts text-only message content")
            elif content is not None and not isinstance(content, str):
                raise ValueError("Canary accepts text-only message content")

    def _attachments(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {
                    "files", "attachments", "images", "image_url", "image", "input_image",
                    "input_audio", "audio_url", "video_url", "folder_id", "folder_knowledge",
                    "knowledge", "terminal_id",
                } and item:
                    raise ValueError("Attachments and attached knowledge are not allowed")
                if key not in {"tools", "spec", "parameters"}:
                    self._attachments(item)
        elif isinstance(value, list):
            for item in value:
                self._attachments(item)

    def _spec(self, name, spec):
        if not isinstance(spec, dict) or spec.get("name") != name:
            raise ValueError("Canary tool name does not match registry")
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("Canary tool must have object parameters")
        properties = parameters.get("properties")
        if not isinstance(properties, dict) or set(properties) != set(TOOL_ARGUMENTS[name]):
            raise ValueError("Unexpected canary tool arguments")
        if parameters.get("required") != TOOL_REQUIRED[name]:
            raise ValueError("Unexpected required canary tool arguments")
        if parameters.get("additionalProperties", False) is not False:
            raise ValueError("Additional canary tool arguments are not allowed")
        for argument, expected_type in TOOL_ARGUMENTS[name].items():
            schema = properties[argument]
            if not isinstance(schema, dict) or schema.get("type") != expected_type:
                raise ValueError("Unexpected canary tool argument type")

    async def inlet(self, body: dict, __user__: dict = None, __metadata__: dict = None,
                    __request__=None, __model__: dict = None) -> dict:
        state = self._state(__request__)
        scope = self._scope(__user__, __metadata__, __model__)
        self._inputs(body, __metadata__)
        if "tools" in body:
            raise ValueError("Explicit tools payloads are not allowed")
        if body.get("model") != scope["model_id"]:
            raise ValueError("Canary request model does not match")
        params = __metadata__.get("params")
        if not isinstance(params, dict):
            raise ValueError("Canary requires trusted parameter metadata")
        existing_scope = getattr(state, "agentcore_web_canary_context", None)
        if existing_scope is not None and existing_scope != scope:
            raise ValueError("Canary request scope cannot change")
        if existing_scope is None:
            state.agentcore_web_canary_calls = 0
        self._counter(state)
        params["function_calling"] = "native"
        body["metadata"] = __metadata__
        body["tool_ids"] = [TOOLKIT_ID]
        __metadata__["tool_ids"] = [TOOLKIT_ID]
        state.agentcore_web_canary_context = scope
        state.max_tool_call_iterations = 3
        state.agentcore_web_canary_authorized = True
        return body

    def _counter(self, state):
        calls = getattr(state, "agentcore_web_canary_calls", None)
        if type(calls) is not int or not 0 <= calls <= 4:
            raise ValueError("Invalid canary invocation counter")

    async def request(self, body: dict, __user__: dict = None, __metadata__: dict = None,
                      __request__=None, __model__: dict = None) -> dict:
        state = self._state(__request__)
        scope = self._scope(__user__, __metadata__, __model__)
        if getattr(state, "agentcore_web_canary_context", None) != scope:
            raise ValueError("Canary inlet authorization is required")
        self._counter(state)
        self._inputs(body, __metadata__)
        if (__metadata__.get("params") or {}).get("function_calling") != "native":
            raise ValueError("Canary native function calling was changed")
        base_model_id = (__model__.get("info") or {}).get("base_model_id")
        if body.get("model") != scope["model_id"] and (
            not base_model_id or body.get("model") != base_model_id
        ):
            raise ValueError("Canary request model does not match")
        registry = __metadata__.get("tools")
        if not isinstance(registry, dict) or set(registry) != set(TOOL_ARGUMENTS):
            raise ValueError("Canary requires exactly search_web and fetch_url")
        for name, tool in registry.items():
            if (
                not isinstance(tool, dict)
                or tool.get("tool_id") != TOOLKIT_ID
                or not callable(tool.get("callable"))
                or tool.get("direct", False) is not False
                or tool.get("type") not in (None, "")
            ):
                raise ValueError("Canary callable must belong to the approved local toolkit")
            self._spec(name, tool.get("spec"))
        outgoing = body.get("tools")
        if not isinstance(outgoing, list) or len(outgoing) != 2:
            raise ValueError("Canary requires exactly two outgoing tool schemas")
        names = set()
        for tool in outgoing:
            if not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool["type"] != "function":
                raise ValueError("Invalid outgoing canary tool schema")
            spec = tool["function"]
            name = spec.get("name") if isinstance(spec, dict) else None
            if name not in registry or name in names or spec != registry[name]["spec"]:
                raise ValueError("Outgoing canary schemas do not match the approved registry")
            names.add(name)
        state.max_tool_call_iterations = 3
        state.agentcore_web_canary_authorized = True
        return body

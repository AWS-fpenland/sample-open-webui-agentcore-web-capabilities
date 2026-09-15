"""Offline Lambda-wrapper contracts; no AWS or live native-extractor claims."""

import asyncio
import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ReadTimeoutError


REGION = "us-east-1"
ARN = "arn:aws:lambda:us-east-1:123456789012:function:web-canary:live"
REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
USER = {"id": "canary-subject", "role": "user", "name": "Private Name", "email": "private@example.com"}
RECORDS = [{"link": "https://example.com/page", "title": "Public page", "snippet": "Public evidence café"}]
DOCUMENT = {"title": "Public page", "final_url": "https://example.com/page", "text": "Public page content",
            "source_capability": "static", "truncated": True, "links": ["https://example.com/next"]}


class FakePayload:
    def __init__(self, raw):
        self.raw = raw
        self.read_sizes = []
        self.close_count = 0
        self.error = None

    def read(self, size):
        self.read_sizes.append(size)
        if self.error:
            raise self.error
        return self.raw[:size]

    def close(self):
        self.close_count += 1


class FakeLambda:
    def __init__(self):
        self.invocations = []
        self.clients = []
        self.factory_calls = []
        self.result = {"records": RECORDS, "document": DOCUMENT}
        self.response_updates = {}
        self.raw = None
        self.invoke_error = None
        self.read_error = None

    def factory(self, service, **options):
        self.factory_calls.append((service, options))
        owner = self

        class Client:
            def __init__(self):
                self.close_count = 0
                self.stream = None

            def invoke(self, **arguments):
                owner.invocations.append(arguments)
                if owner.invoke_error:
                    raise owner.invoke_error
                payload = json.loads(arguments["Payload"])
                result = {"request_id": payload["request_id"], **owner.result}
                raw = owner.raw if owner.raw is not None else json.dumps(result).encode()
                self.stream = FakePayload(raw)
                self.stream.error = owner.read_error
                return {"StatusCode": 200, "Payload": self.stream, **owner.response_updates}

            def close(self):
                self.close_count += 1

        client = Client()
        self.clients.append(client)
        return client


@pytest.fixture
def wrapper(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "agentcore_web_tools.py"
    spec = importlib.util.spec_from_file_location("agentcore_web_tools_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = FakeLambda()
    monkeypatch.setattr(module.boto3, "client", fake.factory)

    def forbidden_session(*args, **kwargs):
        raise AssertionError("Tests must never resolve an AWS profile or credentials")

    monkeypatch.setattr(module.boto3, "Session", forbidden_session)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: uuid.UUID(REQUEST_ID))
    tool = module.Tools()
    tool.valves.region = REGION
    tool.valves.function_arn = ARN
    tool.valves.allowed_subjects = [USER["id"]]
    tool.valves.search_enabled = True
    tool.valves.fetch_enabled = True
    tool.valves.browser_enabled = True
    request = SimpleNamespace(state=SimpleNamespace(agentcore_web_canary_authorized=True))
    return SimpleNamespace(tool=tool, fake=fake, request=request, module=module)


def call(wrapper, method="search_web", **overrides):
    arguments = {"query": "public evidence"} if method == "search_web" else {"url": "https://example.com/page"}
    arguments.update({"__user__": dict(USER), "__request__": wrapper.request})
    arguments.update(overrides)
    return asyncio.run(getattr(wrapper.tool, method)(**arguments))


def assert_denied_without_aws(wrapper, **arguments):
    assert "error" in json.loads(call(wrapper, **arguments))
    assert wrapper.fake.factory_calls == []
    assert wrapper.fake.invocations == []


@pytest.mark.parametrize("user", [None, {}, {**USER, "id": ""}, {**USER, "id": True},
                                  {**USER, "id": "x" * 257}, {**USER, "role": "pending"},
                                  {**USER, "id": "canary-subject-extra"},
                                  {**USER, "id": "CANARY-SUBJECT"},
                                  {**USER, "id": "other-admin", "role": "admin"}])
def test_only_exact_allowlisted_authenticated_subject_is_admitted(wrapper, user):
    assert_denied_without_aws(wrapper, __user__=user)


@pytest.mark.parametrize("role", ["user", "admin"])
def test_allowlisted_role_still_requires_trusted_request_flag(wrapper, role):
    wrapper.request.state.agentcore_web_canary_authorized = False
    assert_denied_without_aws(wrapper, __user__={**USER, "role": role},
                              __metadata__={"agentcore_web_canary_authorized": True, "model": "canary"})


@pytest.mark.parametrize("flag", [None, False, 0, "true", "false", 1, {"authorized": True}])
def test_authorization_flag_must_be_literal_true(wrapper, flag):
    wrapper.request.state.agentcore_web_canary_authorized = flag
    assert_denied_without_aws(wrapper)


def test_missing_request_or_flag_is_denied(wrapper):
    assert_denied_without_aws(wrapper, __request__=None)
    del wrapper.request.state.agentcore_web_canary_authorized
    assert_denied_without_aws(wrapper)


@pytest.mark.parametrize("method", ["search_web", "fetch_url"])
def test_legacy_and_default_off_flags_prevent_invocation(wrapper, method):
    assert_denied_without_aws(wrapper, method=method,
                              __metadata__={"params": {"function_calling": "legacy"}})
    defaults = wrapper.module.Tools.Valves()
    assert defaults.search_enabled is False and defaults.fetch_enabled is False
    assert defaults.browser_enabled is False
    wrapper.tool.valves.search_enabled = False
    wrapper.tool.valves.fetch_enabled = False
    assert_denied_without_aws(wrapper, method=method)


def test_search_and_fetch_flags_are_independent(wrapper):
    wrapper.tool.valves.fetch_enabled = False
    assert json.loads(call(wrapper)) == RECORDS
    assert "error" in json.loads(call(wrapper, "fetch_url"))
    wrapper.tool.valves.search_enabled = False
    wrapper.tool.valves.fetch_enabled = True
    assert "error" in json.loads(call(wrapper))
    assert "Source: https://example.com/page" in call(wrapper, "fetch_url")
    assert len(wrapper.fake.invocations) == 2


def test_browser_disabled_leaves_static_fetch_enabled(wrapper):
    wrapper.tool.valves.browser_enabled = False
    assert_denied_without_aws(wrapper, method="fetch_url", render=True)
    output = call(wrapper, "fetch_url", render=False)
    assert "Capability: static" in output
    assert len(wrapper.fake.invocations) == 1
    assert json.loads(wrapper.fake.invocations[0]["Payload"])["operation"] == "static"


def test_browser_also_requires_fetch_flag(wrapper):
    wrapper.tool.valves.fetch_enabled = False
    wrapper.tool.valves.browser_enabled = True
    assert_denied_without_aws(wrapper, method="fetch_url", render=True)


def test_four_call_limit_is_shared_and_incremented_before_first_await(wrapper):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        started = []

        async def emitter(event):
            if event["data"]["done"] is False:
                started.append(event)
                if len(started) == 4:
                    entered.set()
                await release.wait()

        tasks = [asyncio.create_task(wrapper.tool.search_web(
            "public evidence", __user__=USER, __request__=wrapper.request,
            __event_emitter__=emitter)) for attempt in range(4)]
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert wrapper.request.state.agentcore_web_canary_calls == 4
            assert wrapper.fake.invocations == []
            denied = await asyncio.wait_for(wrapper.tool.fetch_url(
                "https://example.com/page", __user__=USER, __request__=wrapper.request), timeout=1)
            assert "call limit" in json.loads(denied)["error"]
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert all(json.loads(result) == RECORDS for result in results)

    asyncio.run(scenario())
    assert len(wrapper.fake.invocations) == 4


@pytest.mark.parametrize("counter", [4, 5, True, "0", None, -1])
def test_invalid_or_exhausted_call_counter_denies_before_aws(wrapper, counter):
    wrapper.request.state.agentcore_web_canary_calls = counter
    assert_denied_without_aws(wrapper)


def test_payload_whitelist_ignores_model_identity_and_arn(wrapper):
    metadata = {"chat_id": "trusted-chat", "params": {"function_calling": "native"},
                "subject": "model-subject", "role": "admin", "email": "model@example.com",
                "name": "Model Name", "model": "model-identifier", "function_arn": "model-arn",
                "request_id": "model-request-id", "user": {"id": "model-subject"}}
    call(wrapper, __metadata__=metadata)
    invocation = wrapper.fake.invocations[0]
    assert invocation["FunctionName"] == ARN
    assert invocation["InvocationType"] == "RequestResponse"
    assert isinstance(invocation["Payload"], bytes)
    assert json.loads(invocation["Payload"]) == {
        "operation": "search", "arguments": {"query": "public evidence", "count": 3},
        "subject": USER["id"], "role": "user", "request_id": REQUEST_ID, "chat_id": "trusted-chat",
    }
    serialized = invocation["Payload"].decode()
    for private in (USER["email"], USER["name"], "model-subject", "model-arn", "model-request-id", ARN):
        assert private not in serialized


@pytest.mark.parametrize("chat_id", [None, "", True, [], "x" * 257])
def test_invalid_optional_chat_is_not_forwarded(wrapper, chat_id):
    call(wrapper, __metadata__={"chat_id": chat_id})
    assert "chat_id" not in json.loads(wrapper.fake.invocations[0]["Payload"])


@pytest.mark.parametrize("region,arn", [
    (REGION, ARN.rsplit(":", 1)[0]), (REGION, ARN.replace(":live", ":$LATEST")),
    (REGION, ARN.replace(":live", ":other")), (REGION, ARN.replace("us-east-1", "eu-west-1")),
    (REGION, ARN.replace("123456789012", "1234")), (REGION, "web-canary"),
    ("us-west-2", ARN.replace("us-east-1", "us-west-2")), (REGION, ARN + "\n"),
])
def test_invalid_or_unqualified_function_arn_fails_before_client_creation(wrapper, region, arn):
    wrapper.tool.valves.region = region
    wrapper.tool.valves.function_arn = arn
    assert_denied_without_aws(wrapper)


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1", "ap-northeast-1"])
def test_qualified_arn_explicit_region_bounded_sdk_and_resource_cleanup(wrapper, region):
    wrapper.tool.valves.region = region
    wrapper.tool.valves.function_arn = ARN.replace(REGION, region)
    call(wrapper)
    service, options = wrapper.fake.factory_calls[0]
    assert service == "lambda"
    assert set(options) == {"region_name", "config"}
    assert options["region_name"] == region
    assert options["config"].retries == {"total_max_attempts": 1}
    assert options["config"].connect_timeout == 3
    assert options["config"].read_timeout == 65
    assert wrapper.fake.invocations[0]["FunctionName"] == ARN.replace(REGION, region)
    client = wrapper.fake.clients[0]
    assert client.stream.read_sizes == [262145]
    assert client.stream.close_count == 1
    assert client.close_count == 1


@pytest.mark.parametrize("failure", ["function", "status", "oversize", "json", "array", "correlation",
                                     "missing_correlation", "read_timeout", "invoke_timeout", "missing_payload"])
def test_lambda_failures_are_closed_without_retry_or_success_status(wrapper, failure):
    fake = wrapper.fake
    if failure == "function":
        fake.response_updates = {"FunctionError": "Unhandled"}
    elif failure == "status":
        fake.response_updates = {"StatusCode": 202}
    elif failure == "oversize":
        fake.raw = b"x" * 262145
    elif failure == "json":
        fake.raw = b"not JSON private AWS detail"
    elif failure == "array":
        fake.raw = b"[]"
    elif failure == "correlation":
        fake.result = {"request_id": "wrong-request", "records": RECORDS}
    elif failure == "missing_correlation":
        fake.raw = json.dumps({"records": RECORDS}).encode()
    elif failure == "read_timeout":
        fake.read_error = ReadTimeoutError(endpoint_url="https://private.invalid")
    elif failure == "invoke_timeout":
        fake.invoke_error = ReadTimeoutError(endpoint_url="https://private.invalid")
    elif failure == "missing_payload":
        fake.response_updates = {"Payload": None}
    events = []

    async def emitter(event):
        events.append(event)

    result = json.loads(call(wrapper, __event_emitter__=emitter))
    assert result == {"error": "Web capability failed or was rejected; no fallback attempted", "request_id": REQUEST_ID}
    assert len(fake.invocations) == 1
    assert len(fake.clients) == 1
    assert fake.clients[0].close_count == 1
    if failure not in {"invoke_timeout", "missing_payload"}:
        assert fake.clients[0].stream.close_count == 1
        assert fake.clients[0].stream.read_sizes == [262145]
    assert [event["type"] for event in events] == ["status", "status"]
    assert events[-1]["data"] == {"description": "Web capability unavailable or rejected", "done": True}


def test_search_returns_json_string_array_for_native_source_extraction_without_manual_citation(wrapper):
    events = []

    async def emitter(event):
        events.append(event)

    output = call(wrapper, __event_emitter__=emitter)
    assert isinstance(output, str)
    assert json.loads(output) == RECORDS
    assert isinstance(json.loads(output), list)
    assert wrapper.tool.citation is False
    assert [event["type"] for event in events] == ["status", "status"]
    assert events[0]["data"]["done"] is False
    assert events[-1]["data"] == {"description": "Web capability completed", "done": True}


@pytest.mark.parametrize("render,operation", [(False, "static"), (True, "browser")])
def test_fetch_returns_plain_text_provenance_and_truncation_without_manual_citation(wrapper, render, operation):
    wrapper.fake.result = {"document": {**DOCUMENT, "source_capability": operation}}
    events = []

    async def emitter(event):
        events.append(event)

    output = call(wrapper, "fetch_url", render=render, __event_emitter__=emitter)
    assert isinstance(output, str)
    assert output == ("Title: Public page\nSource: https://example.com/page\n"
                      f"Capability: {operation}\nOptional resources omitted: 0 (rendering may differ)\nTruncated: True\n\nPublic page content\n\n"
                      'Links (not yet fetched): ["https://example.com/next"]')
    payload = json.loads(wrapper.fake.invocations[0]["Payload"])
    assert payload["operation"] == operation
    assert payload["arguments"] == {"url": "https://example.com/page"}
    assert len(wrapper.fake.invocations) == 1
    assert wrapper.tool.citation is False
    assert [event["type"] for event in events] == ["status", "status"]
    assert events[-1]["data"] == {"description": "Web capability completed", "done": True}


@pytest.mark.parametrize("method,result", [
    ("search_web", {"error": "Daily quota exceeded"}),
    ("search_web", {"records": {}}), ("search_web", {"records": RECORDS * 4}),
    ("search_web", {"records": [{"link": 1, "snippet": "text"}]}),
    ("search_web", {"records": [{"link": "https://example.com/"}]}),
    ("fetch_url", {"document": {**DOCUMENT, "text": "x" * 20001}}),
    ("fetch_url", {"document": {**DOCUMENT, "text": None}}),
    ("fetch_url", {"document": {**DOCUMENT, "final_url": None}}),
])
def test_rejected_results_finish_with_honest_failure_status(wrapper, method, result):
    wrapper.fake.result = result
    events = []

    async def emitter(event):
        events.append(event)

    output = json.loads(call(wrapper, method, __event_emitter__=emitter))
    assert "error" in output
    assert output["request_id"] == REQUEST_ID
    assert events[-1]["data"] == {"description": "Web capability unavailable or rejected", "done": True}
    assert all(event["type"] == "status" for event in events)
    assert len(wrapper.fake.invocations) == 1


@pytest.mark.parametrize("method,arguments", [
    ("search_web", {"query": " "}), ("search_web", {"query": "x" * 201}),
    ("search_web", {"count": True}), ("search_web", {"count": 4}),
    ("fetch_url", {"url": ""}), ("fetch_url", {"url": "x" * 2049}),
    ("fetch_url", {"render": "true"}),
])
def test_invalid_model_arguments_never_invoke_lambda(wrapper, method, arguments):
    assert_denied_without_aws(wrapper, method=method, **arguments)


def test_thread_dispatch_has_bounded_outer_timeout_and_no_retry(wrapper, monkeypatch):
    dispatched = []
    deadlines = []
    events = []
    original_wait_for = asyncio.wait_for

    async def fake_to_thread(function, payload):
        dispatched.append((function, payload))
        raise TimeoutError("synthetic deadline")

    async def tracked_wait_for(awaitable, *, timeout):
        deadlines.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    async def emitter(event):
        events.append(event)

    monkeypatch.setattr(wrapper.module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(wrapper.module.asyncio, "wait_for", tracked_wait_for)
    output = json.loads(call(wrapper, __event_emitter__=emitter))
    assert "error" in output
    assert deadlines == [68]
    assert len(dispatched) == 1
    assert dispatched[0][0] == wrapper.tool._invoke
    assert dispatched[0][1]["request_id"] == REQUEST_ID
    assert wrapper.fake.factory_calls == []
    assert events[-1]["data"] == {"description": "Web capability unavailable or rejected", "done": True}


def test_cancellation_propagates_and_finishes_failure_status_without_live_thread(wrapper, monkeypatch):
    events = []

    async def scenario():
        entered = asyncio.Event()

        async def fake_to_thread(function, payload):
            entered.set()
            await asyncio.Event().wait()

        async def emitter(event):
            events.append(event)

        monkeypatch.setattr(wrapper.module.asyncio, "to_thread", fake_to_thread)
        task = asyncio.create_task(wrapper.tool.search_web(
            "public evidence", __user__=USER, __request__=wrapper.request, __event_emitter__=emitter))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert wrapper.fake.factory_calls == []
    assert wrapper.request.state.agentcore_web_canary_calls == 1
    assert [event["type"] for event in events] == ["status", "status"]
    assert events[-1]["data"] == {"description": "Web capability unavailable or rejected", "done": True}

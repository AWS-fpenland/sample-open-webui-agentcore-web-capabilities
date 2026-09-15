import copy
import itertools
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.session import get_session
from botocore.validate import validate_parameters

from web_capabilities import quota
from web_capabilities.quota import QuotaExceeded, QuotaLimits, QuotaStore, QuotaUnavailable


NOW = datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc)
TABLE = "web-quota-test"
REGION = "us-west-2"


def aws_error(code):
    return ClientError({"Error": {"Code": code, "Message": "private AWS details"}}, "TransactWriteItems")


class FakeDynamoDB:
    """Atomic, deterministic boto3 method fixture; no reads or AWS credentials."""

    def __init__(self):
        self.items = {}
        self.tokens = {}
        self.calls = []
        self.error = None
        self.commit_then_timeout = False
        self.lock = threading.Lock()
        self.shape = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems").input_shape

    def transact_write_items(self, **request):
        with self.lock:
            self.calls.append(copy.deepcopy(request))
            validate_parameters(request, self.shape)
            if self.error:
                raise self.error
            token = request["ClientRequestToken"]
            if token in self.tokens:
                if self.tokens[token] != request:
                    raise aws_error("IdempotentParameterMismatchException")
                return {}
            updates = [entry["Update"] for entry in request["TransactItems"]]
            keys = [update["Key"]["pk"]["S"] for update in updates]
            assert len(keys) == len(set(keys))
            pending = copy.deepcopy(self.items)
            for update, key in zip(updates, keys):
                assert update["TableName"] == TABLE
                assert update["UpdateExpression"] == "SET #expires = :expires ADD #used :one"
                assert update["ConditionExpression"] == (
                    "(attribute_not_exists(#used) OR #used >= :zero) AND "
                    "(attribute_not_exists(#used) OR #used < :limit) AND :limit > :zero"
                )
                assert update["ExpressionAttributeNames"] == {"#used": "used", "#expires": "expires_at"}
                values = update["ExpressionAttributeValues"]
                assert set(values) == {":one", ":zero", ":limit", ":expires"}
                assert values[":one"] == {"N": "1"}
                assert values[":zero"] == {"N": "0"}
                for value in values.values():
                    assert set(value) == {"N"}
                    assert str(int(value["N"])) == value["N"]
                count = pending.get(key, {}).get("used", 0)
                if not 0 <= count < int(values[":limit"]["N"]):
                    raise aws_error("TransactionCanceledException")
                pending[key] = {"used": count + 1, "expires_at": int(values[":expires"]["N"])}
            self.items = pending
            self.tokens[token] = copy.deepcopy(request)
            if self.commit_then_timeout:
                raise ReadTimeoutError(endpoint_url="https://dynamodb.invalid")
            return {}


@pytest.fixture(autouse=True)
def no_default_aws(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must inject a client, never resolve AWS credentials")
    monkeypatch.setattr(quota.boto3, "client", forbidden)
    monkeypatch.setattr(quota.boto3, "Session", forbidden)
    sequence = itertools.count(1)
    monkeypatch.setattr(quota.uuid, "uuid4", lambda: uuid.UUID(int=next(sequence)))


@pytest.fixture
def client():
    return FakeDynamoDB()


@pytest.fixture
def store(client):
    return QuotaStore(table_name=TABLE, region=REGION, client=client, clock=lambda: NOW)


@pytest.mark.parametrize("capability,user_limit,deployment_limit", [("search", 10, 30), ("browser", 5, 15)])
def test_default_user_and_deployment_limits_are_atomic(store, client, capability, user_limit, deployment_limit):
    for user_number in range(3):
        subject = f"subject-{user_number}"
        for attempt in range(user_limit):
            store.reserve(subject, capability)
        before = copy.deepcopy(client.items)
        with pytest.raises(QuotaExceeded):
            store.reserve(subject, capability)
        assert client.items == before
    before = copy.deepcopy(client.items)
    with pytest.raises(QuotaExceeded):
        store.reserve("fresh-subject", capability)
    assert client.items == before
    assert next(item["used"] for key, item in client.items.items() if key.endswith("#deployment")) == deployment_limit


@pytest.mark.parametrize("capability", ["search", "browser"])
def test_chat_limit_is_additional_and_scoped_to_subject(store, client, capability):
    for attempt in range(3):
        store.reserve("subject-one", capability, chat_id="shared-chat")
    before = copy.deepcopy(client.items)
    with pytest.raises(QuotaExceeded):
        store.reserve("subject-one", capability, chat_id="shared-chat")
    assert client.items == before
    store.reserve("subject-two", capability, chat_id="shared-chat")
    store.reserve("subject-one", capability, chat_id="different-chat")
    store.reserve("subject-two", capability)
    assert len(client.calls[-1]["TransactItems"]) == 2


def test_rotating_or_omitting_chat_cannot_bypass_user_cap(store, client):
    for attempt in range(10):
        store.reserve("subject", "search", chat_id=f"chat-{attempt}")
    before = copy.deepcopy(client.items)
    for chat_id in (None, "new-chat"):
        with pytest.raises(QuotaExceeded):
            store.reserve("subject", "search", chat_id=chat_id)
    assert client.items == before
    store.reserve("subject", "browser")
    assert all(item["used"] == 1 for key, item in client.items.items() if "#browser#" in key)


def test_ttl_types_distinct_keys_and_no_plaintext_context(store, client):
    store.reserve("deployment", "search", chat_id="deployment", request_id="private-request", reserve_id="attempt-1")
    request = client.calls[0]
    assert request["ClientRequestToken"] == "attempt-1"
    assert len(request["TransactItems"]) == 3
    expected_expiry = int(datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp())
    for key, item in client.items.items():
        assert key.startswith("quota#2026-09-15#search#")
        assert item == {"used": 1, "expires_at": expected_expiry}
    store.reserve("private-subject", "browser", chat_id="private-chat", request_id="private-request")
    serialized = json.dumps(client.calls)
    for identifier in ("private-subject", "private-chat", "private-request"):
        assert identifier not in serialized
    for key in client.items:
        if not key.endswith("#deployment"):
            assert len(key.rsplit("#", 1)[1]) == 64
    assert not any(word in serialized for word in ("query", "snippet", "url", "tokens"))


def test_framed_hash_avoids_delimiter_collisions(store, client):
    store.reserve("a#b", "search", chat_id="c")
    store.reserve("a", "search", chat_id="b#c")
    assert len([key for key in client.items if "#chat#" in key]) == 2


def test_utc_rollover_ignores_unexpired_previous_day(client):
    current = NOW
    store = QuotaStore(table_name=TABLE, region=REGION, client=client,
                       limits=QuotaLimits(user_search=1), clock=lambda: current)
    store.reserve("subject", "search")
    current = datetime(2026, 9, 16, 1, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(QuotaExceeded):
        store.reserve("subject", "search")
    current += timedelta(hours=1)
    store.reserve("subject", "search")
    assert len(client.items) == 4


def test_stable_token_and_request_id_not_used_as_dedupe_key(store, client):
    store.reserve("subject", "search", reserve_id="fixed-attempt", request_id="same-request")
    store.reserve("subject", "search", reserve_id="fixed-attempt", request_id="same-request")
    assert all(item["used"] == 1 for item in client.items.values())
    assert client.calls[0] == client.calls[1]
    generated = store.reserve("subject", "search", request_id="same-request")
    assert len(generated) == 36
    assert all(item["used"] == 2 for item in client.items.values())
    with pytest.raises(QuotaUnavailable):
        store.reserve("different-subject", "search", reserve_id="fixed-attempt")


def test_reserve_before_paid_call_and_no_refunds(store, client):
    paid_calls = []

    def paid_call():
        assert all(item["used"] == 1 for item in client.items.values())
        paid_calls.append("attempt")
        raise RuntimeError("upstream failed")

    with pytest.raises(RuntimeError, match="upstream failed"):
        store.reserve("subject", "search")
        paid_call()
    assert paid_calls == ["attempt"]
    assert all(item["used"] == 1 for item in client.items.values())
    client.error = aws_error("TransactionCanceledException")
    with pytest.raises(QuotaExceeded):
        store.reserve("subject", "search")
        paid_call()
    assert paid_calls == ["attempt"]


@pytest.mark.parametrize("error,expected", [
    (aws_error("TransactionCanceledException"), QuotaExceeded),
    (aws_error("AccessDeniedException"), QuotaUnavailable),
    (aws_error("ProvisionedThroughputExceededException"), QuotaUnavailable),
    (EndpointConnectionError(endpoint_url="https://dynamodb.invalid"), QuotaUnavailable),
    (ReadTimeoutError(endpoint_url="https://dynamodb.invalid"), QuotaUnavailable),
])
def test_fail_closed_no_retry_or_detail_leak(store, client, error, expected):
    client.error = error
    with pytest.raises(expected) as caught:
        store.reserve("private-subject", "search")
    assert len(client.calls) == 1
    assert client.items == {}
    assert "private" not in str(caught.value)
    assert "dynamodb.invalid" not in str(caught.value)


def test_timeout_after_commit_keeps_attempt_and_does_not_retry(store, client):
    client.commit_then_timeout = True
    with pytest.raises(QuotaUnavailable):
        store.reserve("subject", "search", reserve_id="uncertain-attempt")
    assert len(client.calls) == 1
    assert all(item["used"] == 1 for item in client.items.values())


@pytest.mark.parametrize("updates", [
    {"subject": None}, {"subject": ""}, {"subject": " "}, {"subject": "anonymous"},
    {"subject": "ANON"}, {"subject": "guest"}, {"subject": True}, {"subject": "x" * 257},
    {"subject": "user\n"}, {"capability": "fetch"}, {"capability": []},
    {"chat_id": ""}, {"chat_id": 123}, {"chat_id": "x" * 257},
    {"request_id": False}, {"request_id": "x" * 257}, {"request_id": "bad\x00id"},
    {"reserve_id": ""}, {"reserve_id": "x" * 37}, {"reserve_id": 123}, {"reserve_id": "bad id"},
])
def test_validation_has_no_aws_reads_or_writes(store, client, updates):
    arguments = {"subject": "subject", "capability": "search", **updates}
    with pytest.raises(ValueError):
        store.reserve(**arguments)
    assert client.calls == []
    assert client.items == {}


@pytest.mark.parametrize("arguments", [
    {"table_name": ""}, {"table_name": "ab"}, {"table_name": "x" * 256}, {"table_name": None},
    {"region": ""}, {"region": None}, {"region": "default"},
    {"limits": {}}, {"clock": 1}, {"client": object()},
])
def test_configuration_validation(client, arguments):
    with pytest.raises(ValueError):
        QuotaStore(**{"table_name": TABLE, "region": REGION, "client": client, **arguments})
    assert client.calls == []


@pytest.mark.parametrize("value", [-1, True, 1.5, "10", None, 1_000_000_001])
def test_limits_require_bounded_integers(value):
    with pytest.raises(ValueError):
        QuotaLimits(user_search=value)


@pytest.mark.parametrize("field", ["user_search", "deployment_search", "chat_search"])
def test_zero_limit_denies_even_missing_items(client, field):
    store = QuotaStore(table_name=TABLE, region=REGION, client=client,
                       limits=QuotaLimits(**{field: 0}), clock=lambda: NOW)
    with pytest.raises(QuotaExceeded):
        store.reserve("subject", "search", chat_id="chat")
    assert client.items == {}


@pytest.mark.parametrize("value", [datetime(2026, 9, 15), None, "2026-09-15"])
def test_bad_clock_denies_without_aws(client, value):
    store = QuotaStore(table_name=TABLE, region=REGION, client=client, clock=lambda: value)
    with pytest.raises(QuotaUnavailable):
        store.reserve("subject", "search")
    assert client.calls == []


def test_distributed_stores_cannot_overspend(client):
    stores = [QuotaStore(table_name=TABLE, region=REGION, client=client, clock=lambda: NOW) for index in range(8)]

    def attempt(index):
        try:
            stores[index % len(stores)].reserve("subject", "search")
            return True
        except QuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(40)))
    assert sum(results) == 10
    assert all(item["used"] == 10 for item in client.items.values())


def test_production_factory_explicit_region_no_profile_no_sdk_retries(monkeypatch, client):
    factory_calls = []

    def factory(service, **options):
        factory_calls.append((service, options))
        return client

    monkeypatch.setattr(quota.boto3, "client", factory)
    store = QuotaStore(table_name=TABLE, region=REGION, clock=lambda: NOW)
    with pytest.raises(ValueError):
        store.reserve("", "search")
    assert factory_calls == []
    store.reserve("subject", "search")
    store.reserve("subject", "browser")
    assert len(factory_calls) == 1
    service, options = factory_calls[0]
    assert service == "dynamodb"
    assert set(options) == {"region_name", "config"}
    assert options["region_name"] == REGION
    assert options["config"].retries == {"total_max_attempts": 1, "mode": "standard"}


@pytest.mark.parametrize("region,retries", [(REGION, {}), (REGION, {"total_max_attempts": 2}),
                                            ("us-east-1", {"total_max_attempts": 1})])
def test_injected_sdk_client_must_disable_retries_and_match_region(client, region, retries):
    client.meta = SimpleNamespace(region_name=region, config=SimpleNamespace(retries=retries))
    with pytest.raises(ValueError):
        QuotaStore(table_name=TABLE, region=REGION, client=client)

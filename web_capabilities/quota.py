"""Daily attempt admission for the private IAM Lambda, not billing metering.

Call reserve BEFORE each paid search/browser call, using an authenticated OWUI
subject supplied by the trusted emitter, never a client identity header. Missing
chat context only removes the additional chat cap, not user/deployment caps.
Success consumes one attempt permanently; there is no refund or read-only check.
On ANY failure do not make the paid call or retry the reservation automatically.

The existing DynamoDB table must have a string partition key ``pk`` (no sort key)
and TTL attribute ``expires_at``. Use one table per deployment. Only pseudonymous
keys, integer attempt counts and expiry timestamps are stored. SHA256 is for
pseudonymization, not anonymity; no secret is needed for this budget guardrail.
TTL is UTC bucket start plus eight days; expiry never implements the daily reset.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class QuotaExceeded(RuntimeError):
    """Transaction cancelled: deny admission without exposing AWS details."""


class QuotaUnavailable(RuntimeError):
    """Admission could not be confirmed; the attempt may already be counted."""


@dataclass(frozen=True)
class QuotaLimits:
    user_search: int = 10
    user_browser: int = 5
    deployment_search: int = 30
    deployment_browser: int = 15
    chat_search: int = 3
    chat_browser: int = 3

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 <= value <= 1_000_000_000:
                raise ValueError("Quota limits must be bounded nonnegative integers")


def _identifier(value: str, name: str, maximum: int = 256):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or value != value.strip() or any(not character.isprintable() for character in value)):
        raise ValueError("Invalid " + name)


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class QuotaStore:
    """Atomic admission using an injected low-level boto3 DynamoDB client.

    If omitted, the client uses Lambda's default IAM role credential chain with
    an explicit region and SDK retries disabled. Injected boto3 clients must also
    disable retries and match the region. Local method fakes need no credentials.
    """

    def __init__(self, *, table_name: str, region: str, client=None,
                 limits: QuotaLimits | None = None,
                 clock: Callable[[], datetime] | None = None):
        if not isinstance(table_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name):
            raise ValueError("Invalid quota table")
        if (not isinstance(region, str) or len(region) > 64
                or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region)):
            raise ValueError("Invalid quota region")
        if limits is not None and not isinstance(limits, QuotaLimits):
            raise ValueError("Invalid quota limits")
        if clock is not None and not callable(clock):
            raise ValueError("Invalid quota clock")
        if client is not None:
            if not callable(getattr(client, "transact_write_items", None)):
                raise ValueError("Invalid quota client")
            metadata = getattr(client, "meta", None)
            if metadata is not None:
                retries = metadata.config.retries or {}
                attempts = retries.get("total_max_attempts")
                no_retries = attempts == 1 or (attempts is None and retries.get("max_attempts") == 0)
                if metadata.region_name != region or not no_retries:
                    raise ValueError("Quota client must match region and disable retries")
        self.table_name = table_name
        self.region = region
        self.client = client
        self.limits = limits if limits is not None else QuotaLimits()
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def reserve(self, subject: str, capability: str, *, chat_id: str | None = None,
                request_id: str | None = None, reserve_id: str | None = None) -> str:
        """Consume one attempt; return the transaction token only on success.

        Optional chat/request IDs come from trusted context and are never stored
        plaintext. Request ID is validated context, NOT an idempotency key: one
        request may make multiple paid calls. Use a fresh UUID reserve_id per paid
        attempt, generated upstream if needed (otherwise generated here). It is
        passed unchanged as ClientRequestToken, <=36 characters. DynamoDB dedupes
        identical transactions for only ten minutes, not across UTC days. Never
        reuse a successful reservation to authorize another paid call or retry an
        uncertain transaction. This method and its SDK client perform no retries.
        """
        _identifier(subject, "trusted subject")
        if subject.casefold() in {"anonymous", "anon", "guest"}:
            raise ValueError("Authenticated OWUI subject required")
        if not isinstance(capability, str) or capability not in ("search", "browser"):
            raise ValueError("Invalid quota capability")
        for name, value in (("chat ID", chat_id), ("request ID", request_id)):
            if value is not None:
                _identifier(value, name)
        if reserve_id is not None and (not isinstance(reserve_id, str)
                                      or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", reserve_id)):
            raise ValueError("Invalid reservation ID")
        token = reserve_id if reserve_id is not None else str(uuid.uuid4())
        try:
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("Quota clock must return an aware datetime")
            bucket = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            day = bucket.date().isoformat()
            expires_at = int((bucket + timedelta(days=8)).timestamp())
        except Exception:
            raise QuotaUnavailable("Quota clock unavailable") from None
        counters = [
            ("user#" + _digest(subject), getattr(self.limits, "user_" + capability)),
            ("deployment", getattr(self.limits, "deployment_" + capability)),
        ]
        if chat_id is not None:
            counters.append(("chat#" + _digest(subject, chat_id), getattr(self.limits, "chat_" + capability)))
        transactions = []
        for scope, limit in counters:
            transactions.append({"Update": {
                "TableName": self.table_name,
                "Key": {"pk": {"S": f"quota#{day}#{capability}#{scope}"}},
                "UpdateExpression": "SET #expires = :expires ADD #used :one",
                "ConditionExpression": "(attribute_not_exists(#used) OR #used >= :zero) AND "
                                       "(attribute_not_exists(#used) OR #used < :limit) AND :limit > :zero",
                "ExpressionAttributeNames": {"#used": "used", "#expires": "expires_at"},
                "ExpressionAttributeValues": {
                    ":one": {"N": "1"}, ":zero": {"N": "0"},
                    ":limit": {"N": str(limit)}, ":expires": {"N": str(expires_at)},
                },
            }})
        try:
            if self.client is None:
                self.client = boto3.client(
                    "dynamodb", region_name=self.region,
                    config=Config(retries={"total_max_attempts": 1, "mode": "standard"},
                                  connect_timeout=2, read_timeout=3),
                )
            self.client.transact_write_items(TransactItems=transactions, ClientRequestToken=token)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise QuotaExceeded("Daily web capability quota exceeded") from None
            raise QuotaUnavailable("Quota admission unavailable") from None
        except Exception:
            raise QuotaUnavailable("Quota admission unavailable") from None
        return token

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Durable job state, replayable event journal, and artifact storage.

DynamoDB tables (created by infra/lib/aiq-stack.ts):

* jobs table  — pk = ``TENANT#<tenant_key>``, sk = ``JOB#<job_id>`` (JobRecord),
                plus ``IDEMP#<client_request_id>`` items mapping to a job_id.
* events table — pk = ``job_id``, sk = ``seq`` (Number). Append uses a
                 conditional PutItem so a seq is never overwritten; the writer
                 allocates seq by incrementing ``last_seq`` on the job item.

S3 (private bucket): ``tenants/<tenant_key>/jobs/<job_id>/report.md``,
``.../ledger.json``, ``documents/<tenant_key>/<collection>/<name>`` (+ ``.metadata.json``).

Everything is scoped by the verified ``tenant_key``; there is no API that reads
another tenant's partition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, AsyncIterator

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .contracts import Event, EventType, JobRecord, JobStatus, ResearchMode, new_job_id

log = logging.getLogger(__name__)

_BOTO_CFG = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=5, read_timeout=20)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clean(obj: Any) -> Any:
    """Convert Decimals from DynamoDB into ints/floats for pydantic."""
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def _ddb_safe(obj: Any) -> Any:
    """DynamoDB rejects floats and empty strings in some positions; normalise."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, list):
        return [_ddb_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _ddb_safe(v) for k, v in obj.items() if v is not None}
    return obj


class JobStore:
    def __init__(self, jobs_table: str | None = None, events_table: str | None = None, bucket: str | None = None,
                 region: str | None = None, ttl_days: int = 30):
        region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._ddb = boto3.resource("dynamodb", region_name=region, config=_BOTO_CFG)
        self._s3 = boto3.client("s3", region_name=region, config=_BOTO_CFG)
        self.jobs = self._ddb.Table(jobs_table or os.environ["AIQ_JOBS_TABLE"])
        self.events = self._ddb.Table(events_table or os.environ["AIQ_EVENTS_TABLE"])
        self.bucket = bucket or os.environ["AIQ_ARTIFACTS_BUCKET"]
        self.ttl_seconds = ttl_days * 86400

    # ----------------------------------------------------------------- jobs --
    @staticmethod
    def _pk(tenant_key: str) -> str:
        return f"TENANT#{tenant_key}"

    def create_job(self, *, tenant_key: str, mode: ResearchMode, question: str, runtime_session_id: str | None,
                   client_request_id: str | None, conversation_id: str | None) -> tuple[JobRecord, bool]:
        """Create a job; returns (record, created). Duplicate client_request_id → existing job."""
        pk = self._pk(tenant_key)
        if client_request_id:
            existing = self.jobs.get_item(Key={"pk": pk, "sk": f"IDEMP#{client_request_id}"}).get("Item")
            if existing:
                rec = self.get_job(tenant_key, existing["job_id"])
                if rec:
                    return rec, False
        job_id = new_job_id(f"{tenant_key}|{client_request_id}" if client_request_id else None)
        ts = now_iso()
        rec = JobRecord(job_id=job_id, tenant_key=tenant_key, mode=mode, status=JobStatus.QUEUED, question=question,
                        created_at=ts, updated_at=ts, runtime_session_id=runtime_session_id,
                        client_request_id=client_request_id, conversation_id=conversation_id,
                        expires_at=int(time.time()) + self.ttl_seconds)
        item = {"pk": pk, "sk": f"JOB#{job_id}", **_ddb_safe(rec.model_dump(mode="json"))}
        try:
            self.jobs.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                found = self.get_job(tenant_key, job_id)
                if found:
                    return found, False
            raise
        if client_request_id:
            self.jobs.put_item(Item={"pk": pk, "sk": f"IDEMP#{client_request_id}", "job_id": job_id,
                                     "expires_at": item["expires_at"]})
        return rec, True

    def get_job(self, tenant_key: str, job_id: str) -> JobRecord | None:
        item = self.jobs.get_item(Key={"pk": self._pk(tenant_key), "sk": f"JOB#{job_id}"}, ConsistentRead=True).get("Item")
        if not item:
            return None
        item = _clean(item)
        item.pop("pk", None)
        item.pop("sk", None)
        return JobRecord.model_validate(item)

    def update_job(self, tenant_key: str, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        names = {f"#{i}": k for i, k in enumerate(fields)}
        values = {f":{i}": _ddb_safe(v) for i, v in enumerate(fields.values())}
        expr = ", ".join(f"#{i} = :{i}" for i in range(len(fields)))
        self.jobs.update_item(Key={"pk": self._pk(tenant_key), "sk": f"JOB#{job_id}"},
                              UpdateExpression=f"SET {expr}", ExpressionAttributeNames=names,
                              ExpressionAttributeValues=values,
                              ConditionExpression="attribute_exists(pk)")

    def request_cancel(self, tenant_key: str, job_id: str) -> JobRecord | None:
        rec = self.get_job(tenant_key, job_id)
        if not rec:
            return None
        if rec.status.terminal:
            return rec
        self.update_job(tenant_key, job_id, cancel_requested=True, status=JobStatus.CANCELLING.value)
        return self.get_job(tenant_key, job_id)

    def cancel_requested(self, tenant_key: str, job_id: str) -> bool:
        item = self.jobs.get_item(Key={"pk": self._pk(tenant_key), "sk": f"JOB#{job_id}"},
                                  ProjectionExpression="cancel_requested", ConsistentRead=True).get("Item") or {}
        return bool(item.get("cancel_requested"))

    def heartbeat(self, tenant_key: str, job_id: str) -> None:
        self.update_job(tenant_key, job_id, heartbeat_at=now_iso())

    def list_jobs(self, tenant_key: str, limit: int = 20) -> list[JobRecord]:
        from boto3.dynamodb.conditions import Key

        resp = self.jobs.query(KeyConditionExpression=Key("pk").eq(self._pk(tenant_key)) & Key("sk").begins_with("JOB#"),
                               ScanIndexForward=False, Limit=limit)
        out = []
        for item in resp.get("Items", []):
            item = _clean(item)
            item.pop("pk", None)
            item.pop("sk", None)
            out.append(JobRecord.model_validate(item))
        return out

    # --------------------------------------------------------------- events --
    def append_event(self, tenant_key: str, job_id: str, etype: EventType, data: dict[str, Any] | None = None,
                     idempotency_key: str | None = None) -> Event:
        """Allocate the next seq on the job item, then write the event with a conditional put."""
        resp = self.jobs.update_item(
            Key={"pk": self._pk(tenant_key), "sk": f"JOB#{job_id}"},
            UpdateExpression="SET last_seq = if_not_exists(last_seq, :zero) + :one, updated_at = :ts",
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ts": now_iso()},
            ConditionExpression="attribute_exists(pk)",
            ReturnValues="UPDATED_NEW",
        )
        seq = int(resp["Attributes"]["last_seq"])
        ev = Event(job_id=job_id, seq=seq, ts=now_iso(), type=etype, data=data or {}, idempotency_key=idempotency_key)
        item = _ddb_safe(ev.model_dump(mode="json"))
        item["expires_at"] = int(time.time()) + self.ttl_seconds
        self.events.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        return ev

    def read_events(self, job_id: str, after: int = 0, limit: int = 500) -> list[Event]:
        from boto3.dynamodb.conditions import Key

        resp = self.events.query(KeyConditionExpression=Key("job_id").eq(job_id) & Key("seq").gt(after), Limit=limit,
                                 ConsistentRead=True)
        out = []
        for item in resp.get("Items", []):
            item = _clean(item)
            item.pop("expires_at", None)
            out.append(Event.model_validate(item))
        return out

    async def tail_events(self, tenant_key: str, job_id: str, after: int = 0, *, poll_seconds: float = 1.0,
                          idle_timeout: float = 3600.0) -> AsyncIterator[Event]:
        """Replay events with seq > after, then keep polling until a terminal event.

        Reconnect-safe: callers pass the last seq they processed. No side effects.
        """
        cursor = after
        deadline = time.monotonic() + idle_timeout
        loop = asyncio.get_running_loop()
        while True:
            batch = await loop.run_in_executor(None, self.read_events, job_id, cursor)
            for ev in batch:
                cursor = ev.seq
                yield ev
                if ev.type in (EventType.COMPLETED, EventType.CANCELLED) or (
                    ev.type == EventType.ERROR and ev.data.get("terminal")
                ):
                    return
            if batch:
                deadline = time.monotonic() + idle_timeout
                continue
            rec = await loop.run_in_executor(None, self.get_job, tenant_key, job_id)
            if rec is None:
                return
            if rec.status.terminal and rec.last_seq <= cursor:
                return
            if time.monotonic() > deadline:
                return
            await asyncio.sleep(poll_seconds)

    # ------------------------------------------------------------------ s3 --
    def report_key(self, tenant_key: str, job_id: str, name: str = "report.md") -> str:
        return f"tenants/{tenant_key}/jobs/{job_id}/{name}"

    def put_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> str:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"), ContentType=content_type)
        return key

    def put_json(self, key: str, obj: Any) -> str:
        return self.put_text(key, json.dumps(obj, ensure_ascii=False, indent=2), "application/json")

    def get_text(self, key: str) -> str:
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")

    def document_prefix(self, tenant_key: str, collection: str) -> str:
        return f"documents/{tenant_key}/{collection}/"

    def put_document(self, tenant_key: str, collection: str, name: str, body: bytes, content_type: str,
                     extra_metadata: dict[str, Any] | None = None) -> str:
        key = self.document_prefix(tenant_key, collection) + name
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        # Bedrock Knowledge Base sidecar: <object key>.metadata.json with filterable attributes.
        meta = {"metadataAttributes": {"tenant_key": tenant_key, "collection": collection,
                                       "document_name": name, **(extra_metadata or {})}}
        self._s3.put_object(Bucket=self.bucket, Key=key + ".metadata.json", Body=json.dumps(meta).encode(),
                            ContentType="application/json")
        return key

    def list_documents(self, tenant_key: str, collection: str | None = None) -> list[dict[str, Any]]:
        prefix = f"documents/{tenant_key}/" + (f"{collection}/" if collection else "")
        out: list[dict[str, Any]] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".metadata.json"):
                    continue
                rel = obj["Key"][len(f"documents/{tenant_key}/"):]
                coll, _, name = rel.partition("/")
                out.append({"collection": coll, "name": name, "size": obj["Size"],
                            "last_modified": obj["LastModified"].isoformat()})
        return out

    def delete_collection(self, tenant_key: str, collection: str) -> int:
        prefix = self.document_prefix(tenant_key, collection)
        keys = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend({"Key": o["Key"]} for o in page.get("Contents", []))
        for i in range(0, len(keys), 1000):
            self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
        return len(keys)

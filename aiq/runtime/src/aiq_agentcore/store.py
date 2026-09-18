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

Phase 3 (11-architecture.md §4): the same table also holds the **library index** —
``PKG#<created_at>#<job_id>`` rows (time-ordered projection of the Research Package, created at
submit so running jobs are visible), ``PREF#models`` (per-tenant model defaults), ``TOMB#<job_id>``
(delete tombstones, TTL 90 d) and ``EVAL#<created_at>#<eval_id>`` (evaluation runs). The S3 package
prefix is ``packages/<tenant_key>/<job_id>/`` (manifest.json, report.md, ledger.json, sources.json,
artifacts/, exports/) — outside ``tenants/`` so the 30-day lifecycle rule never expires a package. Reads are
always keyed by the verified tenant.
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
    def __init__(
        self,
        jobs_table: str | None = None,
        events_table: str | None = None,
        bucket: str | None = None,
        region: str | None = None,
        ttl_days: int = 30,
    ):
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

    def create_job(
        self,
        *,
        tenant_key: str,
        mode: ResearchMode,
        question: str,
        runtime_session_id: str | None,
        client_request_id: str | None,
        conversation_id: str | None,
        models: dict[str, Any] | None = None,
        parent_job_id: str | None = None,
        relation: str | None = None,
        eval_id: str | None = None,
        index: bool = True,
    ) -> tuple[JobRecord, bool]:
        """Create a job; returns (record, created). Duplicate client_request_id → existing job.

        When ``index`` is true a ``PKG#`` library row is written in the same call so the package is listable while
        it runs (deep/auto jobs); short meta turns can opt out."""
        pk = self._pk(tenant_key)
        if client_request_id:
            existing = self.jobs.get_item(Key={"pk": pk, "sk": f"IDEMP#{client_request_id}"}).get("Item")
            if existing:
                rec = self.get_job(tenant_key, existing["job_id"])
                if rec:
                    return rec, False
        job_id = new_job_id(f"{tenant_key}|{client_request_id}" if client_request_id else None)
        ts = now_iso()
        rec = JobRecord(
            job_id=job_id,
            tenant_key=tenant_key,
            mode=mode,
            status=JobStatus.QUEUED,
            question=question,
            created_at=ts,
            updated_at=ts,
            runtime_session_id=runtime_session_id,
            client_request_id=client_request_id,
            conversation_id=conversation_id,
            expires_at=int(time.time()) + self.ttl_seconds,
            models=models,
            parent_job_id=parent_job_id,
            relation=relation or ("root" if index else None),
            eval_id=eval_id,
            package_sk=f"PKG#{ts}#{job_id}" if index else None,
        )
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
            self.jobs.put_item(
                Item={"pk": pk, "sk": f"IDEMP#{client_request_id}", "job_id": job_id, "expires_at": item["expires_at"]}
            )
        if index:
            self.put_package_index(rec, {})
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
        self.jobs.update_item(
            Key={"pk": self._pk(tenant_key), "sk": f"JOB#{job_id}"},
            UpdateExpression=f"SET {expr}",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",
        )

    def request_cancel(self, tenant_key: str, job_id: str) -> JobRecord | None:
        rec = self.get_job(tenant_key, job_id)
        if not rec:
            return None
        if rec.status.terminal:
            return rec
        self.update_job(tenant_key, job_id, cancel_requested=True, status=JobStatus.CANCELLING.value)
        return self.get_job(tenant_key, job_id)

    def cancel_requested(self, tenant_key: str, job_id: str) -> bool:
        item = (
            self.jobs.get_item(
                Key={"pk": self._pk(tenant_key), "sk": f"JOB#{job_id}"},
                ProjectionExpression="cancel_requested",
                ConsistentRead=True,
            ).get("Item")
            or {}
        )
        return bool(item.get("cancel_requested"))

    def heartbeat(self, tenant_key: str, job_id: str) -> None:
        self.update_job(tenant_key, job_id, heartbeat_at=now_iso())

    def list_jobs(self, tenant_key: str, limit: int = 20) -> list[JobRecord]:
        from boto3.dynamodb.conditions import Key

        resp = self.jobs.query(
            KeyConditionExpression=Key("pk").eq(self._pk(tenant_key)) & Key("sk").begins_with("JOB#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        out = []
        for item in resp.get("Items", []):
            item = _clean(item)
            item.pop("pk", None)
            item.pop("sk", None)
            out.append(JobRecord.model_validate(item))
        return out

    # --------------------------------------------------------------- events --
    def append_event(
        self,
        tenant_key: str,
        job_id: str,
        etype: EventType,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
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

        resp = self.events.query(
            KeyConditionExpression=Key("job_id").eq(job_id) & Key("seq").gt(after), Limit=limit, ConsistentRead=True
        )
        out = []
        for item in resp.get("Items", []):
            item = _clean(item)
            item.pop("expires_at", None)
            out.append(Event.model_validate(item))
        return out

    async def tail_events(
        self, tenant_key: str, job_id: str, after: int = 0, *, poll_seconds: float = 1.0, idle_timeout: float = 3600.0
    ) -> AsyncIterator[Event]:
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
            # Stop tailing once everything is read and the job is terminal OR paused for the user (clarifying):
            # the next user turn resumes it and the client tails again from its cursor.
            if (rec.status.terminal or rec.status == JobStatus.CLARIFYING) and rec.last_seq <= cursor:
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

    def put_document(
        self,
        tenant_key: str,
        collection: str,
        name: str,
        body: bytes,
        content_type: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> str:
        key = self.document_prefix(tenant_key, collection) + name
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        # Bedrock Knowledge Base sidecar: <object key>.metadata.json with filterable attributes.
        meta = {
            "metadataAttributes": {
                "tenant_key": tenant_key,
                "collection": collection,
                "document_name": name,
                **(extra_metadata or {}),
            }
        }
        self._s3.put_object(
            Bucket=self.bucket, Key=key + ".metadata.json", Body=json.dumps(meta).encode(), ContentType="application/json"
        )
        return key

    def list_documents(self, tenant_key: str, collection: str | None = None) -> list[dict[str, Any]]:
        prefix = f"documents/{tenant_key}/" + (f"{collection}/" if collection else "")
        out: list[dict[str, Any]] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".metadata.json"):
                    continue
                rel = obj["Key"][len(f"documents/{tenant_key}/") :]
                coll, _, name = rel.partition("/")
                out.append(
                    {"collection": coll, "name": name, "size": obj["Size"], "last_modified": obj["LastModified"].isoformat()}
                )
        return out

    def delete_collection(self, tenant_key: str, collection: str) -> int:
        prefix = self.document_prefix(tenant_key, collection)
        keys = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend({"Key": o["Key"]} for o in page.get("Contents", []))
        for i in range(0, len(keys), 1000):
            self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i : i + 1000], "Quiet": True})
        return len(keys)

    # ------------------------------------------------------------ phase 3: library index, prefs, tombstones --
    @staticmethod
    def package_sk(created_at: str, job_id: str) -> str:
        return f"PKG#{created_at}#{job_id}"

    def package_prefix(self, tenant_key: str, job_id: str) -> str:
        # NOT under tenants/ (30-day lifecycle rule applies there); packages are kept until the researcher deletes them
        return f"packages/{tenant_key}/{job_id}/"

    def put_package_index(self, rec: JobRecord, summary: dict[str, Any]) -> dict[str, Any]:
        """Write/refresh the ``PKG#`` projection row for a job (≤ 4 KB: title, question excerpt, status, models, counts…)."""
        sk = rec.package_sk or self.package_sk(rec.created_at, rec.job_id)
        row = {
            "pk": self._pk(rec.tenant_key),
            "sk": sk,
            "job_id": rec.job_id,
            "created_at": rec.created_at,
            "updated_at": now_iso(),
            "status": rec.status.value,
            "mode": rec.mode.value,
            "question": (rec.question or "")[:300],
            "title": (rec.title or summary.get("title") or "")[:200] or None,
            "models": rec.models or summary.get("models"),
            "cost_usd": rec.cost_usd if rec.cost_usd is not None else summary.get("cost_usd"),
            "counts": summary.get("counts") or {},
            "tags": summary.get("tags") or [],
            "pinned": bool(summary.get("pinned", False)),
            "lineage": {
                "parent_package_id": rec.parent_job_id,
                "relation": rec.relation or "root",
                "root_package_id": summary.get("root_package_id") or rec.parent_job_id or rec.job_id,
            },
            "conversation_id": rec.conversation_id,
            "depth": summary.get("depth"),
            "completed_at": summary.get("completed_at"),
            "manifest_key": summary.get("manifest_key"),
            "report_key": rec.report_key,
            "eval_id": rec.eval_id,
            "error": (rec.error or "")[:300] or None,
        }
        existing = self.get_package_index(rec.tenant_key, sk)
        if existing:  # organisation fields are owned by the user, never overwritten by a status refresh
            for k in ("tags", "pinned", "title"):
                if k not in summary and existing.get(k) not in (None, [], False):
                    row[k] = existing[k]
        self.jobs.put_item(Item=_ddb_safe(row))
        return _clean(row)

    def get_package_index(self, tenant_key: str, sk: str) -> dict[str, Any] | None:
        item = self.jobs.get_item(Key={"pk": self._pk(tenant_key), "sk": sk}, ConsistentRead=True).get("Item")
        return _clean(item) if item else None

    def index_for_job(self, tenant_key: str, job_id: str) -> dict[str, Any] | None:
        rec = self.get_job(tenant_key, job_id)
        if not rec:
            return None
        sk = rec.package_sk or self.package_sk(rec.created_at, rec.job_id)
        return self.get_package_index(tenant_key, sk)

    def update_package_index(self, tenant_key: str, job_id: str, **fields: Any) -> dict[str, Any] | None:
        """User-owned organisation fields (tags, pinned, title, notes) and status refreshes."""
        rec = self.get_job(tenant_key, job_id)
        if not rec:
            return None
        sk = rec.package_sk or self.package_sk(rec.created_at, rec.job_id)
        fields["updated_at"] = now_iso()
        names = {f"#{i}": k for i, k in enumerate(fields)}
        values = {f":{i}": _ddb_safe(v) for i, v in enumerate(fields.values())}
        expr = ", ".join(f"#{i} = :{i}" for i in range(len(fields)))
        self.jobs.update_item(
            Key={"pk": self._pk(tenant_key), "sk": sk},
            UpdateExpression=f"SET {expr}",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",
        )
        if "title" in fields:
            self.update_job(tenant_key, job_id, title=fields["title"])
        return self.get_package_index(tenant_key, sk)

    def list_packages(
        self, tenant_key: str, *, limit: int = 50, cursor: str | None = None, newest_first: bool = True
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Time-ordered page of PKG# rows. ``cursor`` is the last sk seen (opaque to callers)."""
        from boto3.dynamodb.conditions import Key

        cond = Key("pk").eq(self._pk(tenant_key)) & Key("sk").begins_with("PKG#")
        kw: dict[str, Any] = {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first, "Limit": limit}
        if cursor:
            kw["ExclusiveStartKey"] = {"pk": self._pk(tenant_key), "sk": cursor}
        resp = self.jobs.query(**kw)
        items = [_clean(i) for i in resp.get("Items", [])]
        lek = resp.get("LastEvaluatedKey")
        return items, (lek["sk"] if lek else None)

    def delete_package(self, tenant_key: str, job_id: str) -> bool:
        """Delete the library row + job record and leave a tombstone; S3 objects are deleted by the caller."""
        rec = self.get_job(tenant_key, job_id)
        if not rec:
            return False
        sk = rec.package_sk or self.package_sk(rec.created_at, rec.job_id)
        pk = self._pk(tenant_key)
        # the resource's client applies the document transformation, so plain Python values are expected here
        self._ddb.meta.client.transact_write_items(
            TransactItems=[
                {"Delete": {"TableName": self.jobs.name, "Key": {"pk": pk, "sk": sk}}},
                {"Delete": {"TableName": self.jobs.name, "Key": {"pk": pk, "sk": f"JOB#{job_id}"}}},
                {
                    "Put": {
                        "TableName": self.jobs.name,
                        "Item": {
                            "pk": pk,
                            "sk": f"TOMB#{job_id}",
                            "deleted_at": now_iso(),
                            "expires_at": int(time.time()) + 90 * 86400,
                        },
                    }
                },
            ]
        )
        return True

    def is_tombstoned(self, tenant_key: str, job_id: str) -> bool:
        return bool(self.jobs.get_item(Key={"pk": self._pk(tenant_key), "sk": f"TOMB#{job_id}"}).get("Item"))

    def get_prefs(self, tenant_key: str) -> dict[str, Any] | None:
        item = self.jobs.get_item(Key={"pk": self._pk(tenant_key), "sk": "PREF#models"}).get("Item")
        return _clean(item).get("models") if item else None

    def put_prefs(self, tenant_key: str, models: dict[str, Any]) -> None:
        self.jobs.put_item(
            Item=_ddb_safe({"pk": self._pk(tenant_key), "sk": "PREF#models", "models": models, "updated_at": now_iso()})
        )

    def put_eval(self, tenant_key: str, eval_id: str, record: dict[str, Any]) -> None:
        self.jobs.put_item(
            Item=_ddb_safe(
                {
                    "pk": self._pk(tenant_key),
                    "sk": f"EVAL#{record.get('created_at', now_iso())}#{eval_id}",
                    "eval_id": eval_id,
                    **record,
                }
            )
        )

    def list_evals(self, tenant_key: str, limit: int = 20) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        resp = self.jobs.query(
            KeyConditionExpression=Key("pk").eq(self._pk(tenant_key)) & Key("sk").begins_with("EVAL#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [_clean(i) for i in resp.get("Items", [])]

    # --------------------------------------------------------------------------------- phase 3: S3 helpers --
    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get_bytes(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def head(self, key: str) -> dict[str, Any] | None:
        try:
            return self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def list_keys(self, prefix: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(
                {"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].isoformat()}
                for o in page.get("Contents", [])
            )
        return out

    def delete_prefix(self, prefix: str) -> int:
        keys = [{"Key": o["key"]} for o in self.list_keys(prefix)]
        for i in range(0, len(keys), 1000):
            self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i : i + 1000], "Quiet": True})
        return len(keys)

    def copy(self, src_key: str, dst_key: str) -> None:
        self._s3.copy_object(Bucket=self.bucket, CopySource={"Bucket": self.bucket, "Key": src_key}, Key=dst_key)

    def presign(self, key: str, *, filename: str, content_type: str, ttl: int = 600) -> str:
        """Short-lived, authenticated download link (11-architecture §7). Only called after the tenant check."""
        return self._s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
                "ResponseContentType": content_type,
            },
            ExpiresIn=ttl,
        )

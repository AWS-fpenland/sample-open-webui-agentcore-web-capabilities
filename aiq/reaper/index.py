# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Stale-job reaper (AWS Lambda, scheduled). A job runs as an in-session async task on one AgentCore
Runtime microVM; the adapter refreshes ``heartbeat_at`` every 30 s. If the microVM dies, the job would
stay ``running`` forever. This function marks jobs whose heartbeat is older than STALE_AFTER_SECONDS as
``failed`` and appends a terminal ``error`` event so tailing clients finish. Idempotent; scans only
non-terminal statuses.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime

import boto3

STALE_AFTER = int(os.environ.get("STALE_AFTER_SECONDS", "600"))
JOBS = os.environ["JOBS_TABLE"]
EVENTS = os.environ["EVENTS_TABLE"]
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", str(30 * 86400)))
ACTIVE = {"queued", "running", "cancelling"}

ddb = boto3.resource("dynamodb")
jobs = ddb.Table(JOBS)
events = ddb.Table(EVENTS)


def _parse(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        try:
            return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            return None


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def reap(now: float | None = None) -> dict:
    now = now or time.time()
    reaped, scanned = [], 0
    kwargs = {"FilterExpression": "begins_with(sk, :j) AND #s IN (:q, :r, :c)",
              "ExpressionAttributeNames": {"#s": "status"},
              "ExpressionAttributeValues": {":j": "JOB#", ":q": "queued", ":r": "running", ":c": "cancelling"}}
    while True:
        page = jobs.scan(**kwargs)
        for item in page.get("Items", []):
            scanned += 1
            last = _parse(item.get("heartbeat_at")) or _parse(item.get("updated_at")) or _parse(item.get("created_at"))
            if last is None or now - last < STALE_AFTER:
                continue
            pk, sk, job_id = item["pk"], item["sk"], item["job_id"]
            try:
                resp = jobs.update_item(
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression="SET #s = :f, #e = :msg, updated_at = :ts, last_seq = if_not_exists(last_seq, :zero) + :one",
                    ConditionExpression="#s IN (:q, :r, :c)",
                    ExpressionAttributeNames={"#s": "status", "#e": "error"},
                    ExpressionAttributeValues={":f": "failed", ":msg": f"stale: no heartbeat for {int(now - last)}s (runtime session lost)",
                                               ":ts": now_iso(), ":zero": 0, ":one": 1, ":q": "queued", ":r": "running",
                                               ":c": "cancelling"},
                    ReturnValues="UPDATED_NEW",
                )
            except ddb.meta.client.exceptions.ConditionalCheckFailedException:
                continue  # finished in the meantime
            seq = int(resp["Attributes"]["last_seq"])
            events.put_item(Item={"job_id": job_id, "seq": seq, "ts": now_iso(), "type": "error",
                                  "data": {"error": {"code": "stale", "message": "The research job lost its runtime session "
                                                                                  "and was marked failed; you can retry.",
                                                     "retryable": True}, "terminal": True, "reaper": True},
                                  "expires_at": int(now) + TTL_SECONDS},
                            ConditionExpression="attribute_not_exists(job_id)")
            reaped.append(job_id)
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    result = {"scanned_active": scanned, "reaped": reaped, "stale_after_seconds": STALE_AFTER}
    print(json.dumps({"event": "reaper.run", **result}))
    return result


def handler(event, context):  # noqa: ARG001
    return reap()

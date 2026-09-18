# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import importlib
import os
import sys
import time

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(__file__)


@pytest.fixture
def reaper():
    os.environ.update({"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "JOBS_TABLE": "jobs", "EVENTS_TABLE": "events", "STALE_AFTER_SECONDS": "600"})
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(TableName="jobs", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])  # noqa: E501
        ddb.create_table(TableName="events", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}, {"AttributeName": "seq", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}, {"AttributeName": "seq", "AttributeType": "N"}])  # noqa: E501
        sys.path.insert(0, os.path.join(HERE, ".."))
        if "index" in sys.modules:
            del sys.modules["index"]
        mod = importlib.import_module("index")
        yield mod, boto3.resource("dynamodb", region_name="us-east-1")


def _job(tbl, job_id, status, heartbeat_iso, last_seq=5):
    tbl.put_item(Item={"pk": "TENANT#u_x", "sk": f"JOB#{job_id}", "job_id": job_id, "status": status,
                       "heartbeat_at": heartbeat_iso, "last_seq": last_seq})


def test_reaps_only_stale_active_jobs(reaper):
    mod, ddb = reaper
    jobs, events = ddb.Table("jobs"), ddb.Table("events")
    old = "2026-01-01T00:00:00.000Z"
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    _job(jobs, "job_" + "a" * 32, "running", old)
    _job(jobs, "job_" + "b" * 32, "running", fresh)
    _job(jobs, "job_" + "c" * 32, "completed", old)
    _job(jobs, "job_" + "d" * 32, "cancelling", old, last_seq=9)
    out = mod.reap()
    assert sorted(out["reaped"]) == sorted(["job_" + "a" * 32, "job_" + "d" * 32])
    a = jobs.get_item(Key={"pk": "TENANT#u_x", "sk": "JOB#job_" + "a" * 32})["Item"]
    assert a["status"] == "failed" and "stale" in a["error"] and int(a["last_seq"]) == 6
    ev = events.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("job_id").eq("job_" + "d" * 32))["Items"]
    assert len(ev) == 1 and int(ev[0]["seq"]) == 10 and ev[0]["type"] == "error" and ev[0]["data"]["terminal"] is True
    # idempotent
    assert mod.reap()["reaped"] == []

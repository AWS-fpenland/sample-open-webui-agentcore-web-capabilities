# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AIQ_REGION", "us-east-1")
os.environ.setdefault("AIQ_JOBS_TABLE", "aiq-test-jobs")
os.environ.setdefault("AIQ_EVENTS_TABLE", "aiq-test-events")
os.environ.setdefault("AIQ_ARTIFACTS_BUCKET", "aiq-test-artifacts")
os.environ.setdefault("AIQ_JWT_ISSUER", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL")
os.environ.setdefault("AIQ_JWT_ALLOWED_CLIENTS", "client-a,client-b")
os.environ.setdefault("AIQ_ENGINE", "mock")
os.environ.setdefault("AIQ_KB_ID", "KBTEST")
os.environ.setdefault("AIQ_KB_DATA_SOURCE_ID", "DSTEST")
os.environ.setdefault("AIQ_GATEWAY_URL", "https://gw.example.invalid/mcp")


@pytest.fixture
def aws_tables():
    """Moto-backed DynamoDB tables + S3 bucket mirroring infra/lib/aiq-stack.ts."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(TableName=os.environ["AIQ_JOBS_TABLE"], BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        ddb.create_table(TableName=os.environ["AIQ_EVENTS_TABLE"], BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}, {"AttributeName": "seq", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"},
                                               {"AttributeName": "seq", "AttributeType": "N"}])
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=os.environ["AIQ_ARTIFACTS_BUCKET"])
        yield


@pytest.fixture
def store(aws_tables):
    from aiq_agentcore.store import JobStore

    return JobStore()


@pytest.fixture
def principal():
    from aiq_agentcore.contracts import Principal

    return Principal(sub="11111111-2222-3333-4444-555555555555", issuer=os.environ["AIQ_JWT_ISSUER"],
                     client_id="client-a", username="user-a", token_expires_at=4_000_000_000)


@pytest.fixture
def other_principal():
    from aiq_agentcore.contracts import Principal

    return Principal(sub="99999999-2222-3333-4444-555555555555", issuer=os.environ["AIQ_JWT_ISSUER"],
                     client_id="client-a", username="user-b", token_expires_at=4_000_000_000)

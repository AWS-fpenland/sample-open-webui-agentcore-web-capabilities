#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Operator diagnostic: exercise the AI-Q AgentCore runtime end to end with a real Cognito user.

Mints a Cognito access token headlessly (SRP, same app client Open WebUI uses — the runtime's
JWT authorizer trusts that client), then drives the runtime's operations over raw HTTPS with
``Authorization: Bearer`` (JWT-authorized runtimes cannot be called with the SigV4 SDK).

Usage (credentials for the deployment account in the environment):
  uv run --no-project --with pycognito --with boto3 --with httpx python aiq/scripts/smoke.py \
     --run-id fable51-79d40d45 --user-pool us-east-1_XXXX --client-id <owui app client> \
     --username <user> --password-env SMOKE_PASSWORD  health chat:shallow "What is Amazon S3 Vectors?"

Operations: health | chat:<mode> <question> | submit:<mode> <question> | events <job_id> [after] | status <job_id>
            | cancel <job_id> | approve <job_id> <answer|approve|reject> | collections | ingest <collection> <file>...
            | raw '<json op payload>'  (phase 3: packages.list/get/update/delete/compare/rerun, export, artifact.url,
              models, models.prefs, models.validate, eval, eval.list)
Never prints tokens or passwords. Prints event lines as received (JSON), then a summary.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from urllib.parse import quote

import boto3
import httpx


def mint_token(pool: str, client_id: str, username: str, password: str, region: str) -> str:
    from pycognito import Cognito

    secret = boto3.client("cognito-idp", region_name=region).describe_user_pool_client(
        UserPoolId=pool, ClientId=client_id)["UserPoolClient"].get("ClientSecret")
    u = Cognito(pool, client_id, client_secret=secret, username=username, user_pool_region=region)
    u.authenticate(password=password)
    return u.access_token


def runtime_arn(run_id: str, region: str) -> str:
    cfn = boto3.client("cloudformation", region_name=region)
    outs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=f"aiq-{run_id}")["Stacks"][0]["Outputs"]}
    return outs["RuntimeArn"]


FULL = os.environ.get("SMOKE_FULL") == "1"  # print whole event lines (phase-3 responses can exceed 600 chars)


def invoke(arn: str, region: str, token: str, session_id: str, payload: dict, timeout: float = 900.0):
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "text/event-stream, application/json", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id}
    t0 = time.monotonic()
    first = None
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=20.0)) as client, client.stream("POST", url, headers=headers,
                                                                                          json=payload) as resp:
        print(f"# HTTP {resp.status_code} request-id={resp.headers.get('x-amzn-requestid')} trace={resp.headers.get('x-amzn-trace-id')}",
              file=sys.stderr)
        if resp.status_code != 200:
            print(resp.read().decode()[:800], file=sys.stderr)
            return []
        events = []
        for raw in resp.iter_lines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            try:
                obj = json.loads(line)
                if isinstance(obj, str):
                    obj = json.loads(obj)
            except json.JSONDecodeError:
                continue
            if first is None:
                first = time.monotonic() - t0
            events.append(obj)
            line_out = json.dumps(obj, ensure_ascii=False)
            print(line_out if FULL else line_out[:600])
    print(f"# ttfb={first and round(first, 2)}s total={round(time.monotonic() - t0, 2)}s events={len(events)}", file=sys.stderr)
    return events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--user-pool", required=True)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-env", default="SMOKE_PASSWORD")
    ap.add_argument("--session", default=None, help="runtime session id (>=33 chars); default: random per run")
    ap.add_argument("--token-file", default=None, help="reuse a previously minted token (path); never committed")
    ap.add_argument("--collection", default=None, help="document collection to include for chat/submit")
    ap.add_argument("op")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    if a.token_file and os.path.exists(a.token_file):
        token = open(a.token_file).read().strip()
    else:
        token = mint_token(a.user_pool, a.client_id, a.username, os.environ[a.password_env], a.region)
        if a.token_file:
            with open(os.open(a.token_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as f:
                f.write(token)
    arn = runtime_arn(a.run_id, a.region)
    sid = a.session or f"smoke-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}"
    op, args = a.op, a.args
    if op == "health":
        payload = {"op": "health"}
    elif op.startswith("chat:") or op.startswith("submit:"):
        kind, mode = op.split(":", 1)
        payload = {"op": kind, "mode": mode, "messages": [{"role": "user", "content": " ".join(args)}],
                   "client_request_id": f"smoke-{uuid.uuid4().hex[:12]}", "conversation_id": sid,
                   **({"collection": a.collection} if a.collection else {})}
    elif op == "events":
        payload = {"op": "events", "job_id": args[0], "after": int(args[1]) if len(args) > 1 else 0, "tail": True}
    elif op in ("status", "cancel"):
        payload = {"op": op, "job_id": args[0]}
    elif op == "approve":
        ans = args[1] if len(args) > 1 else "approve"
        payload = {"op": "approve", "job_id": args[0], "approval": "reject" if ans == "reject" else "approve",
                   "revision": None if ans in ("approve", "reject") else ans}
    elif op == "collections":
        payload = {"op": "collections"}
    elif op == "raw":  # phase 3: any op as JSON, e.g. raw '{"op":"packages.list","limit":5}'
        payload = json.loads(" ".join(args))
    elif op == "ingest":
        docs = []
        for path in args[1:]:
            with open(path, "rb") as f:
                docs.append({"name": os.path.basename(path), "content_type": "text/plain" if path.endswith((".txt", ".md")) else
                             "application/pdf" if path.endswith(".pdf") else "application/octet-stream",
                             "content_b64": base64.b64encode(f.read()).decode()})
        payload = {"op": "ingest", "collection": args[0], "documents": docs}
    else:
        ap.error(f"unknown op {op}")
        return 2
    events = invoke(arn, a.region, token, sid, payload)
    terminal = [e for e in events if e.get("type") in ("completed", "cancelled", "error", "job.status", "health", "packages", "package",
                                                        "package.deleted", "comparison", "export", "artifact.url", "models",
                                                        "models.prefs", "models.validated", "eval.started", "evals")]
    print(f"# session={sid} job_ids={sorted({e.get('job_id') for e in events if e.get('job_id')})} terminal={[e.get('type') for e in terminal][-1:]}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Install (or refresh) the AI-Q manifold pipe in a running Open WebUI through its admin API,
without touching the database or any other integration. Idempotent.

Steps (Open WebUI v0.11.x API, verified against pinned source):
  1. POST /api/v1/functions/create  (or /id/{id}/update when it exists)  — the pipe code
  2. POST /api/v1/functions/id/{id}/valves/update                        — RUNTIME_ARN, REGION, RUN_ID
  3. POST /api/v1/functions/id/{id}/toggle                               — activate (created inactive)
  4. For each exposed model <id>.<mode>: POST /api/v1/models/model/access/update with a read grant for
     ONE group only (the run's tester group), so no other user sees the new models. Admins see all models.

Auth: an admin Open WebUI session JWT (from the browser: `localStorage.token`, or GET /api/v1/auths/).
Usage:
  OWUI_TOKEN=... python aiq/scripts/install_pipe.py --url https://oui.example --function-id aiq_agentcore \
      --pipe aiq/pipe/aiq_agentcore_pipe.py --runtime-arn arn:... --region us-east-1 --run-id fable51-79d40d45 \
      --group-name aiq-fable51-79d40d45-testers [--disable]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import httpx

MODES = ["auto", "shallow", "deep", "deep_clarify"]
MODEL_NAMES = {"auto": "AI-Q Research (auto)", "shallow": "AI-Q Quick answer", "deep": "AI-Q Deep research",
               "deep_clarify": "AI-Q Deep research (clarify first)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--function-id", default="aiq_agentcore")
    ap.add_argument("--pipe", required=True)
    ap.add_argument("--runtime-arn", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--group-name", required=True, help="Open WebUI group that may read the models (from Cognito group sync)")
    ap.add_argument("--disable", action="store_true", help="deactivate the function instead of installing")
    ap.add_argument("--workbench-url", default="", help="Research Workbench base URL (deep links on package cards)")
    ap.add_argument("--actions", default="", help="path to aiq_actions.py; installs/updates the action function and binds it to the 4 models")
    a = ap.parse_args()
    token = os.environ.get("OWUI_TOKEN")
    if not token:
        print("OWUI_TOKEN (admin session JWT) is required", file=sys.stderr)
        return 2
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    c = httpx.Client(base_url=a.url.rstrip("/"), headers=h, timeout=60)

    me = c.get("/api/v1/auths/").json()
    print(f"signed in as role={me.get('role')} id={me.get('id')}")
    if me.get("role") != "admin":
        print("an admin session is required", file=sys.stderr)
        return 3

    existing = c.get(f"/api/v1/functions/id/{a.function_id}")
    if a.disable:
        if existing.status_code == 200 and existing.json().get("is_active"):
            r = c.post(f"/api/v1/functions/id/{a.function_id}/toggle")
            print("toggled:", r.status_code, r.json().get("is_active"))
        else:
            print("already inactive or absent")
        return 0

    content = open(a.pipe, encoding="utf-8").read()
    version = re.search(r"^version:\s*(\S+)", content, re.M)
    body = {"id": a.function_id, "name": "AI-Q Research on AgentCore", "content": content,
            "meta": {"description": f"NVIDIA AI-Q research agents on Amazon Bedrock AgentCore (run {a.run_id}, pipe {version.group(1) if version else '?'})",
                     "manifest": {}}}
    if existing.status_code == 200:
        r = c.post(f"/api/v1/functions/id/{a.function_id}/update", json=body)
        print("function updated:", r.status_code)
    else:
        r = c.post("/api/v1/functions/create", json=body)
        print("function created:", r.status_code)
    if r.status_code >= 300:
        print(r.text[:500], file=sys.stderr)
        return 4
    r = c.post(f"/api/v1/functions/id/{a.function_id}/valves/update",
               json={"RUNTIME_ARN": a.runtime_arn, "REGION": a.region, "RUN_ID": a.run_id, "WORKBENCH_URL": a.workbench_url})
    print("valves:", r.status_code, {k: v for k, v in (r.json() if r.status_code == 200 else {}).items() if k != "RUNTIME_ARN"})
    fn = c.get(f"/api/v1/functions/id/{a.function_id}").json()
    if not fn.get("is_active"):
        r = c.post(f"/api/v1/functions/id/{a.function_id}/toggle")
        print("activated:", r.status_code, r.json().get("is_active"))
    else:
        print("already active")

    groups = c.get("/api/v1/groups/").json()
    gid = next((g["id"] for g in groups if g.get("name") == a.group_name), None)
    if not gid:
        print(f"group {a.group_name!r} not found in Open WebUI yet (it is created on the first OIDC login of a member); "
              "models remain admin-only until you re-run this step.", file=sys.stderr)
        return 5
    grants = [{"principal_type": "group", "principal_id": gid, "permission": "read"}]
    for mode in MODES:
        mid = f"{a.function_id}.{mode}"
        r = c.post("/api/v1/models/model/access/update", json={"id": mid, "name": MODEL_NAMES[mode], "access_grants": grants})
        print(f"access {mid}: {r.status_code}", (r.text[:120] if r.status_code >= 300 else "ok"))
    # ---- action function: install + bind to the four AI-Q model rows (meta.actionIds; A Q11-03) ----
    if a.actions:
        acontent = open(a.actions, encoding="utf-8").read()
        aid = "aiq_actions"
        abody = {"id": aid, "name": "AI-Q package actions", "content": acontent,
                 "meta": {"description": f"Export / Re-run / Compare / Open for AI-Q research packages (run {a.run_id})", "manifest": {}}}
        ex = c.get(f"/api/v1/functions/id/{aid}")
        r = c.post(f"/api/v1/functions/id/{aid}/update" if ex.status_code == 200 else "/api/v1/functions/create", json=abody)
        print("action function:", r.status_code, (r.text[:200] if r.status_code >= 300 else "ok"))
        r = c.post(f"/api/v1/functions/id/{aid}/valves/update", json={"RUNTIME_ARN": a.runtime_arn, "REGION": a.region, "WORKBENCH_URL": a.workbench_url})
        print("action valves:", r.status_code)
        fn = c.get(f"/api/v1/functions/id/{aid}").json()
        if not fn.get("is_active"):
            print("action activated:", c.post(f"/api/v1/functions/id/{aid}/toggle").status_code)
        for mode in MODES:
            mid = f"{a.function_id}.{mode}"
            row = c.get("/api/v1/models/model", params={"id": mid})
            if row.status_code == 200 and row.json():
                model = row.json()
                meta = dict(model.get("meta") or {})
                meta["actionIds"] = sorted(set((meta.get("actionIds") or []) + [f"{aid}.{s['id']}" for s in
                                                                                  [{"id": "export"}, {"id": "rerun"}, {"id": "compare"}, {"id": "open"}]]))
                form = {"id": mid, "base_model_id": model.get("base_model_id"), "name": model.get("name") or MODEL_NAMES[mode], "meta": meta,
                        "params": model.get("params") or {}, "access_grants": model.get("access_grants") or grants, "is_active": True}
                r = c.post("/api/v1/models/model/update", params={"id": mid}, json=form)
            else:
                form = {"id": mid, "base_model_id": None, "name": MODEL_NAMES[mode],
                        "meta": {"actionIds": [f"{aid}.export", f"{aid}.rerun", f"{aid}.compare", f"{aid}.open"]}, "params": {},
                        "access_grants": grants, "is_active": True}
                r = c.post("/api/v1/models/create", json=form)
            print(f"bind actions → {mid}: {r.status_code}", (r.text[:160] if r.status_code >= 300 else "ok"))
    print(json.dumps({"function_id": a.function_id, "group_id": gid, "models": [f"{a.function_id}.{m}" for m in MODES]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

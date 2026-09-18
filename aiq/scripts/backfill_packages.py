#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Backfill Research Packages for jobs completed before phase 3 (ADR-21 migration).

Runs with operator credentials against the run's tables/bucket (outside the JWT path — it is tenant-agnostic by
construction: every package is written under the job's own tenant prefix, nothing crosses tenants). Idempotent:
existing manifests are refreshed, PKG# rows are put/updated, nothing is deleted. Jobs without a stored report
(meta turns, failures) get no package.

  AWS_PROFILE=sa-ss uv run --no-project --with boto3 --with pydantic --with pyyaml --with httpx --with "PyJWT[crypto]" \\
      python aiq/scripts/backfill_packages.py --run-id fable51-79d40d45 [--dry-run] [--tenant u_...]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "runtime", "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--tenant")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    import boto3

    cfn = boto3.client("cloudformation", region_name=a.region)
    outs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=f"aiq-{a.run_id}")["Stacks"][0]["Outputs"]}
    os.environ.update(
        AIQ_JOBS_TABLE=outs["JobsTable"],
        AIQ_EVENTS_TABLE=outs["EventsTable"],
        AIQ_ARTIFACTS_BUCKET=outs["ArtifactsBucket"],
        AIQ_REGION=a.region,
    )
    from aiq_agentcore import modellab_runtime, packages
    from aiq_agentcore.contracts import JobRecord, JobStatus
    from aiq_agentcore.store import JobStore, _clean

    st = JobStore(region=a.region)
    mx = modellab_runtime.load_matrix(st)
    print(f"matrix: {'loaded ' + (mx.get('generated_at') or '') if mx else 'unavailable'}")
    scan = st.jobs.meta.client.get_paginator("scan")
    done = skipped = 0
    for page in scan.paginate(TableName=st.jobs.name):
        for raw in page.get("Items", []):
            item = _clean(raw)  # the resource's client already returns plain Python values
            if not str(item.get("sk", "")).startswith("JOB#"):
                continue
            tenant = str(item["pk"]).removeprefix("TENANT#")
            if a.tenant and tenant != a.tenant:
                continue
            item.pop("pk", None)
            item.pop("sk", None)
            try:
                rec = JobRecord.model_validate(item)
            except Exception as e:  # noqa: BLE001
                print("  skip (unparseable)", item.get("job_id"), e.__class__.__name__)
                skipped += 1
                continue
            if rec.status != JobStatus.COMPLETED or not rec.report_key or st.head(rec.report_key) is None:
                skipped += 1
                continue
            if st.is_tombstoned(tenant, rec.job_id):
                skipped += 1
                continue
            if not rec.package_sk:
                rec = rec.model_copy(
                    update={"package_sk": st.package_sk(rec.created_at, rec.job_id), "relation": rec.relation or "root"}
                )
                if not a.dry_run:
                    st.update_job(tenant, rec.job_id, package_sk=rec.package_sk, relation=rec.relation)
            if a.dry_run:
                print("  would backfill", tenant[:12], rec.job_id, rec.mode.value, rec.created_at)
                done += 1
                continue
            events = st.read_events(rec.job_id, after=0, limit=2000)
            arts = [e.data.get("record") for e in events if e.type.value == "artifact" and e.data.get("record")]
            manifest = packages.finalize(st, rec, matrix=mx, runtime_version="backfill", artifacts=arts)
            manifest["models"]["selection"] = "backfill_inferred"
            packages.write_manifest(st, manifest)
            print(
                f"  backfilled {tenant[:12]} {rec.job_id} {rec.mode.value} sources={len(manifest['sources'])} cost=${manifest['cost']['total_usd']:.4f}"  # noqa: E501
            )
            done += 1
    print(json.dumps({"backfilled": done, "skipped": skipped, "dry_run": a.dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

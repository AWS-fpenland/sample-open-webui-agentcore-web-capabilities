#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model Lab operator CLI — build the honest Capability Matrix for AI-Q on AgentCore.

  uv run --no-project --with boto3 --with httpx --with aws-bedrock-token-generator python aiq/scripts/modellab.py \
      catalog  --out <dir> [--from-evidence <dir>]          # live control-plane snapshot (or reuse saved JSON)
      pricing  --out <dir> [--offers-dir <dir>] [--overlay <json>]
      probe    --out <dir> [--lanes converse,mantle_chat,...] [--only <substr>] [--workers 6]
      roles    --out <dir> [--only <substr>] [--top N]        # role mini-probes on entries that passed plain+tools
      matrix   --out <dir> [--publish s3://bucket/key]         # assemble + digest (+ upload latest.json)

Every step writes JSON under --out (default evidence/model-lab). Credentials: the caller's (operator) — the matrix records
who probed. Costs of the sweep are computed from the same Price List rates and printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))  # aiq/
from modellab import catalog as C, matrix as M, pricing as P, probes as PR, roles as R  # noqa: E402


def _w(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)


def _r(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_catalog(a) -> None:
    if a.from_evidence:
        d = a.from_evidence
        inputs = C.load_catalog_inputs(
            {
                "foundation_models": f"{d}/00-list-foundation-models.json",
                "inference_profiles": f"{d}/01-list-inference-profiles-system.json",
                "mantle": {r: f"{d}/02-mantle-models-{r}.json" for r in a.mantle_regions.split(",")},
            }
        )
    else:
        inputs = C.fetch_catalog_inputs(a.region, a.mantle_regions.split(","))
        _w(f"{a.out}/00-list-foundation-models.json", {"modelSummaries": inputs["foundation_models"]})
        _w(f"{a.out}/01-list-inference-profiles-system.json", {"inferenceProfileSummaries": inputs["inference_profiles"]})
        for r, data in inputs["mantle"].items():
            _w(f"{a.out}/02-mantle-models-{r}.json", {"object": "list", "data": data})
    entries = C.build_catalog(
        foundation_models=inputs["foundation_models"],
        inference_profiles=inputs["inference_profiles"],
        mantle_models=inputs["mantle"],
        region=a.region,
    )
    names = {}
    for m in inputs["foundation_models"]:
        base, _ = C._base_id(m["modelId"])
        names.setdefault(base, m.get("modelName") or base)
    _w(
        f"{a.out}/catalog.json",
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "region": a.region,
            "control_plane_names": names,
            "entries": [e.to_dict() for e in entries],
        },
    )
    by_lane = {}
    for e in entries:
        by_lane[e.lane] = by_lane.get(e.lane, 0) + 1
    print(f"catalog: {len(entries)} entries {by_lane}")


def cmd_pricing(a) -> None:
    cat = _r(f"{a.out}/catalog.json")
    if a.offers_dir:
        offers = P.load_offers(
            {
                svc: f"{a.offers_dir}/{svc}.{a.region}.json"
                for svc in P.SERVICES
                if os.path.exists(f"{a.offers_dir}/{svc}.{a.region}.json")
            }
        )
    else:
        offers = P.fetch_offers(a.region)
    rates, acc, versions = P.parse_all(offers, a.region)
    overlay = P.load_overlay(a.overlay) if a.overlay else None
    prices = P.join_prices(cat["entries"], rates, acc, cat["control_plane_names"], overlay)
    _w(
        f"{a.out}/prices.json",
        {
            "offer_versions": versions,
            "prices": prices,
            "accounting": {
                "excluded": len(acc.excluded),
                "unclassified": acc.unclassified[:50],
                "rate_conflicts": acc.rate_conflicts[:50],
            },
        },
    )
    priced = sum(1 for v in prices.values() if v["source"] != "unpriced")
    print(f"pricing: {priced}/{len(prices)} base ids priced; offers {versions}; unclassified {len(acc.unclassified)}")
    for k, v in prices.items():
        if v["source"] == "unpriced":
            print("  UNPRICED", k)


def cmd_probe(a) -> None:
    import boto3

    cat = _r(f"{a.out}/catalog.json")
    lanes = a.lanes.split(",")
    entries = [
        e
        for e in cat["entries"]
        if e["lane"] in lanes and (not a.only or a.only in e["id"]) and e.get("mantle_status") != "unavailable"
    ]
    print(f"probing {len(entries)} entries on lanes {lanes} with {a.workers} workers", flush=True)
    os.makedirs(f"{a.out}/probes", exist_ok=True)
    existing = {}
    if os.path.exists(f"{a.out}/probes.json") and not a.fresh:
        existing = _r(f"{a.out}/probes.json")
    if not a.fresh:
        # per-entry files are the durable record of a sweep (probes.json is only the aggregate); recover from them
        by_file = {
            f"{e['lane']}__{e['id'].replace(':', '_').replace('/', '_')}.json": f"{e['lane']}::{e['id']}" for e in cat["entries"]
        }
        for fn in sorted(os.listdir(f"{a.out}/probes")) if os.path.isdir(f"{a.out}/probes") else []:
            key = by_file.get(fn)
            if key and key not in existing:
                data = _r(f"{a.out}/probes/{fn}")
                existing[key] = data.get("results", data) if isinstance(data, dict) and "results" in data else data
        if a.retry_failed:
            # Marketplace/throttle failures are intermittent in this account (observed 2026-09-18: the same id passed at
            # 18:37Z and was denied at 19:02Z). Re-probe those entries; keep every earlier attempt under `attempts`.
            transient = ("AccessDeniedException", "ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException")
            retry = {
                k
                for k, v in existing.items()
                if not (v.get("plain") or {}).get("ok")
                and any(
                    str((v.get("plain") or {}).get("error_code") or "").startswith(t) for t in transient + ("http_429", "http_5")
                )
            }
            entries = [e for e in entries if f"{e['lane']}::{e['id']}" in retry]
            print(f"  retrying {len(entries)} entries whose first attempt failed transiently", flush=True)
        else:
            entries = [e for e in entries if f"{e['lane']}::{e['id']}" not in existing]
            print(f"  {len(existing)} already probed; {len(entries)} to go", flush=True)
    t0 = time.monotonic()

    def on_done(e, res):
        oks = "".join("✓" if r.ok else "✗" for r in res.values())
        first_err = next((f"{r.probe}:{r.error_code}" for r in res.values() if not r.ok and r.error_code), "")
        print(f"  {time.strftime('%H:%M:%S')} {e['lane']:16s} {e['id']:55s} {oks} {first_err}", flush=True)
        _w(
            f"{a.out}/probes/{e['lane']}__{e['id'].replace(':', '_').replace('/', '_')}.json",
            {
                "key": f"{e['lane']}::{e['id']}",
                "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "results": {k: v.to_dict() for k, v in res.items()},
            },
        )

    sess = boto3.Session()
    ident = sess.client("sts").get_caller_identity()["Arn"]
    results = PR.run_sweep(
        entries, region=a.region, mantle_regions=a.mantle_regions.split(","), workers=a.workers, on_done=on_done, session=sess
    )
    for k, v in results.items():
        prev = existing.get(k)
        if prev is not None:
            attempts = prev.pop("attempts", [])
            attempts.append({"at": prev.get("probed_at"), "plain": prev.get("plain")})
            v["attempts"] = attempts
        v["probed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        existing[k] = v
    _w(f"{a.out}/probes.json", existing)
    _w(
        f"{a.out}/probes.meta.json",
        {
            "probed_as": ident,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seconds": round(time.monotonic() - t0, 1),
            "entries": len(existing),
        },
    )
    print(f"probe sweep done in {round(time.monotonic() - t0)}s; {len(existing)} entries recorded")


def cmd_roles(a) -> None:
    import boto3

    cat = _r(f"{a.out}/catalog.json")
    probes = _r(f"{a.out}/probes.json")
    cands = []
    for e in cat["entries"]:
        pr = probes.get(f"{e['lane']}::{e['id']}") or {}
        if e["lane"] not in M.RUNTIME_LANES:
            continue
        if (pr.get("plain") or {}).get("ok") and (pr.get("stream") or {}).get("ok") and (not a.only or a.only in e["id"]):
            cands.append(e)
    # one entry per (base id, lane) — prefer the global profile, then geo, then in-region
    pref = {"global": 0, "geo": 1, "in_region": 2}
    best: dict[tuple[str, str], dict] = {}
    for e in cands:
        k = (e["base_model_id"], e["lane"])
        if k not in best or pref[e["routing"]] < pref[best[k]["routing"]]:
            best[k] = e
    cands = sorted(best.values(), key=lambda e: (e["provider"], e["name"]))
    if a.lanes and a.lanes != "converse,mantle_chat,mantle_responses,mantle_messages":
        cands = [e for e in cands if e["lane"] in a.lanes.split(",")]
    if a.top:
        cands = cands[: a.top]
    role_subset = tuple(r for r in (a.roles or "").split(",") if r) or None
    print(f"role probes on {len(cands)} entries", flush=True)
    existing = _r(f"{a.out}/roles.json") if os.path.exists(f"{a.out}/roles.json") and not a.fresh else {}
    sess = boto3.Session()
    conv = PR.ConverseProber(a.region, sess)
    mantle = {r: PR.MantleProber(r, PR.MantleProber.mint_token(r)) for r in a.mantle_regions.split(",")}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(e):
        return e, R.probe_roles(e, conv, mantle.get(e["region"]), roles=role_subset or tuple(R.ROLE_PROBES))

    # with --roles, re-probe only those roles for every candidate and merge; otherwise skip entries already probed
    todo = cands if role_subset else [e for e in cands if f"{e['lane']}::{e['id']}" not in existing]
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for fut in as_completed([pool.submit(one, e) for e in todo]):
            e, res = fut.result()
            key = f"{e['lane']}::{e['id']}"
            merged = dict(existing.get(key) or {})
            merged.update({k: v.to_dict() for k, v in res.items()})
            existing[key] = merged
            print(
                f"  {time.strftime('%H:%M:%S')} {e['lane']:16s} {e['id']:55s} "
                + " ".join(f"{k}={v.score}" for k, v in res.items()),
                flush=True,
            )
            _w(f"{a.out}/roles.json", existing)
    _w(f"{a.out}/roles.json", existing)
    print("roles done")


def cmd_matrix(a) -> None:
    cat = _r(f"{a.out}/catalog.json")
    prices = _r(f"{a.out}/prices.json")
    probes = _r(f"{a.out}/probes.json") if os.path.exists(f"{a.out}/probes.json") else {}
    roles = _r(f"{a.out}/roles.json") if os.path.exists(f"{a.out}/roles.json") else {}
    meta = _r(f"{a.out}/probes.meta.json") if os.path.exists(f"{a.out}/probes.meta.json") else {}
    defaults = json.loads(a.defaults) if a.defaults else {}
    mx = M.assemble(
        cat["entries"],
        prices["prices"],
        probes,
        roles,
        probed_as=meta.get("probed_as", "unknown"),
        region=a.region,
        offer_versions=prices["offer_versions"],
        defaults=defaults,
    )
    _w(f"{a.out}/matrix.json", mx)
    print(json.dumps(mx["summary"], indent=1))
    for role in ("writer", "planner", "shallow", "router"):
        print(f"--- leaderboard {role}")
        for r in M.leaderboard(mx, role, 8):
            print(
                f"  {r['score']:3d} {r['name']:32s} {r['lane']:16s} {r['id']:50s} in={r['input_per_1m']} out={r['output_per_1m']} ttft={r['ttft_ms']}"  # noqa: E501
            )
    if a.publish:
        import boto3

        bucket, key = a.publish[5:].split("/", 1)
        s3 = boto3.client("s3", region_name=a.region)
        body = json.dumps(mx, ensure_ascii=False, default=str).encode()
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        s3.put_object(
            Bucket=bucket,
            Key=key.rsplit("/", 1)[0] + f"/{mx['generated_at'].replace(':', '')}.json",
            Body=body,
            ContentType="application/json",
        )
        print(f"published s3://{bucket}/{key} ({len(body)} bytes, digest {mx['digest'][:12]})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["catalog", "pricing", "probe", "roles", "matrix"])
    ap.add_argument("--out", default="evidence/model-lab")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--mantle-regions", default="us-east-1")
    ap.add_argument("--from-evidence")
    ap.add_argument("--offers-dir")
    ap.add_argument("--overlay")
    ap.add_argument("--lanes", default="converse,mantle_chat,mantle_responses,mantle_messages")
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--top", type=int)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--roles", help="roles subset for `roles` (comma list), re-probed and merged into roles.json")
    ap.add_argument("--defaults")
    ap.add_argument("--publish")
    a = ap.parse_args()
    {"catalog": cmd_catalog, "pricing": cmd_pricing, "probe": cmd_probe, "roles": cmd_roles, "matrix": cmd_matrix}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())

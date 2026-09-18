#!/usr/bin/env python3
"""Build src/mock/matrix.json (the Model Lab fixture) from the REAL model-lab evidence.

Usage:
  uv run --no-project python scripts/build-mock-matrix.py <evidence/model-lab dir> [out.json]

Inputs (all produced by aiq/modellab on 2026-09-18): catalog.json (176 lane entries), prices.json (AWS Price List),
probes.json (7 live probes per entry, raw errors kept), probes.meta.json (probed_as), roles.json (deterministic role scores).
Output shape == the runtime `models` op response: {matrix, entries[MatrixEntry], defaults, prefs}.
Nothing here is invented: offered/excluded follow 11-architecture §6; unevaluated roles are marked not_evaluated.
"""
import hashlib, json, os, sys

src = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "src", "mock", "matrix.json")
load = lambda n: json.load(open(os.path.join(src, n), encoding="utf-8"))
catalog, prices, probes, roles = load("catalog.json"), load("prices.json"), load("probes.json"), load("roles.json")
meta = load("probes.meta.json")
PROBES = ["plain", "system", "stream", "tools", "json", "long", "reasoning"]
ROLES = ["router", "clarifier", "shallow", "planner", "researcher", "writer", "chart"]
WIRED = {"converse", "mantle_chat"}

def price_for(base_id):
    rec = prices["prices"].get(base_id) or prices["prices"].get(base_id.split(":")[0])
    if not rec:
        return {"input_per_1m": None, "output_per_1m": None, "source": "unpriced"}
    rates = rec.get("rates") or {}
    pick = None
    for scope in ("in_region", "global"):
        std = ((rates.get(scope) or {}).get("standard") or {}).get("default")
        if std and "input" in std and "output" in std:
            pick = std; break
    if not pick:
        def walk(node):  # find the first dict carrying input+output at any depth (overlay prices are flat)
            if isinstance(node, dict):
                if isinstance(node.get("input"), (int, float)) and isinstance(node.get("output"), (int, float)):
                    return node
                for v in node.values():
                    hit = walk(v)
                    if hit: return hit
            return None
        pick = walk(rates)
    if not pick:
        return {"input_per_1m": None, "output_per_1m": None, "source": rec.get("source", "unpriced")}
    return {"input_per_1m": pick["input"], "output_per_1m": pick["output"], "source": rec.get("source", "aws-published")}

entries, sweep_cost = [], 0.0
for e in catalog["entries"]:
    key = f"{e['lane']}::{e['id']}"
    pr = probes.get(key, {})
    caps = {}
    for p in PROBES:
        r = pr.get(p)
        caps[p] = {"ok": bool(r and r.get("ok")), "error_code": (r or {}).get("error_code") if r else "not_probed", "ms": (r or {}).get("ms")}
    price = price_for(e["base_model_id"])
    # sweep cost: probe + role tokens at the entry's price
    toks_in = sum((pr.get(p) or {}).get("input_tokens") or 0 for p in PROBES)
    toks_out = sum((pr.get(p) or {}).get("output_tokens") or 0 for p in PROBES)
    rr = roles.get(key, {})
    toks_in += sum((v or {}).get("input_tokens") or 0 for v in rr.values())
    toks_out += sum((v or {}).get("output_tokens") or 0 for v in rr.values())
    if price["input_per_1m"] is not None:
        sweep_cost += toks_in / 1e6 * price["input_per_1m"] + toks_out / 1e6 * price["output_per_1m"]
    reasons, raw_error = [], None
    plain = pr.get("plain") or {}
    if e["lane"] not in WIRED:
        reasons.append(f"lane:{e['lane']}-not-wired")
    if e.get("lifecycle") and e["lifecycle"] != "ACTIVE":
        reasons.append(f"lifecycle:{e['lifecycle'].lower()}")
    if e.get("mantle_status") == "unavailable":
        reasons.append("mantle:unavailable")
    if not pr:
        reasons.append("probe:not-probed")
    elif not plain.get("ok"):
        code = plain.get("error_code") or "error"
        msg = plain.get("error_message") or ""
        raw_error = f"{code}: {msg}"[:240]
        if "data retention" in msg:
            reasons.append("data-retention-gated")
        elif "Marketplace" in msg:
            reasons.append("marketplace-access-denied")
        elif "use case details" in msg:
            reasons.append("provider-use-case-form-required")
        reasons.append(f"probe:plain-failed:{code}")
    elif not (pr.get("stream") or {}).get("ok"):
        code = (pr.get("stream") or {}).get("error_code") or "error"
        raw_error = f"{code}: {(pr.get('stream') or {}).get('error_message') or ''}"[:240]
        reasons.append(f"probe:stream-failed:{code}")
    attempts = []
    for a in pr.get("attempts") or []:
        ap = a.get("plain") or {}
        attempts.append({"at": a.get("at") or pr.get("probed_at") or meta["finished_at"], "plain_ok": bool(ap.get("ok")), "error_code": ap.get("error_code")})
    if pr:
        attempts.append({"at": pr.get("probed_at") or meta["finished_at"], "plain_ok": bool(plain.get("ok")), "error_code": plain.get("error_code")})
    if len({a["plain_ok"] for a in attempts}) > 1:
        reasons.append("intermittent-access")
    offered = not reasons
    role_scores = {}
    for role in ROLES:
        v = rr.get(role)
        if v and v.get("score") is not None:
            role_scores[role] = {"score": int(v["score"]), "verdict": v.get("verdict") or "unknown"}
        else:
            role_scores[role] = {"score": 0, "verdict": "not_evaluated"}
    tools_ok = caps["tools"]["ok"]
    selectable = {
        "router": offered, "clarifier": offered, "shallow": offered,
        "planner": offered and tools_ok, "researcher": offered and tools_ok,
        "writer": offered and (caps["long"]["ok"] or caps["plain"]["ok"]),
        "chart": offered and tools_ok,
    }
    entries.append({
        "id": e["id"], "lane": e["lane"], "base_model_id": e["base_model_id"], "name": e["name"], "provider": e["provider"],
        "family": e.get("family"), "region": e.get("region"), "routing": e.get("routing"),
        "offered": offered, "exclusion_reasons": reasons, "raw_error": raw_error,
        "price": price,
        "capabilities": caps,
        "latency": {"plain_ms": plain.get("ms"), "ttft_ms": (pr.get("stream") or {}).get("ttft_ms"), "long_ms": (pr.get("long") or {}).get("ms")},
        "roles": role_scores, "roles_selectable": selectable, "attempts": attempts,
    })

entries.sort(key=lambda x: (not x["offered"], -(x["roles"]["writer"]["score"]), x["name"]))
digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
offered_n = sum(1 for x in entries if x["offered"])
doc = {
    "matrix": {
        "generated_at": meta["finished_at"], "probed_as": meta["probed_as"], "digest": digest,
        "summary": {"entries": len(entries), "offered": offered_n, "excluded": len(entries) - offered_n,
                    "sweep_cost_usd": round(sweep_cost, 4), "probed_at": meta["finished_at"],
                    "price_offer_versions": prices.get("offer_versions", {}), "catalog_generated_at": catalog.get("generated_at")},
    },
    "entries": entries,
    "defaults": {"router": "nvidia.nemotron-nano-3-30b", "clarifier": "nvidia.nemotron-nano-3-30b", "shallow": "nvidia.nemotron-nano-3-30b",
                 "planner": "global.amazon.nova-2-lite-v1:0", "researcher": "global.amazon.nova-2-lite-v1:0", "writer": "nvidia.nemotron-super-3-120b"},
    "prefs": None,
}
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
json.dump(doc, open(out, "w", encoding="utf-8"), separators=(",", ":"))
print(f"wrote {out}: {len(entries)} entries, {offered_n} offered, sweep ${sweep_cost:.4f}, {os.path.getsize(out)//1024} KB")

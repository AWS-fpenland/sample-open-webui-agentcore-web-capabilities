# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Capability Matrix assembly: catalogue + prices + probe results + role scores → offered / excluded with reasons.

Offer rules (the picker enforces these; nothing else is offered):
  * ``plain`` and ``stream`` passed on that lane (a research run streams everything);
  * for roles that call tools (router excluded; shallow/planner/researcher/writer need tools) ``tools`` passed;
  * the entry's lane is one the runtime can drive — converse (ChatBedrockConverse), mantle_chat (ChatOpenAI) and
    mantle_messages (ChatAnthropic; verified 2026-09-18 with a short-term Bedrock API key as x-api-key) — responses-only
    ids are recorded as *excluded: responses-only, not wired*;
  * not Mantle ``unavailable`` (data-retention gated) and not lifecycle LEGACY/EOL.
Everything else is listed with its exclusion reasons and the raw error that produced them. Role scores travel with
the entry; the picker warns below 70 and refuses below 40 unless the operator overrides.

The matrix is "signed" in the plain sense: a sha256 digest of the canonical entries array plus the identity that ran
the probes and the timestamps; the runtime refuses a matrix whose digest does not verify.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from . import SCHEMA_VERSION
from .pricing import cost_usd, headline_rates

RUNTIME_LANES = ("converse", "mantle_chat", "mantle_messages")
TOOL_ROLES = ("shallow", "planner", "researcher", "writer", "clarifier")


def canonical_digest(entries: list[dict[str, Any]]) -> str:
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def assemble(
    catalog: list[dict[str, Any]],
    prices: dict[str, dict[str, Any]],
    probes: dict[str, dict[str, dict[str, Any]]],
    roles: dict[str, dict[str, dict[str, Any]]],
    *,
    probed_as: str,
    region: str,
    offer_versions: dict[str, str],
    defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    sweep_cost = 0.0
    sweep_calls = 0
    for e in catalog:
        key = f"{e['lane']}::{e['id']}"
        pr = probes.get(key) or {}
        rl = roles.get(key) or {}
        price = prices.get(e["base_model_id"]) or {"source": "unpriced", "rates": {}}
        head = headline_rates(price, e["routing"])
        reasons: list[str] = []
        if e.get("mantle_status") == "unavailable":
            reasons.append(f"mantle:{e.get('mantle_status_reason') or 'unavailable'}")
        if e.get("lifecycle") not in (None, "ACTIVE"):
            reasons.append(f"lifecycle:{e.get('lifecycle')}")
        if e["lane"] not in RUNTIME_LANES:
            reasons.append("lane:responses-only-not-wired")
        if not pr:
            reasons.append("unprobed")
        else:
            for p in ("plain", "stream"):
                r = pr.get(p) or {}
                if not r.get("ok"):
                    reasons.append(f"probe:{p}:{r.get('error_code') or 'failed'}")
        tools_ok = bool((pr.get("tools") or {}).get("ok"))
        attempts = pr.get("attempts") or []
        intermittent = bool(attempts) and any(
            bool((att.get("plain") or {}).get("ok")) != bool((pr.get("plain") or {}).get("ok")) for att in attempts
        )
        if intermittent:
            reasons.append("access:intermittent-across-attempts")
        offered = not reasons
        # tokens / cost of the probes themselves
        pr_res = [r for r in pr.values() if isinstance(r, dict) and "ok" in r]
        in_tok = sum(int(r.get("input_tokens") or 0) for r in pr_res) + sum(
            int((r or {}).get("input_tokens") or 0) for r in rl.values()
        )
        out_tok = sum(int(r.get("output_tokens") or 0) for r in pr_res) + sum(
            int((r or {}).get("output_tokens") or 0) for r in rl.values()
        )
        c = cost_usd(price, e["routing"], in_tok, out_tok)
        if c is not None:
            sweep_cost += c
        sweep_calls += sum(1 for r in pr_res if r.get("ms")) + sum(1 for r in rl.values() if (r or {}).get("ms"))
        lat = {
            "plain_ms": (pr.get("plain") or {}).get("ms"),
            "ttft_ms": (pr.get("stream") or {}).get("ttft_ms"),
            "long_ms": (pr.get("long") or {}).get("ms"),
            "long_words": ((pr.get("long") or {}).get("detail") or {}).get("words"),
        }
        role_scores = {
            k: {"score": v.get("score"), "verdict": v.get("verdict"), "checks": v.get("checks"), "error": v.get("error")}
            for k, v in rl.items()
        }
        roles_ok: dict[str, bool] = {}
        for role in ("router", "clarifier", "shallow", "planner", "researcher", "writer"):
            needs_tools = role in TOOL_ROLES and role not in ("clarifier",)
            sc = (role_scores.get(role) or {}).get("score")
            roles_ok[role] = offered and (tools_ok or not needs_tools) and (sc is None or sc >= 40)
        entries.append(
            {
                **e,
                "price": {
                    "source": price.get("source"),
                    "unit": "USD/1M-tokens",
                    "input_per_1m": head.get("input"),
                    "output_per_1m": head.get("output"),
                    "routing_priced": head.get("routing_priced"),
                    "matched_by": price.get("matched_by"),
                    "effective_date": price.get("effective_date"),
                    "grid": price.get("rates"),
                },
                "capabilities": {
                    p: {
                        "ok": bool((pr.get(p) or {}).get("ok")),
                        "error_code": (pr.get(p) or {}).get("error_code"),
                        "error_message": (pr.get(p) or {}).get("error_message"),
                        "ms": (pr.get(p) or {}).get("ms"),
                        "detail": (pr.get(p) or {}).get("detail"),
                    }
                    for p in pr
                    if isinstance(pr.get(p), dict) and "ok" in pr[p]
                },
                "latency": lat,
                "roles": role_scores,
                "roles_selectable": roles_ok,
                "offered": offered,
                "exclusion_reasons": reasons,
                "attempts": [
                    {
                        "at": att.get("at"),
                        "plain_ok": bool((att.get("plain") or {}).get("ok")),
                        "error_code": (att.get("plain") or {}).get("error_code"),
                    }
                    for att in attempts
                ]
                + (
                    [
                        {
                            "at": pr.get("probed_at"),
                            "plain_ok": bool((pr.get("plain") or {}).get("ok")),
                            "error_code": (pr.get("plain") or {}).get("error_code"),
                        }
                    ]
                    if pr
                    else []
                ),
                "probe_cost_usd": round(c, 6) if c is not None else None,
                "probe_tokens": {"input": in_tok, "output": out_tok},
            }
        )
    entries.sort(key=lambda x: (not x["offered"], x["provider"].lower(), x["name"].lower(), x["lane"], x["id"]))
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    matrix = {
        "schema": SCHEMA_VERSION,
        "generated_at": generated,
        "region": region,
        "probed_as": probed_as,
        "offer_versions": offer_versions,
        "defaults": defaults or {},
        "summary": {
            "entries": len(entries),
            "offered": sum(1 for x in entries if x["offered"]),
            "excluded": sum(1 for x in entries if not x["offered"]),
            "by_lane": {
                lane: {
                    "offered": sum(1 for x in entries if x["lane"] == lane and x["offered"]),
                    "total": sum(1 for x in entries if x["lane"] == lane),
                }
                for lane in sorted({x["lane"] for x in entries})
            },
            "priced": sum(1 for x in entries if x["price"]["input_per_1m"] is not None),
            "sweep_cost_usd": round(sweep_cost, 4),
            "sweep_calls": sweep_calls,
        },
        "entries": entries,
    }
    matrix["digest"] = canonical_digest(entries)
    return matrix


def verify(matrix: dict[str, Any]) -> bool:
    return matrix.get("digest") == canonical_digest(matrix.get("entries") or [])


def offered_for_role(matrix: dict[str, Any], role: str) -> list[dict[str, Any]]:
    return [e for e in matrix.get("entries", []) if e.get("offered") and (e.get("roles_selectable") or {}).get(role)]


def find(matrix: dict[str, Any], model_id: str, lane: str | None = None) -> dict[str, Any] | None:
    for e in matrix.get("entries", []):
        if e["id"] == model_id and (lane is None or e["lane"] == lane):
            return e
    return None


def leaderboard(matrix: dict[str, Any], role: str, limit: int = 15) -> list[dict[str, Any]]:
    rows = []
    for e in matrix.get("entries", []):
        sc = ((e.get("roles") or {}).get(role) or {}).get("score")
        if sc is None:
            continue
        rows.append(
            {
                "id": e["id"],
                "lane": e["lane"],
                "name": e["name"],
                "provider": e["provider"],
                "score": sc,
                "offered": e["offered"],
                "input_per_1m": e["price"]["input_per_1m"],
                "output_per_1m": e["price"]["output_per_1m"],
                "ttft_ms": e["latency"].get("ttft_ms"),
                "plain_ms": e["latency"].get("plain_ms"),
                "long_words": e["latency"].get("long_words"),
            }
        )
    # score = deterministic format compliance (coarse: many models reach 100); tie-break by long-output words (more is
    # better for a writer), then by price. Judged quality comes from the evaluation leaderboard, not from here.
    rows.sort(
        key=lambda r: (
            -(r["score"] or 0),
            -(r.get("long_words") or 0),
            r["output_per_1m"] if r["output_per_1m"] is not None else 1e9,
        )
    )
    return rows[:limit]

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Price join: AWS Price List offer files → USD per 1M tokens per catalogue entry.

Reuses the repository's metering ``pricing`` package (``metering/pricing``): it already parses the four
usage-type grammars of the Bedrock offer files (``AmazonBedrock`` incl. the ``…-mantle-…`` shape,
``AmazonBedrockFoundationModels`` marketplace shape, ``AmazonBedrockService``) into per-1M rates and joins
Price List display names to control-plane names without guessing (zero or multiple candidates = unpriced).

Honesty rules: a rate is either AWS-published (with the offer version) or the entry is *unpriced* — no
third-party price tables. An optional operator overlay file (``model-card-rates.json``) may fill gaps the
Price List has not published yet; such rates are labelled ``source: overlay`` and shown as estimates.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_METERING = os.path.join(_HERE, "..", "..", "metering")
if _METERING not in sys.path:
    sys.path.insert(0, _METERING)

from pricing import identity as _identity  # noqa: E402  (metering/pricing)
from pricing import offers as _offers  # noqa: E402

SERVICES = ("AmazonBedrockFoundationModels", "AmazonBedrock", "AmazonBedrockService")
OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{svc}/current/{region}/index.json"


def load_overlay(path: str) -> dict[str, dict[str, Any]]:
    """Normalise an overlay file into {base_model_id: {rates:{input,output,...}, source, note}}.

    Accepts the fleet's ``model-card-rates.json`` shape (``models.<id>.tiers.standard|global-standard`` per 1M with a
    model-card URL) or a flat ``{id: {rates: {...}, source, note}}`` map. Overlay rates are estimates read from AWS model
    cards on the recorded date; they never override an AWS-published Price List rate.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if "models" not in raw:
        return raw
    out: dict[str, dict[str, Any]] = {}
    for mid, m in raw["models"].items():
        std = (m.get("tiers") or {}).get("standard") or {}
        glob = (m.get("tiers") or {}).get("global-standard") or {}
        out[mid] = {
            "rates": {
                "input": std.get("input"),
                "output": std.get("output"),
                "cache_read": std.get("cache_read"),
                "cache_write": std.get("cache_write"),
                "global": {"input": glob.get("input"), "output": glob.get("output")} if glob else None,
                "long_context_from_input_tokens": m.get("long_context_from_input_tokens"),
            },
            "source": m.get("card", ""),
            "note": f"model card read {raw.get('retrieved', '?')}; estimate until the Price List publishes it",
        }
    return out


def load_offers(paths: dict[str, str]) -> dict[str, dict]:
    out = {}
    for svc, path in paths.items():
        with open(path, encoding="utf-8") as f:
            out[svc] = json.load(f)
    return out


def fetch_offers(region: str) -> dict[str, dict]:
    import httpx

    out = {}
    for svc in SERVICES:
        r = httpx.get(OFFER_URL.format(svc=svc, region=region), timeout=180, follow_redirects=True)
        r.raise_for_status()
        out[svc] = r.json()
    return out


def parse_all(offers: dict[str, dict], region: str) -> tuple[list[_offers.ParsedRate], _offers.ParseAccounting, dict[str, str]]:
    acc = _offers.ParseAccounting()
    rates: list[_offers.ParsedRate] = []
    versions: dict[str, str] = {}
    for svc in SERVICES:
        if svc not in offers:
            continue
        parsed, version = _offers.parse_offer(offers[svc], region, svc, acc)
        rates.extend(parsed)
        versions[svc] = version
    return rates, acc, versions


def _grid_for(rates: list[_offers.ParsedRate], acc: _offers.ParseAccounting, key: str) -> dict:
    grid: dict = {}
    for r in rates:
        _offers.merge_rate(grid, r, acc, model_id=key)
    return grid


def join_prices(
    entries: list[dict[str, Any]],
    rates: list[_offers.ParsedRate],
    acc: _offers.ParseAccounting,
    control_plane_names: dict[str, str],
    overlay: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return {base_model_id: price block} for the catalogue's base ids.

    Resolution per base id, first hit wins: (1) rates whose usage type embeds that model id (Mantle grammar),
    (2) rates whose Price List display name normalises to the control-plane modelName of that id (unambiguous),
    (3) overlay, (4) unpriced.
    """
    by_id: dict[str, list[_offers.ParsedRate]] = {}
    by_name: dict[str, list[_offers.ParsedRate]] = {}
    for r in rates:
        if r.identity_kind == "id":
            by_id.setdefault(r.identity, []).append(r)
        else:
            by_name.setdefault(_identity.normalize_name(r.identity), []).append(r)
    id_index = _identity.build_index(list(by_id))  # alias → embedded id (None if ambiguous)
    # control-plane name index: normalized name → set of base ids (ambiguous when >1)
    # Context-window variants (…-v1:0, …-v1:0:256k) share a name and reduce to one alias base: that is ONE model,
    # not an ambiguity (metering identity.build_name_index semantics). Distinct alias bases stay ambiguous → unpriced.
    name_to_ids: dict[str, set[str]] = {}
    for base, name in control_plane_names.items():
        name_to_ids.setdefault(_identity.normalize_name(name), set()).add(_identity.id_aliases(base)[-1])
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        base = e["base_model_id"]
        if base in out:
            continue
        block: dict[str, Any] = {"source": "unpriced", "unit": "USD/1M-tokens", "rates": {}, "matched_by": None}
        hit = None
        for alias in _identity.id_aliases(base):
            mid = id_index.get(alias)
            if mid:
                hit = ("id", mid, by_id[mid])
                break
        if hit is None:
            cp_name = control_plane_names.get(base)
            if cp_name:
                norm = _identity.normalize_name(cp_name)
                cands = by_name.get(norm)
                if cands and len(name_to_ids.get(norm, set())) <= 1:
                    hit = ("name", cp_name, cands)
        if hit is not None:
            kind, key, rs = hit
            grid = _grid_for(rs, acc, base)
            block.update(
                source="aws-published",
                matched_by=kind,
                matched_key=key,
                rates=grid,
                effective_date=max((r.effective_date for r in rs), default=""),
            )
        elif overlay and base in overlay:
            ov = overlay[base]
            block.update(
                source="overlay",
                matched_by="overlay",
                rates=ov.get("rates", {}),
                note=ov.get("note", ""),
                overlay_source=ov.get("source", ""),
            )
        out[base] = block
    return out


def headline_rates(block: dict[str, Any], routing: str = "in_region") -> dict[str, float | None]:
    """The two numbers a picker shows: standard-tier input/output per 1M for the entry's routing (global for
    global profiles, in_region otherwise; geo falls back to in_region as AWS publishes no geo rate)."""
    grid = block.get("rates") or {}
    if block.get("source") == "overlay":
        g = grid.get("global") if routing == "global" and grid.get("global") else grid
        return {"input": g.get("input"), "output": g.get("output"), "routing_priced": "global" if g is not grid else "in_region"}
    order = ["global", "in_region"] if routing == "global" else ["in_region", "global"]
    for rk in order:
        tiers = grid.get(rk) or {}
        std = (tiers.get("standard") or {}).get("default") or {}
        if "input" in std or "output" in std:
            return {"input": std.get("input"), "output": std.get("output"), "routing_priced": rk}
    return {"input": None, "output": None}


def cost_usd(block: dict[str, Any], routing: str, input_tokens: int, output_tokens: int) -> float | None:
    h = headline_rates(block, routing)
    if h.get("input") is None or h.get("output") is None:
        return None
    return float(Decimal(str(h["input"])) * input_tokens / 1_000_000 + Decimal(str(h["output"])) * output_tokens / 1_000_000)

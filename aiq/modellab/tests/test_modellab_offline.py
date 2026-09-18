# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the Model Lab: catalogue reconciliation, price join, matrix offer rules (no AWS calls)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from modellab import catalog as C, matrix as M, pricing as P  # noqa: E402

FM = [
    {
        "modelId": "anthropic.claude-opus-5",
        "modelName": "Claude Opus 5",
        "providerName": "Anthropic",
        "inputModalities": ["TEXT", "IMAGE"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["INFERENCE_PROFILE"],
        "modelLifecycle": {"status": "ACTIVE"},
    },
    {
        "modelId": "amazon.nova-2-lite-v1:0",
        "modelName": "Nova 2 Lite",
        "providerName": "Amazon",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["INFERENCE_PROFILE"],
        "modelLifecycle": {"status": "ACTIVE"},
    },
    {
        "modelId": "amazon.nova-2-lite-v1:0:256k",
        "modelName": "Nova 2 Lite",
        "providerName": "Amazon",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["PROVISIONED"],
        "modelLifecycle": {"status": "ACTIVE"},
    },
    {
        "modelId": "nvidia.nemotron-nano-3-30b",
        "modelName": "Nemotron Nano 3 30B",
        "providerName": "NVIDIA",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["ON_DEMAND"],
        "modelLifecycle": {"status": "ACTIVE"},
    },
    {
        "modelId": "amazon.titan-embed-text-v2:0",
        "modelName": "Titan Text Embeddings V2",
        "providerName": "Amazon",
        "inputModalities": ["TEXT"],
        "outputModalities": ["EMBEDDING"],
        "inferenceTypesSupported": ["ON_DEMAND"],
        "modelLifecycle": {"status": "ACTIVE"},
    },
    {
        "modelId": "amazon.nova-premier-v1:0",
        "modelName": "Nova Premier",
        "providerName": "Amazon",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "inferenceTypesSupported": ["INFERENCE_PROFILE"],
        "modelLifecycle": {"status": "LEGACY"},
    },
]
PROFILES = [
    {
        "inferenceProfileId": "global.anthropic.claude-opus-5",
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-5"}],
    },
    {
        "inferenceProfileId": "us.amazon.nova-2-lite-v1:0",
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-lite-v1:0"}],
    },
    {
        "inferenceProfileId": "us.amazon.nova-premier-v1:0",
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-premier-v1:0"}],
    },
]
MANTLE = {
    "us-east-1": [
        {"id": "anthropic.claude-opus-5", "status": "available", "data_retention": {"mode": "default"}},
        {
            "id": "anthropic.claude-fable-5",
            "status": "unavailable",
            "status_reason": "not available under data retention mode 'default'",
        },
        {"id": "openai.gpt-5.5", "status": "available"},
        {"id": "nvidia.nemotron-nano-3-30b", "status": "available"},
    ]
}


def test_catalog_reconciles_lanes_and_excludes_non_chat():
    entries = C.build_catalog(foundation_models=FM, inference_profiles=PROFILES, mantle_models=MANTLE, region="us-east-1")
    keys = {(e.id, e.lane) for e in entries}
    assert ("global.anthropic.claude-opus-5", "converse") in keys  # profile-only model reachable via its profile
    assert ("anthropic.claude-opus-5", "converse") not in keys  # INFERENCE_PROFILE-only: bare id is not invocable
    assert ("nvidia.nemotron-nano-3-30b", "converse") in keys  # on-demand in-Region id
    assert ("nvidia.nemotron-nano-3-30b", "mantle_chat") in keys and ("nvidia.nemotron-nano-3-30b", "mantle_responses") in keys
    assert ("anthropic.claude-opus-5", "mantle_messages") in keys and ("anthropic.claude-opus-5", "mantle_chat") not in keys
    assert not any(e.id.startswith("amazon.titan-embed") for e in entries)  # embeddings excluded
    assert not any(e.base_model_id == "amazon.nova-premier-v1:0" for e in entries)  # LEGACY excluded
    fable = next(e for e in entries if e.id == "anthropic.claude-fable-5")
    assert fable.mantle_status == "unavailable" and fable.lifecycle == "UNAVAILABLE"
    gpt = next(e for e in entries if e.id == "openai.gpt-5.5" and e.lane == "mantle_chat")
    assert gpt.name.startswith("GPT-5.5") and gpt.provider == "OpenAI"  # humanised name for Mantle-only ids
    nova = next(e for e in entries if e.id == "us.amazon.nova-2-lite-v1:0")
    assert nova.routing == "geo" and nova.name == "Nova 2 Lite" and nova.family == "nova"


def _offer(rows):
    products, terms = {}, {}
    for i, (usagetype, attrs, usd) in enumerate(rows):
        sku = f"SKU{i}"
        products[sku] = {"attributes": {"usagetype": usagetype, "regionCode": "us-east-1", **attrs}}
        terms[sku] = {
            "T": {
                "effectiveDate": "2026-09-01T00:00:00Z",
                "priceDimensions": {"D": {"unit": "1K tokens", "pricePerUnit": {"USD": str(usd)}}},
            }
        }
    return {"version": "v-test", "products": products, "terms": {"OnDemand": terms}}


def test_price_join_by_embedded_id_and_by_name_with_context_variants():
    offers = {
        "AmazonBedrock": _offer(
            [
                (
                    "USE1-nvidia.nemotron-nano-3-30b-mantle-input-tokens-standard",
                    {"model": "Nemotron Nano 3 30B", "provider": "Nvidia"},
                    0.00006,
                ),
                (
                    "USE1-nvidia.nemotron-nano-3-30b-mantle-output-tokens-standard",
                    {"model": "Nemotron Nano 3 30B", "provider": "Nvidia"},
                    0.00024,
                ),
                ("USE1-Nova2.0Lite-input-tokens", {"model": "Nova 2.0 Lite"}, 0.0003),
                ("USE1-Nova2.0Lite-output-tokens", {"model": "Nova 2.0 Lite"}, 0.0025),
                ("USE1-Nova2.0Lite-input-tokens-cross-region-global", {"model": "Nova 2.0 Lite"}, 0.00033),
                ("USE1-Nova2.0Lite-output-tokens-cross-region-global", {"model": "Nova 2.0 Lite"}, 0.00275),
            ]
        )
    }
    rates, acc, versions = P.parse_all(offers, "us-east-1")
    entries = [
        e.to_dict()
        for e in C.build_catalog(foundation_models=FM, inference_profiles=PROFILES, mantle_models=MANTLE, region="us-east-1")
    ]
    names = {
        "anthropic.claude-opus-5": "Claude Opus 5",
        "amazon.nova-2-lite-v1:0": "Nova 2 Lite",
        "amazon.nova-2-lite-v1:0:256k": "Nova 2 Lite",
        "nvidia.nemotron-nano-3-30b": "Nemotron Nano 3 30B",
    }
    prices = P.join_prices(
        entries,
        rates,
        acc,
        names,
        overlay={"anthropic.claude-opus-5": {"rates": {"input": 5.0, "output": 25.0}, "source": "model card", "note": "test"}},
    )
    nem = prices["nvidia.nemotron-nano-3-30b"]
    assert nem["source"] == "aws-published" and nem["matched_by"] == "id"
    assert P.headline_rates(nem, "in_region") == {"input": 0.06, "output": 0.24, "routing_priced": "in_region"}
    nova = prices["amazon.nova-2-lite-v1:0"]
    assert nova["source"] == "aws-published" and nova["matched_by"] == "name"  # 2 context variants, one model
    assert (
        P.headline_rates(nova, "global")["routing_priced"] == "global"
        and P.headline_rates(nova, "geo")["routing_priced"] == "in_region"
    )
    assert prices["anthropic.claude-opus-5"]["source"] == "overlay"
    assert P.cost_usd(nem, "in_region", 1_000_000, 1_000_000) == 0.30


def test_matrix_offer_rules_and_digest():
    entries = [
        e.to_dict()
        for e in C.build_catalog(foundation_models=FM, inference_profiles=PROFILES, mantle_models=MANTLE, region="us-east-1")
    ]
    ok = {"ok": True, "ms": 10, "input_tokens": 5, "output_tokens": 5}
    bad = {"ok": False, "ms": 10, "error_code": "AccessDeniedException", "error_message": "denied"}
    probes = {}
    for e in entries:
        k = f"{e['lane']}::{e['id']}"
        if e["id"] == "global.anthropic.claude-opus-5":
            probes[k] = {p: dict(ok) for p in ("plain", "stream", "tools", "json", "long")}
        elif e["id"] == "nvidia.nemotron-nano-3-30b" and e["lane"] == "converse":
            probes[k] = {"plain": dict(ok), "stream": dict(ok), "tools": dict(bad)}
        elif e["id"] == "openai.gpt-5.5" and e["lane"] == "mantle_responses":
            probes[k] = {"plain": dict(ok), "stream": dict(ok), "tools": dict(ok)}
    roles = {
        "converse::global.anthropic.claude-opus-5": {
            "writer": {"score": 100, "verdict": "suitable"},
            "router": {"score": 33, "verdict": "unsuitable"},
        }
    }
    prices = {e["base_model_id"]: {"source": "unpriced", "rates": {}} for e in entries}
    mx = M.assemble(entries, prices, probes, roles, probed_as="arn:test", region="us-east-1", offer_versions={})
    by = {(x["id"], x["lane"]): x for x in mx["entries"]}
    opus = by[("global.anthropic.claude-opus-5", "converse")]
    assert opus["offered"] and opus["roles_selectable"]["writer"] and not opus["roles_selectable"]["router"]  # score 33 < 40
    nem = by[("nvidia.nemotron-nano-3-30b", "converse")]
    assert (
        nem["offered"] and not nem["roles_selectable"]["writer"] and nem["roles_selectable"]["router"]
    )  # tools failed → no tool roles
    gpt = by[("openai.gpt-5.5", "mantle_responses")]
    assert not gpt["offered"] and "lane:responses-only-not-wired" in gpt["exclusion_reasons"]
    fable = by[("anthropic.claude-fable-5", "mantle_messages")]
    assert (
        not fable["offered"]
        and any(r.startswith("mantle:") for r in fable["exclusion_reasons"])
        and "unprobed" in fable["exclusion_reasons"]
    )
    assert M.verify(mx)
    mx["entries"][0]["offered"] = not mx["entries"][0]["offered"]
    assert not M.verify(mx)  # tampering is detected
    assert M.leaderboard(mx, "writer")[0]["id"] == "global.anthropic.claude-opus-5"
    json.dumps(mx)  # serialisable

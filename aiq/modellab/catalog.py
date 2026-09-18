# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Catalogue reconciliation: ListFoundationModels ∪ ListInferenceProfiles ∪ Mantle ``/v1/models``.

Two disjoint id spaces are reconciled here (verified live 2026-09-18 in the development account):
  * Bedrock Runtime (Converse): ``<vendor>.<model>[-vN:0]`` in-Region ids and ``us.``/``global.`` inference
    profiles (cross-Region). ``INFERENCE_PROFILE``-only models are invocable *only* through a profile.
  * Mantle (OpenAI-compatible plane): bare ``<vendor>.<model>`` ids that differ per Region and carry a
    ``status``/``data_retention`` block. GPT-5.x ids exist only here; ``us./global.openai.*`` only on Converse.

A catalogue *entry* is one invocable id on one lane. Lanes:
  converse          Bedrock Runtime Converse/ConverseStream (boto3, SigV4)
  mantle_chat       Mantle ``/v1/chat/completions`` (bearer = short-term Bedrock API key)
  mantle_responses  Mantle ``/v1/responses``
  mantle_messages   Mantle ``/anthropic/v1/messages`` (Anthropic Messages API)
The probes decide which lanes actually work for each id; the catalogue only enumerates candidates.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

LANES = ("converse", "mantle_chat", "mantle_responses", "mantle_messages")
MANTLE_ENDPOINT = "https://bedrock-mantle.{region}.api.aws"

# Vendors / name fragments that are never chat candidates (embeddings, rerankers, image, video, speech-only).
_NON_CHAT_NAME_RE = re.compile(
    r"embed|rerank|canvas|reel|sonic|marengo|pegasus|stable-|titan-image|titan-embed|"
    r"voxtral|vision-7b|safeguard|coder",
    re.IGNORECASE,
)
_FAMILY_RULES: tuple[tuple[str, str], ...] = (
    (r"claude", "claude"),
    (r"nova", "nova"),
    (r"titan", "titan"),
    (r"gpt-oss", "gpt-oss"),
    (r"gpt-", "gpt"),
    (r"nemotron", "nemotron"),
    (r"llama", "llama"),
    (r"mistral|mixtral|ministral|magistral|devstral|pixtral", "mistral"),
    (r"qwen", "qwen"),
    (r"deepseek", "deepseek"),
    (r"kimi", "kimi"),
    (r"glm", "glm"),
    (r"gemma", "gemma"),
    (r"minimax", "minimax"),
    (r"grok", "grok"),
    (r"palmyra", "palmyra"),
    (r"jamba", "jamba"),
    (r"command", "command"),
)


@dataclass
class CatalogEntry:
    id: str  # invocable id on this lane
    lane: str
    base_model_id: str  # vendor.model id without routing prefix
    name: str  # human-readable
    provider: str
    family: str
    region: str
    routing: str  # in_region | geo | global
    modalities_in: list[str] = field(default_factory=list)
    streaming: bool | None = None
    inference_types: list[str] = field(default_factory=list)
    lifecycle: str | None = None
    listed_by: list[str] = field(default_factory=list)  # list_foundation_models | inference_profiles | mantle
    mantle_status: str | None = None
    mantle_status_reason: str | None = None
    data_retention: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def family_of(model_id: str, name: str = "") -> str:
    hay = f"{model_id} {name}".lower()
    for pattern, fam in _FAMILY_RULES:
        if re.search(pattern, hay):
            return fam
    return model_id.split(".", 1)[0]


def _base_id(invocable: str) -> tuple[str, str]:
    """('vendor.model', routing) — peel exactly one routing scope (global./us./eu./apac./...)."""
    head, dot, rest = invocable.partition(".")
    if dot and "." in rest:
        if head == "global":
            return rest, "global"
        if head in {"us", "eu", "apac", "ap", "ca", "sa", "jp", "au"}:
            return rest, "geo"
    return invocable, "in_region"


def humanize(model_id: str) -> str:
    """Best-effort human name for ids absent from ListFoundationModels (Mantle-only ids)."""
    base, _ = _base_id(model_id)
    vendor, _, rest = base.partition(".")
    rest = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", rest)  # date-stamped snapshot
    words = []
    for tok in re.split(r"[-.]", rest) if not re.match(r"^gpt-\d", rest) else [rest]:
        if not tok:
            continue
        if re.fullmatch(r"\d+b", tok):
            words.append(tok.upper())
        elif re.fullmatch(r"[a-z]\d+[a-z]?", tok):
            words.append(tok.upper())
        else:
            words.append(tok.capitalize())
    name = " ".join(words)
    fixes = {"Gpt": "GPT", "Glm": "GLM", "Vl": "VL", "It": "IT", "Oss": "OSS", "Minimax": "MiniMax", "Moonshotai": ""}
    for k, v in fixes.items():
        name = re.sub(rf"\b{k}\b", v, name)
    if re.match(r"^gpt-", rest):
        name = rest.replace("gpt-", "GPT-").replace("-", " ").replace("GPT ", "GPT-", 1)
        name = re.sub(r"\b(\w)", lambda m: m.group(1).upper(), name)
    vendor_names = {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "amazon": "Amazon",
        "meta": "Meta",
        "mistral": "Mistral AI",
        "qwen": "Qwen",
        "deepseek": "DeepSeek",
        "moonshotai": "Moonshot AI",
        "moonshot": "Moonshot AI",
        "zai": "Z.AI",
        "google": "Google",
        "minimax": "MiniMax",
        "nvidia": "NVIDIA",
        "xai": "xAI",
        "writer": "Writer",
        "cohere": "Cohere",
        "ai21": "AI21 Labs",
        "stability": "Stability AI",
        "twelvelabs": "TwelveLabs",
    }
    return name.strip() or base, vendor_names.get(vendor, vendor)


def is_chat_candidate(summary: dict[str, Any]) -> bool:
    if "TEXT" not in (summary.get("outputModalities") or []):
        return False
    if "TEXT" not in (summary.get("inputModalities") or []):
        return False
    if (summary.get("modelLifecycle") or {}).get("status") != "ACTIVE":
        return False
    if _NON_CHAT_NAME_RE.search(summary.get("modelId", "")) or _NON_CHAT_NAME_RE.search(summary.get("modelName", "")):
        return False
    types = set(summary.get("inferenceTypesSupported") or [])
    return bool(types & {"ON_DEMAND", "INFERENCE_PROFILE"})


def build_catalog(
    *,
    foundation_models: Iterable[dict[str, Any]],
    inference_profiles: Iterable[dict[str, Any]],
    mantle_models: dict[str, list[dict[str, Any]]],
    region: str,
) -> list[CatalogEntry]:
    """Reconcile the three sources into lane-specific candidate entries.

    ``mantle_models`` maps region → the ``data`` array of that Region's ``/v1/models`` response.
    """
    fm = {m["modelId"]: m for m in foundation_models}
    names: dict[str, tuple[str, str]] = {}  # base id -> (name, provider) from the control plane
    for mid, m in fm.items():
        base, _ = _base_id(mid)
        names.setdefault(base, (m.get("modelName") or base, m.get("providerName") or base.split(".")[0]))
        # also register the un-versioned alias so Mantle ids like anthropic.claude-haiku-4-5 resolve
        stripped = re.sub(r"(-\d{8})?(-v\d+)?(:\d+.*)?$", "", base)
        names.setdefault(stripped, names[base])
    entries: list[CatalogEntry] = []
    seen: set[tuple[str, str]] = set()

    def add(entry: CatalogEntry) -> None:
        key = (entry.id, entry.lane)
        if key not in seen:
            seen.add(key)
            entries.append(entry)

    # Converse lane: in-Region on-demand ids
    for mid, m in fm.items():
        if not is_chat_candidate(m):
            continue
        base, _ = _base_id(mid)
        name, provider = names[base]
        if "ON_DEMAND" in (m.get("inferenceTypesSupported") or []):
            add(
                CatalogEntry(
                    id=mid,
                    lane="converse",
                    base_model_id=base,
                    name=name,
                    provider=provider,
                    family=family_of(base, name),
                    region=region,
                    routing="in_region",
                    modalities_in=list(m.get("inputModalities") or []),
                    streaming=m.get("responseStreamingSupported"),
                    inference_types=list(m.get("inferenceTypesSupported") or []),
                    lifecycle=(m.get("modelLifecycle") or {}).get("status"),
                    listed_by=["list_foundation_models"],
                )
            )
    # Converse lane: cross-Region inference profiles (the only way to reach INFERENCE_PROFILE-only models)
    for p in inference_profiles:
        pid = p.get("inferenceProfileId") or ""
        if p.get("status") != "ACTIVE" or p.get("type") not in (None, "SYSTEM_DEFINED"):
            continue
        model_ids = {x.get("modelArn", "").split("/")[-1] for x in p.get("models") or []}
        base_ids = {mid for mid in model_ids if mid}
        if not base_ids:
            continue
        base = sorted(base_ids)[0]
        m = fm.get(base)
        if m is None or not is_chat_candidate(m):
            continue
        name, provider = names.get(base) or (base, base.split(".")[0])
        _, routing = _base_id(pid)
        add(
            CatalogEntry(
                id=pid,
                lane="converse",
                base_model_id=base,
                name=name,
                provider=provider,
                family=family_of(base, name),
                region=region,
                routing=routing,
                modalities_in=list(m.get("inputModalities") or []),
                streaming=m.get("responseStreamingSupported"),
                inference_types=list(m.get("inferenceTypesSupported") or []),
                lifecycle=(m.get("modelLifecycle") or {}).get("status"),
                listed_by=["inference_profiles", "list_foundation_models"],
            )
        )
    # Mantle lanes
    for mregion, data in mantle_models.items():
        for m in data:
            mid = m.get("id") or ""
            if not mid or _NON_CHAT_NAME_RE.search(mid):
                continue
            base = mid
            if base in names:
                name, provider = names[base]
            else:
                stripped = re.sub(r"(-\d{4}-\d{2}-\d{2})$", "", base)
                name, provider = names.get(stripped) or humanize(base)
            lanes = ["mantle_messages"] if base.startswith("anthropic.") else ["mantle_chat", "mantle_responses"]
            for lane in lanes:
                add(
                    CatalogEntry(
                        id=mid,
                        lane=lane,
                        base_model_id=base,
                        name=name,
                        provider=provider,
                        family=family_of(base, name),
                        region=mregion,
                        routing="in_region",
                        modalities_in=["TEXT"],
                        streaming=None,
                        inference_types=["MANTLE"],
                        lifecycle="ACTIVE" if m.get("status") == "available" else "UNAVAILABLE",
                        listed_by=["mantle"],
                        mantle_status=m.get("status"),
                        mantle_status_reason=m.get("status_reason"),
                        data_retention=m.get("data_retention"),
                    )
                )
    entries.sort(key=lambda e: (e.provider.lower(), e.name.lower(), e.lane, e.routing, e.id))
    return entries


def load_catalog_inputs(paths: dict[str, str]) -> dict[str, Any]:
    """Read the saved control-plane snapshots (evidence files) instead of calling AWS."""
    out: dict[str, Any] = {"mantle": {}}
    with open(paths["foundation_models"], encoding="utf-8") as f:
        out["foundation_models"] = json.load(f)["modelSummaries"]
    with open(paths["inference_profiles"], encoding="utf-8") as f:
        out["inference_profiles"] = json.load(f)["inferenceProfileSummaries"]
    for region, path in (paths.get("mantle") or {}).items():
        with open(path, encoding="utf-8") as f:
            out["mantle"][region] = json.load(f)["data"]
    return out


def fetch_catalog_inputs(region: str, mantle_regions: Iterable[str], session=None) -> dict[str, Any]:
    """Live control-plane snapshot (bedrock:ListFoundationModels, ListInferenceProfiles, Mantle GET /v1/models)."""
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import httpx

    session = session or boto3.Session()
    br = session.client("bedrock", region_name=region)
    fms = br.list_foundation_models()["modelSummaries"]
    profiles: list[dict[str, Any]] = []
    token = None
    while True:
        kw = {"nextToken": token} if token else {}
        resp = br.list_inference_profiles(**kw)
        profiles.extend(resp.get("inferenceProfileSummaries", []))
        token = resp.get("nextToken")
        if not token:
            break
    creds = session.get_credentials().get_frozen_credentials()
    mantle: dict[str, list[dict[str, Any]]] = {}
    for mr in mantle_regions:
        url = MANTLE_ENDPOINT.format(region=mr) + "/v1/models"
        req = AWSRequest(method="GET", url=url, headers={"Accept": "application/json"})
        SigV4Auth(creds, "bedrock", mr).add_auth(req)
        r = httpx.get(url, headers=dict(req.headers), timeout=30)
        r.raise_for_status()
        mantle[mr] = r.json().get("data", [])
    return {"foundation_models": fms, "inference_profiles": profiles, "mantle": mantle}

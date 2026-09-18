# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model Lab at run time: the signed Capability Matrix is the only source of truth for what may be selected.

Resolution order for a role (11-architecture.md §5): request override → tenant preferences (``PREF#models``) →
deployment defaults (``AIQ_MODEL_<ROLE>`` env). Every resolved (model_id, lane) must be *offered* in the matrix and
selectable for that role; otherwise the request fails with a typed ``invalid_request`` naming the model and the
reason. Nothing is substituted silently.

The matrix is read from ``s3://<bucket>/model-lab/matrix/latest.json`` (published by ``aiq/scripts/modellab.py``),
cached for five minutes, and its sha256 digest is verified on load; a matrix that fails verification is refused and the
deployment defaults are the only accepted selection (reported in ``health``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

ROLES = ("router", "clarifier", "shallow", "planner", "researcher", "writer")
MATRIX_KEY = os.environ.get("AIQ_MATRIX_KEY", "model-lab/matrix/latest.json")
_CACHE: dict[str, Any] = {"at": 0.0, "matrix": None, "error": None}
_TTL = 300


def _digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def load_matrix(store, force: bool = False) -> dict[str, Any] | None:
    """Cached, digest-verified matrix (None when absent or invalid; the reason is kept for ``health``)."""
    now = time.monotonic()
    if not force and _CACHE["matrix"] is not None and now - _CACHE["at"] < _TTL:
        return _CACHE["matrix"]
    try:
        raw = store.get_bytes(MATRIX_KEY)
        mx = json.loads(raw)
        if mx.get("digest") != _digest(mx.get("entries") or []):
            raise ValueError("matrix digest mismatch")
        _CACHE.update(at=now, matrix=mx, error=None)
    except Exception as e:  # noqa: BLE001
        _CACHE.update(at=now, error=f"{e.__class__.__name__}: {str(e)[:200]}")
        if _CACHE["matrix"] is None:
            log.warning(json.dumps({"event": "matrix.unavailable", "error": _CACHE["error"]}))
    return _CACHE["matrix"]


def matrix_status() -> dict[str, Any]:
    mx = _CACHE["matrix"]
    return {
        "loaded": mx is not None,
        "generated_at": mx.get("generated_at") if mx else None,
        "digest": (mx.get("digest") or "")[:16] if mx else None,
        "entries": len(mx.get("entries", [])) if mx else 0,
        "offered": (mx.get("summary") or {}).get("offered") if mx else 0,
        "error": _CACHE["error"],
    }


def deployment_defaults() -> dict[str, dict[str, str]]:
    env = os.environ
    return {
        "router": {"model_id": env.get("AIQ_MODEL_ROUTER", ""), "lane": "converse"},
        "clarifier": {"model_id": env.get("AIQ_MODEL_CLARIFIER") or env.get("AIQ_MODEL_SHALLOW", ""), "lane": "converse"},
        "shallow": {"model_id": env.get("AIQ_MODEL_SHALLOW", ""), "lane": "converse"},
        "planner": {"model_id": env.get("AIQ_MODEL_PLANNER", ""), "lane": "converse"},
        "researcher": {"model_id": env.get("AIQ_MODEL_RESEARCHER", ""), "lane": "converse"},
        "writer": {"model_id": env.get("AIQ_MODEL_WRITER", ""), "lane": "converse"},
    }


def find_entry(mx: dict[str, Any] | None, model_id: str, lane: str) -> dict[str, Any] | None:
    for e in (mx or {}).get("entries", []):
        if e.get("id") == model_id and e.get("lane") == lane:
            return e
    return None


def human_name(mx: dict[str, Any] | None, model_id: str, lane: str = "converse") -> str:
    e = find_entry(mx, model_id, lane)
    if e:
        return e["name"]
    for x in (mx or {}).get("entries", []):  # same id on another lane still tells us the name
        if x.get("id") == model_id or x.get("base_model_id") == model_id:
            return x["name"]
    return model_id


def validate(mx: dict[str, Any] | None, models: dict[str, Any]) -> list[dict[str, str]]:
    """Problems for a per-role selection: [] means every choice is offered and selectable for its role."""
    problems: list[dict[str, str]] = []
    if mx is None:
        return [
            {
                "role": r,
                "model_id": str((c or {}).get("model_id") if isinstance(c, dict) else getattr(c, "model_id", "")),
                "reason": "capability matrix unavailable; only deployment defaults are accepted",
            }
            for r, c in models.items()
        ]
    for role, choice in models.items():
        mid = choice.get("model_id") if isinstance(choice, dict) else getattr(choice, "model_id", None)
        lane = (choice.get("lane") if isinstance(choice, dict) else getattr(choice, "lane", None)) or "converse"
        if role not in ROLES:
            problems.append({"role": role, "model_id": str(mid), "reason": "unknown role"})
            continue
        e = find_entry(mx, mid, lane)
        if e is None:
            problems.append({"role": role, "model_id": str(mid), "reason": f"not in the capability matrix on lane {lane}"})
            continue
        if not e.get("offered"):
            problems.append(
                {"role": role, "model_id": mid, "reason": "excluded: " + "; ".join(e.get("exclusion_reasons") or ["not offered"])}
            )
            continue
        if not (e.get("roles_selectable") or {}).get(role, False):
            sc = ((e.get("roles") or {}).get(role) or {}).get("score")
            tools_ok = ((e.get("capabilities") or {}).get("tools") or {}).get("ok")
            why = (
                "role probe below the selectable threshold"
                if sc is not None and sc < 40
                else ("tool use failed on this lane" if tools_ok is False else "not selectable for this role")
            )
            problems.append({"role": role, "model_id": mid, "reason": f"{why} (score {sc})"})
    return problems


def resolve(
    mx: dict[str, Any] | None, request_models: dict[str, Any] | None, prefs: dict[str, Any] | None
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Resolve every role to {model_id, lane, human_name, source}; returns (roles, problems)."""
    defaults = deployment_defaults()
    resolved: dict[str, dict[str, Any]] = {}
    to_check: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        choice = None
        source = "deploy_default"
        if request_models and role in request_models:
            c = request_models[role]
            choice = c if isinstance(c, dict) else c.model_dump()
            source = "session_override"
        elif prefs and role in prefs and isinstance(prefs[role], dict) and prefs[role].get("model_id"):
            choice = dict(prefs[role])
            source = "tenant_preference"
        if choice is None:
            choice = dict(defaults[role])
        resolved[role] = {
            "model_id": choice["model_id"],
            "lane": choice.get("lane", "converse"),
            "source": source,
            "human_name": human_name(mx, choice["model_id"], choice.get("lane", "converse")),
        }
        if source != "deploy_default":
            to_check[role] = choice
    problems = validate(mx, to_check) if to_check else []
    return resolved, problems


def offered_for_role(mx: dict[str, Any] | None, role: str) -> list[dict[str, Any]]:
    out = []
    for e in (mx or {}).get("entries", []):
        if e.get("offered") and (e.get("roles_selectable") or {}).get(role):
            out.append(
                {
                    "id": e["id"],
                    "lane": e["lane"],
                    "name": e["name"],
                    "provider": e["provider"],
                    "score": ((e.get("roles") or {}).get(role) or {}).get("score"),
                    "input_per_1m": (e.get("price") or {}).get("input_per_1m"),
                    "output_per_1m": (e.get("price") or {}).get("output_per_1m"),
                    "price_source": (e.get("price") or {}).get("source"),
                    "ttft_ms": (e.get("latency") or {}).get("ttft_ms"),
                }
            )
    out.sort(key=lambda r: (-(r["score"] or 0), r["output_per_1m"] if r["output_per_1m"] is not None else 1e9))
    return out


def estimate_deep_run(mx: dict[str, Any] | None, roles: dict[str, dict[str, Any]]) -> dict[str, float | None]:
    """Cheap, honest estimate from phase-2 usage shape (~200K input / ~20K output tokens per deep job, 80 % of it in
    planner+researcher, 20 % in the writer) × the matrix prices. Shown *before* a run; actuals replace it after."""
    if mx is None:
        return {"low": None, "high": None, "basis": "matrix unavailable"}

    def rate(role):
        e = find_entry(mx, roles[role]["model_id"], roles[role]["lane"])
        p = (e or {}).get("price") or {}
        return p.get("input_per_1m"), p.get("output_per_1m")

    total = 0.0
    known = True
    for role, share_in, share_out in (("planner", 0.35, 0.25), ("researcher", 0.45, 0.35), ("writer", 0.20, 0.40)):
        i, o = rate(role)
        if i is None or o is None:
            known = False
            continue
        total += i * 200_000 * share_in / 1e6 + o * 20_000 * share_out / 1e6
    if not known and total == 0:
        return {"low": None, "high": None, "basis": "unpriced models"}
    return {
        "low": round(total * 0.6, 3),
        "high": round(total * 1.6, 3),
        "basis": "200K in / 20K out tokens (phase-2 shape)" + ("; some roles unpriced" if not known else ""),
    }


def price_tokens(mx: dict[str, Any] | None, model_id: str, lane: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    e = find_entry(mx, model_id, lane) or next((x for x in (mx or {}).get("entries", []) if x.get("id") == model_id), None)
    p = (e or {}).get("price") or {}
    if p.get("input_per_1m") is None or p.get("output_per_1m") is None:
        return {"usd": None, "source": "unpriced"}
    return {
        "usd": round(p["input_per_1m"] * input_tokens / 1e6 + p["output_per_1m"] * output_tokens / 1e6, 6),
        "source": p.get("source"),
        "input_per_1m": p["input_per_1m"],
        "output_per_1m": p["output_per_1m"],
    }

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Amazon Bedrock Guardrails at the adapter boundary (native replacement for AI-Q's optional
NeMo Guardrails middleware).

Modes (AIQ_GUARDRAIL_MODE, set by the CDK stack):
  * off      — default. No ApplyGuardrail call; nothing in the request path.
  * audit    — every INPUT (research question) and OUTPUT (final report) is assessed; the assessment is
               journaled as a `guardrail` event and metered, but nothing is ever blocked or rewritten.
  * enforce  — a blocked input fails the job with a typed, non-retryable error that names the filter that
               fired; a blocked output replaces the report with the guardrail's message; PII anonymisation
               (if configured) rewrites the report.

Research requests are instruction-shaped by nature, so the stack ships with the PROMPT_ATTACK filter at
NONE unless an operator raises it; every filter strength is a CDK context knob (see 07-operator-runbook).
ApplyGuardrail latency is ~200 ms. Failures of the guardrail service itself are surfaced, never silently
ignored: in enforce mode INPUT fails closed and OUTPUT fails open with a warning; in audit mode both fail open.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)
_CFG = Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=3, read_timeout=15)
MAX_TEXT = 200_000  # ApplyGuardrail accepts large text; keep a bound
MODES = ("off", "audit", "enforce")


@dataclass
class GuardrailResult:
    action: str  # NONE | GUARDRAIL_INTERVENED | MODIFIED | ERROR | DISABLED
    text: str  # what the caller should use: original text unless enforce mode blocked/rewrote it
    reasons: list[str] = field(default_factory=list)
    assessments: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "off"

    @property
    def intervened(self) -> bool:
        """The guardrail *would* block this text (regardless of mode)."""
        return self.action == "GUARDRAIL_INTERVENED"

    @property
    def enforced(self) -> bool:
        return self.mode == "enforce"

    @property
    def blocked(self) -> bool:
        """Blocked for real: intervened AND the deployment enforces the policy."""
        return self.intervened and self.enforced


def mode() -> str:
    m = os.environ.get("AIQ_GUARDRAIL_MODE", "").strip().lower()
    if m in MODES:
        return m
    # Backward compatibility with runtimes deployed before the mode knob: an ID alone meant enforce.
    return "enforce" if os.environ.get("AIQ_GUARDRAIL_ID", "").strip() else "off"


def configured() -> tuple[str, str] | None:
    gid = os.environ.get("AIQ_GUARDRAIL_ID", "").strip()
    ver = os.environ.get("AIQ_GUARDRAIL_VERSION", "DRAFT").strip() or "DRAFT"
    return (gid, ver) if gid else None


def describe() -> dict[str, Any]:
    """For the health endpoint: how the content policy is wired on this runtime."""
    cfg = configured()
    return {"mode": mode(), "guardrail_id": cfg[0] if cfg else None, "guardrail_version": cfg[1] if cfg else None}


def _summarise(assessments: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for a in assessments or []:
        for f in (a.get("contentPolicy") or {}).get("filters", []) or []:
            reasons.append(f"content:{f.get('type')}:{f.get('confidence')}:{f.get('action')}")
        for t in (a.get("topicPolicy") or {}).get("topics", []) or []:
            reasons.append(f"topic:{t.get('name')}:{t.get('action')}")
        for p in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities", []) or []:
            reasons.append(f"pii:{p.get('type')}:{p.get('action')}")
        for w in (a.get("wordPolicy") or {}).get("customWords", []) or []:
            reasons.append(f"word:{w.get('action')}")
    return reasons[:20]


def human_reasons(reasons: list[str]) -> str:
    """'content:PROMPT_ATTACK:HIGH:BLOCKED' → 'prompt attack (HIGH confidence)'; topics/PII named as-is."""
    out: list[str] = []
    for r in reasons:
        parts = r.split(":")
        if parts[0] == "content" and len(parts) >= 3:
            out.append(f"{parts[1].lower().replace('_', ' ')} ({parts[2]} confidence)")
        elif parts[0] == "topic" and len(parts) >= 2:
            out.append(f"topic '{parts[1]}'")
        elif parts[0] == "pii" and len(parts) >= 2:
            out.append(f"PII {parts[1]}")
        else:
            out.append(r)
    return ", ".join(dict.fromkeys(out)) or "unspecified filter"


def apply(text: str, source: str, client=None) -> GuardrailResult:
    """source: 'INPUT' or 'OUTPUT'. Returns the text the caller should use plus the assessment.

    In audit mode the returned text is always the original; `intervened` tells the caller what would
    have happened so it can journal/meter it.
    """
    m = mode()
    cfg = configured()
    if m == "off" or not cfg:
        return GuardrailResult("DISABLED", text, mode=m)
    gid, ver = cfg
    client = client or boto3.client("bedrock-runtime", region_name=os.environ.get("AIQ_REGION"), config=_CFG)
    try:
        resp = client.apply_guardrail(guardrailIdentifier=gid, guardrailVersion=ver, source=source,
                                      content=[{"text": {"text": text[:MAX_TEXT]}}])
    except Exception as e:  # noqa: BLE001
        log.error("guardrail apply failed (%s, mode=%s): %s", source, m, e.__class__.__name__)
        return GuardrailResult("ERROR", text, reasons=[f"apply_failed:{e.__class__.__name__}"], mode=m)
    action = resp.get("action", "NONE")
    assessments = resp.get("assessments", []) or []
    reasons = _summarise(assessments)
    outputs = resp.get("outputs") or []
    joined = " ".join(o.get("text", "") for o in outputs if isinstance(o, dict)).strip()
    new_text = text
    if action == "GUARDRAIL_INTERVENED":
        if m == "enforce":
            base = joined or "This request was blocked by the content policy."
            new_text = f"{base} Flagged: {human_reasons(reasons)}."
        else:
            log.info("guardrail audit: %s would be blocked (%s)", source, ", ".join(reasons)[:300])
    elif joined and source == "OUTPUT" and joined != text[:MAX_TEXT]:
        # ANONYMIZE returns rewritten text without intervening; only applied when enforcing.
        action = "MODIFIED"
        if m == "enforce":
            new_text = joined
    return GuardrailResult(action, new_text, reasons=reasons, assessments=assessments, mode=m)

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Amazon Bedrock Guardrails at the adapter boundary (native replacement for AI-Q's optional
NeMo Guardrails middleware).

Two checkpoints, both opt-in via AIQ_GUARDRAIL_ID / AIQ_GUARDRAIL_VERSION:
  * INPUT  — the user's research question before any model/tool call (prompt-attack, content, topics)
  * OUTPUT — the final report before it is stored/streamed (content filters; PII anonymisation if configured)
A blocked input fails the job with a typed error; a blocked output replaces the report with the
guardrail's message and records the assessment. ApplyGuardrail latency is ~200 ms; failures of the
guardrail service itself are surfaced (fail closed for INPUT, fail open with a warning for OUTPUT —
the report is still delivered but flagged), never silently ignored.
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


@dataclass
class GuardrailResult:
    action: str  # NONE | GUARDRAIL_INTERVENED | ERROR | DISABLED
    text: str
    reasons: list[str] = field(default_factory=list)
    assessments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.action == "GUARDRAIL_INTERVENED"


def configured() -> tuple[str, str] | None:
    gid = os.environ.get("AIQ_GUARDRAIL_ID", "").strip()
    ver = os.environ.get("AIQ_GUARDRAIL_VERSION", "DRAFT").strip() or "DRAFT"
    return (gid, ver) if gid else None


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


def apply(text: str, source: str, client=None) -> GuardrailResult:
    """source: 'INPUT' or 'OUTPUT'. Returns the (possibly rewritten) text and the assessment."""
    cfg = configured()
    if not cfg:
        return GuardrailResult("DISABLED", text)
    gid, ver = cfg
    client = client or boto3.client("bedrock-runtime", region_name=os.environ.get("AIQ_REGION"), config=_CFG)
    try:
        resp = client.apply_guardrail(guardrailIdentifier=gid, guardrailVersion=ver, source=source,
                                      content=[{"text": {"text": text[:MAX_TEXT]}}])
    except Exception as e:  # noqa: BLE001
        log.error("guardrail apply failed (%s): %s", source, e.__class__.__name__)
        return GuardrailResult("ERROR", text, reasons=[f"apply_failed:{e.__class__.__name__}"])
    action = resp.get("action", "NONE")
    assessments = resp.get("assessments", []) or []
    outputs = resp.get("outputs") or []
    new_text = text
    if action == "GUARDRAIL_INTERVENED":
        new_text = " ".join(o.get("text", "") for o in outputs if isinstance(o, dict)).strip() or \
            "This request was blocked by the content policy."
    elif outputs and source == "OUTPUT":
        # ANONYMIZE returns rewritten text without intervening
        joined = " ".join(o.get("text", "") for o in outputs if isinstance(o, dict)).strip()
        if joined and joined != text[:MAX_TEXT]:
            new_text = joined
            action = "MODIFIED"
    return GuardrailResult(action, new_text, reasons=_summarise(assessments), assessments=assessments)

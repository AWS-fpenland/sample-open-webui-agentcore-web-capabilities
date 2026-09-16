# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudWatch Embedded Metric Format (EMF) records for job outcomes — written to stdout, picked up by
the runtime's log group, no SDK call and no extra IAM. Namespace AIQ/AgentCore; dimensions kept low
cardinality (RunId, Mode, Outcome) so the series count stays bounded."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

NAMESPACE = "AIQ/AgentCore"


def emit_job_metrics(*, mode: str, outcome: str, seconds: float, usage: dict[str, Any] | None,
                     citations_verified: int = 0, citations_unverified: int = 0, sources: int = 0) -> None:
    u = usage or {}
    metrics = {
        "JobCount": 1,
        "JobSeconds": round(float(seconds), 3),
        "InputTokens": int(u.get("input_tokens", 0) or 0),
        "OutputTokens": int(u.get("output_tokens", 0) or 0),
        "LlmCalls": int(u.get("llm_calls", 0) or 0),
        "Searches": int(u.get("searches", 0) or 0),
        "PagesFetched": int(u.get("pages", 0) or 0),
        "DocumentRetrievals": int(u.get("retrievals", 0) or 0),
        "CitationsVerified": int(citations_verified),
        "CitationsUnverified": int(citations_unverified),
        "SourcesRetrieved": int(sources),
    }
    units = {"JobCount": "Count", "JobSeconds": "Seconds"}
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["RunId", "Mode", "Outcome"]],
                "Metrics": [{"Name": k, "Unit": units.get(k, "Count")} for k in metrics],
            }],
        },
        "RunId": os.environ.get("AIQ_RUN_ID", "unknown"),
        "Mode": mode,
        "Outcome": outcome,
        **metrics,
    }
    _write(record)


def emit_guardrail_metric(*, source: str, action: str, mode: str) -> None:
    """One record per non-trivial guardrail assessment (GUARDRAIL_INTERVENED / MODIFIED / ERROR) so operators can
    see what an `audit`-mode policy *would* block before switching it to `enforce`."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["RunId", "GuardrailMode", "Source", "Action"]],
                "Metrics": [{"Name": "GuardrailAssessments", "Unit": "Count"}],
            }],
        },
        "RunId": os.environ.get("AIQ_RUN_ID", "unknown"),
        "GuardrailMode": mode,
        "Source": source,
        "Action": action,
        "GuardrailAssessments": 1,
    }
    _write(record)


def _write(record: dict[str, Any]) -> None:
    # Leading newline: other loggers may leave stdout mid-line, which makes EMF unparsable.
    sys.stdout.write("\n" + json.dumps(record) + "\n")
    sys.stdout.flush()

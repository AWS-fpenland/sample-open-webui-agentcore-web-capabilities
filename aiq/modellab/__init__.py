# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model Lab — an honest, probed catalogue of every Amazon Bedrock model this account can reach.

Pipeline (``aiq/scripts/modellab.py`` drives it):

  catalog  → one entry per *invocable id per lane* (Converse in-Region / cross-Region profiles; Mantle
             chat-completions / responses / messages), human-readable names, providers, families, modalities.
  pricing  → USD per 1M tokens joined from the AWS Price List offer files (via the repository's metering
             ``pricing`` package: four offer-file grammars, alias-safe id joins, never a guess).
  probes   → live capability probes per entry per lane (plain, system prompt, streaming, tool use, JSON,
             long output, reasoning controls) with raw errors; latency and cost of the probes themselves.
  roles    → role-suitability mini-probes (router, clarifier, shallow researcher, planner, researcher, writer,
             chart-skill willingness) scored deterministically.
  matrix   → the signed Capability Matrix the runtime and the picker treat as the only source of truth:
             offered / excluded with reasons, per-role scores, prices, latencies; digest + who probed.

Nothing here is offered that did not pass a live probe on that lane; nothing is substituted silently.
"""

__all__ = ["catalog", "pricing", "probes", "roles", "matrix"]
SCHEMA_VERSION = "aiq-modellab/v1"

<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Captured implementation learnings

[Documentation home](../README.md)

These notes preserve reusable maintainer patterns learned while building the
sample. They are not supported product interfaces or deployment instructions.
Verify every pattern against current source before reusing it.

## Architecture patterns

- [`admin-console-on-existing-cognito-pool.md`](architecture-patterns/admin-console-on-existing-cognito-pool.md)
  — pattern for adding a separately authorized SPA/API to an existing Cognito
  pool.
- [`native-tool-loop-managed-web-capabilities.md`](architecture-patterns/native-tool-loop-managed-web-capabilities.md)
  — lessons on pinned native-loop integration, app-attested identity, managed
  Browser boundaries, attempt quotas and evidence-driven acceptance.

## Integration issues

- [`bedrock-guardrail-prompt-attack-blocks-research-briefs.md`](integration-issues/bedrock-guardrail-prompt-attack-blocks-research-briefs.md)
  — a Bedrock Guardrail PROMPT_ATTACK filter blocked a legitimate, instruction-shaped
  research brief; content policy is now off by default with tunable `off | audit | enforce`
  modes and per-filter strengths (`-c guardrailMode=…`, `-c guardrail<Filter>=…`).
- [`agentcore-code-interpreter-as-aiq-sandbox-provider.md`](integration-issues/agentcore-code-interpreter-as-aiq-sandbox-provider.md)
  — AgentCore Code Interpreter as the AI-Q sandbox provider: entry point must be a class,
  `readFiles`/`writeFiles` reject absolute paths (python base64 shims over `executeCommand`),
  no outbound internet (`SANDBOX` network mode, no pip bootstrap).

## Tooling decisions

- [`aiq-on-bedrock-nemotron-hybrid-model-roles.md`](tooling-decisions/aiq-on-bedrock-nemotron-hybrid-model-roles.md)
  — NVIDIA AI-Q deep research on Bedrock: Nemotron-only role mapping failed three ways;
  the declared hybrid (Nemotron Nano router/shallow, Nova 2 Lite orchestration, Nemotron
  Super writer) completes with fully verified citations; NIM-kwarg shim; `reasoning_effort`.

Current implementations and operator contracts:

- [`../../infra/lib/aiq-stack.ts`](../../infra/lib/aiq-stack.ts) (guardrail modes, model/budget/sandbox context)
- [`../../aiq/runtime/src/aiq_agentcore/guardrails.py`](../../aiq/runtime/src/aiq_agentcore/guardrails.py),
  [`sandbox_agentcore.py`](../../aiq/runtime/src/aiq_agentcore/sandbox_agentcore.py),
  [`bedrock_compat.py`](../../aiq/runtime/src/aiq_agentcore/bedrock_compat.py)
- AI-Q operator runbook and ADRs: research package `research/aiq-agentcore-openwebui-20260915/fable51-79d40d45/` (outside this repo)

- [`../../infra/lib/metering-console.ts`](../../infra/lib/metering-console.ts)
- [`../../infra/lib/metering-stack.ts`](../../infra/lib/metering-stack.ts)
- [Metering guide](../METERING.md)
- [Console maintainer README](../../console/README.md)

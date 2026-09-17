---
title: Running NVIDIA AI-Q deep research on Bedrock — Nemotron-only role mapping fails, a declared hybrid (Nano / Nova 2 Lite / Super) completes with fully verified citations
date: 2026-09-16
category: tooling-decisions
module: aiq-agentcore
problem_type: tooling_decision
component: assistant
severity: high
applies_when:
  - Porting a NIM- or OpenAI-validated LangChain/deepagents agent (AI-Q or similar) to ChatBedrockConverse
  - Assigning Bedrock models per role in a multi-agent research workflow when Claude-class models are unavailable or an NVIDIA lane is required
  - Using NVIDIA Nemotron on Bedrock with reasoning control (reasoning_effort) and 16K output ceilings
  - Choosing between upstream's default model profile and a per-role mapping before trusting deep-research output
tags: [bedrock, nemotron, nova-2-lite, aiq, deep-research, model-roles, chatbedrockconverse, agentcore-runtime]
related_components: [tooling]
---

# Running NVIDIA AI-Q deep research on Bedrock — Nemotron-only role mapping fails, a declared hybrid (Nano / Nova 2 Lite / Super) completes with fully verified citations

## Context

NVIDIA AI-Q's `chat_deepresearcher_agent` is a NeMo Agent Toolkit workflow with six LLM seats: intent router, clarifier, shallow researcher, and a deepagents-style deep researcher (orchestrator/source router/planner → concurrent researchers → writer commit). Upstream validates it only against NIM-hosted models: the shipped profile puts `nvidia/nemotron-3-ultra-550b-a55b` in every deep seat and treats anything else as an unvalidated "custom profile" (`01-aiq-source-baseline.md` lines 186, 925–935). Run `research/aiq-agentcore-openwebui-20260915/fable51-79d40d45/` ported it to AgentCore Runtime with every seat on `_type: aws_bedrock` (`aiq/runtime/configs/aiq_bedrock.yml`).

Account constraints (126458880449, us-east-1): Anthropic models were unusable (agreement `PENDING`/`ERROR`, "You have not filled out the request form" — `evidence/commands/18-bedrock-model-activation.txt`, `23-INCIDENT-claude-model-access.md`); four NVIDIA models had agreements AVAILABLE (`35-nvidia-models-bedrock.txt`); Nova 2 Lite worked (`21-model-probe-admin.txt`). Both `nvidia.nemotron-super-3-120b` and `nvidia.nemotron-nano-3-30b` pass Converse plain, tool-use and `maxTokens 16384` probes (`36-nemotron-converse-probe.txt`).

## Guidance

**1. Do not assume upstream's role mapping transfers to Bedrock Nemotron.** Three Nemotron-only deep configurations failed (06 P3–P5):

- v7, Super (default reasoning) as orchestrator/planner/researchers/writer: the orchestrator spent the run in `ls`/`read_file`/`glob`/`think`/`get_verified_sources` and never delegated (`task`) → `EmptySourceRegistryError` → terminal `error` at 234 s (`40-runtime-smoke-nemotron.txt`).
- v8, all-Nano: delegated, but the writer never committed → `writer_output_not_committed`; deep failed at 24.8 s while shallow on the same model passed 5/5 (`evidence/eval/all-nano-v8/results.md`).
- v9, Super with `reasoning_effort=medium` on planner/researchers/writer: delegated (3 tasks / 3 research batches) but researchers never called the source tool; 204 s to "Deep research produced no verifiable sources" (`super-reasoning-medium-v9/results.md`, `47-super-reasoning-deep-warnings.txt`).

All three failed honestly — upstream's guards refuse uncited or uncommitted output, so no fabricated report reached a user. Isolated probes show both sizes call the `task` delegation tool at every reasoning level (`42-nemotron-reasoning-effort-probe.txt`), so the gap is prompt adherence inside the long orchestrator context tuned for Ultra 550B, not missing tool support. (The `hybrid-nova-orchestrator-v11` failure is unrelated: a sandbox entry-point defect, fixed in 518e7b8.)

**2. Use the hybrid, declared explicitly.** Nano for router/shallow/clarifier, Nova 2 Lite for the deepagents orchestration (orchestrator, source router and planner all bind to `planner_llm`; researchers to `researcher_llm`), Super for the writer:

```
-c modelRouter=nvidia.nemotron-nano-3-30b \
-c modelShallow=nvidia.nemotron-nano-3-30b \
-c modelPlanner=global.amazon.nova-2-lite-v1:0 \
-c modelResearcher=global.amazon.nova-2-lite-v1:0 \
-c modelWriter=nvidia.nemotron-super-3-120b
```

`infra/bin/aiq.ts` → `AIQ_MODEL_*` env (`infra/lib/aiq-stack.ts`) → `${AIQ_MODEL_*}` in `aiq_bedrock.yml`; the clarifier follows `modelShallow`. Changing a model is a new runtime version with no image rebuild, and the live mapping is readable from the runtime env (`50-runtime-v14-final.txt`: planner Nova 2 Lite, writer Super, router Nano). Nothing is substituted silently.

**3. Shim the NIM-only kwargs at the adapter, not in upstream.** AI-Q passes `extra_headers` (relay/runtime.py) and `bind_tools(..., parallel_tool_calls=...)` (shallow_researcher, clarifier). `ChatBedrockConverse._converse_params()` has an explicit signature, so the first model call dies with `TypeError: ... unexpected keyword argument 'extraHeaders'` (`15-aiq-bedrock-compat-finding.md`). `bedrock_compat.py` wraps both once at engine start:

```python
def _converse_params(self, **kwargs):
    if not has_var_kw:
        for key in [k for k in kwargs if k not in accepted]:
            kwargs.pop(key)
            if key not in _WARNED:
                _WARNED.add(key)
                log.warning("bedrock_compat: ignoring unsupported Converse kwarg %r from upstream AI-Q", key)
    return original_params(self, **kwargs)
```

Bedrock Converse always permits parallel tool use; upstream's "exactly one call" intent is enforced by AI-Q's own post-check.

**4. Drive Nemotron reasoning per role — never on the router.** Nemotron on Bedrock takes `additionalModelRequestFields.reasoning_effort` in {none, minimal, low, medium, high, xhigh, max}; the Bedrock-native `reasoningConfig` shape is rejected (`ValidationException: Invalid 'reasoning_effort': unknown variant 'type'`, `41-nemotron-reasoning-probe.txt` case d). `high` returns `reasoningContent` blocks and inflates output (Nano: 60 → 1613 output tokens for the same probe). Plumbing in `engine_aiq.py::_variant_config`:

```python
for role in ("router", "clarifier", "shallow", "planner", "researcher", "writer"):
    effort = os.environ.get(f"AIQ_REASONING_EFFORT_{role.upper()}", "").strip().lower()
    block = cfg.get("llms", {}).get(f"{role}_llm")
    if effort and isinstance(block, dict):
        fields = dict(block.get("additional_model_request_fields") or {})
        fields["reasoning_effort"] = effort
        block["additional_model_request_fields"] = fields
```

Set via `-c reasoningPlanner|reasoningResearcher|reasoningWriter|reasoningShallow|reasoningRouter=none|low|medium|high` (`07-operator-runbook.md`, Phase 2 operations). The intent classifier parses plain text, so leave the router at provider default.

**5. Budget output tokens and surface truncation.** Deep seats default to 16384 (`-c maxTokensDeep`, `-c maxTokensWriter` → `AIQ_MAX_TOKENS_DEEP/WRITER`; NAT's own default is 300). `engine_aiq.py` inspects every `LLM_END` for `stopReason == max_tokens` and emits `warning: "model output truncated by max_tokens (...); raise AIQ_MAX_TOKENS_* or reduce reasoning"`; the `usage` event carries `truncated_outputs`. Truncation is a frequent silent cause of "the model made no tool calls", and reasoning effort eats the same budget.

## Why This Matters

- **It works, measurably.** Hybrid v12 (sandbox off): deep completed in 367 s, 18 sources, 9/1 citations verified/unverified, 4/4 expected topics, 9 searches + 12 pages via AgentCore Browser (`evidence/eval/hybrid-nova-orchestrator-v12-nosandbox/`). v13 (sandbox on): 1106 s, 57 sources, 14/14 verified, 4.58M/135K tokens (06 §Model decision). Final 7-question eval on v14: 6/7 completed and every completed answer had 100 % of its citation markers verified (4/4, 5/5, 8/8, 7/7, 13/13, 23/23); the one failure was the strict-citation policy on a fresh-news question; judge groundedness 3–5 (`evidence/eval/final-hybrid-sandbox/results.md`).
- **Latency justifies the seats.** Over the phase-2 window Super p50 was 7.8 s per call (94 invocations) against Nano 1.67 s and Nova 2 Lite 1.76 s (346 invocations) (`51-bedrock-model-metrics.txt`). Super belongs in the one long-form seat, not in a 40-call orchestration loop.
- **Cost is legible.** Price List (`53-nemotron-pricing-pricelist.txt`): Super standard $0.00015/1K in, $0.00065/1K out; Nano priority input $0.00011/1K (standard row needs pagination). An observed hybrid deep job is ≈ $0.80–1.20 model + $0.08 search + $0.05–0.10 browser, dominated by 1.8–2.4M input tokens of Nova orchestrator/researcher context replay (`cost-model.csv`, last row) — the knobs are `maxTokensDeep`, `fetchMaxPages`, `max_researcher_model_calls: 60`.
- **Failures stayed honest.** Because each configuration died at upstream's guards, `aiq/scripts/eval_run.py` could compare configurations on identical deterministic metrics instead of eyeballing prose.

## When to Apply

- Porting any NIM/OpenAI-validated LangChain agent (AI-Q, deepagents orchestrations) to `ChatBedrockConverse`: expect `extra_headers`, `parallel_tool_calls` and `chat_template_kwargs` rejections; shim or strip them before touching prompts.
- Assigning models per role in a multi-agent Bedrock workflow when Claude is unavailable or an NVIDIA lane is required: probe delegation in isolation, then re-test under the real orchestrator context, and treat long-context prompt adherence as model-specific.
- Nemotron on Bedrock specifically: `reasoning_effort` (not `reasoningConfig`), 16K output ceiling, reasoning inflates output tokens — couple effort with `max_tokens`.
- Not needed for shallow-only deployments (all-Nano cites exactly: 3/3, 5/5, 5/5, 1/1) or accounts where Claude-class models are usable — but re-run the eval before assuming upstream's mapping holds there.

## Examples

Hybrid deploy as run on v14 (`50-runtime-v14-final.txt`):

```
npx cdk -a 'npx ts-node --prefer-ts-exts bin/aiq.ts' deploy -c aiq=on -c runId=<id> \
  -c account=<acct> -c region=us-east-1 -c userPoolId=<pool> -c allowedClients=<client> \
  -c imageDigest=sha256:… \
  -c modelRouter=nvidia.nemotron-nano-3-30b -c modelShallow=nvidia.nemotron-nano-3-30b \
  -c modelPlanner=global.amazon.nova-2-lite-v1:0 -c modelResearcher=global.amazon.nova-2-lite-v1:0 \
  -c modelWriter=nvidia.nemotron-super-3-120b -c sandbox=on -c fetchMaxPages=24
```

Re-trying an all-Nemotron deep seat with explicit reasoning (what P5 did), one flag per role:

```
-c modelPlanner=nvidia.nemotron-super-3-120b -c modelResearcher=nvidia.nemotron-super-3-120b \
-c reasoningPlanner=medium -c reasoningResearcher=medium -c reasoningWriter=medium
```

The shim firing live: `47-super-reasoning-deep-warnings.txt` — `bedrock_compat: ignoring unsupported Converse kwarg 'extraHeaders' from upstream AI-Q`.

Evaluating a configuration: `python aiq/scripts/eval_run.py --run-id … --questions aiq/eval/questions.json --out evidence/eval/<label> --judge-model nvidia.nemotron-super-3-120b` (`07-operator-runbook.md`, Phase 2 operations).

## Related

- `06-validation-and-evaluation.md` — Phase 2 gate table (P1–P6), §Model decision, §Final evaluation run.
- `MORNING-REPORT-phase2.md` §2 NVIDIA models; `05-implementation-and-deployment.md` runtime versions v7–v17; `08-upstream-feature-parity.md` §3 Models.
- `01-aiq-source-baseline.md` §c.2 — NIM assumptions #2 (`chat_template_kwargs`), #3 (`parallel_tool_calls`), #14 (validation boundary); upstream profile lines 925–935.
- Evidence: `evidence/commands/15-aiq-bedrock-compat-finding.md`, `35-`, `36-`, `40-`, `41-`, `42-`, `47-`, `50-`, `51-`, `53-*.txt`; `evidence/eval/{all-nano-v8, super-reasoning-medium-v9, hybrid-nova-orchestrator-v11, hybrid-nova-orchestrator-v12-nosandbox, final-hybrid-sandbox}/results.md`.
- Code (worktree `sample-open-webui-on-aws-with-bedrock`, branch `feat/aiq-agentcore-fable51-79d40d45`): `aiq/runtime/src/aiq_agentcore/bedrock_compat.py`, `engine_aiq.py`, `aiq/runtime/configs/aiq_bedrock.yml`, `infra/bin/aiq.ts`, `infra/lib/aiq-stack.ts`; `07-operator-runbook.md` §Phase 2 operations.
- Sibling learnings: `docs/solutions/integration-issues/bedrock-guardrail-prompt-attack-blocks-research-briefs.md` (content policy defaults), `docs/solutions/integration-issues/agentcore-code-interpreter-as-aiq-sandbox-provider.md` (sandbox file transfer and network constraints).
- Still open: Nemotron-only deep orchestration (orchestrator/researcher prompt tuning), Nano standard-tier price row, a live chart artifact from the Super writer (06 P12).

# AI-Q on AgentCore — runtime container

See `../README.md` for the architecture. This directory is the container image
that runs on Amazon Bedrock AgentCore Runtime. It installs the upstream NVIDIA
AI-Q Blueprint at a pinned commit and adds the `aiq_agentcore` adapter.

## Content policy (optional Bedrock Guardrail)

Off by default: no guardrail resource exists and nothing screens questions or reports. The stack sets
`AIQ_GUARDRAIL_MODE` (`off` | `audit` | `enforce`); `audit` assesses each research question (INPUT) and final
report (OUTPUT), journals a `guardrail` event (`mode`, `enforced`, `reasons`) and emits the `GuardrailAssessments`
EMF series without blocking; `enforce` fails a blocked question with `invalid_request` naming the filter and
withholds a blocked report. Filter strengths are CDK context (`-c guardrailMode=audit -c guardrailPromptAttack=NONE
-c guardrailHate=MEDIUM …`); `PROMPT_ATTACK` defaults to NONE because research briefs are instruction-shaped.
`health` reports `guardrail.mode`. Background and evidence: `docs/solutions/integration-issues/bedrock-guardrail-prompt-attack-blocks-research-briefs.md`.

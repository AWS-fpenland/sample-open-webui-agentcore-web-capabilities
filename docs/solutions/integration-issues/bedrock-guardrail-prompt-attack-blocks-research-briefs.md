---
title: Bedrock Guardrail PROMPT_ATTACK filter blocks legitimate AI-Q research briefs (content policy off by default, tunable off/audit/enforce modes)
date: 2026-09-16
category: integration-issues
module: aiq-agentcore
problem_type: integration_issue
component: assistant
symptoms:
  - Legitimate deep-research brief submitted from Open WebUI fails in about 0.3 s with `AI-Q error (invalid_request)` saying the request was blocked by the content policy and asking the user to rephrase
  - Job journal holds a single `guardrail` event `content:PROMPT_ATTACK:MEDIUM:BLOCKED` at source INPUT followed immediately by a terminal error
  - No model or tool call is made; the job never reaches planning or web search
  - Only imperative, instruction-shaped prompts are blocked (deeply research X and build me a documentation package; you MUST generate a bar chart) even though they contain nothing adversarial
  - The blocked message did not name the filter or confidence that fired, so the operator could not tell which policy knob to tune
root_cause: config_error
resolution_type: code_fix
severity: high
tags: [bedrock-guardrails, prompt-attack, false-positive, content-policy-defaults, agentcore-runtime, cdk-context, audit-mode, open-webui]
related_components: [tooling]
---

# Bedrock Guardrail PROMPT_ATTACK filter blocks legitimate AI-Q research briefs (content policy off by default, tunable off/audit/enforce modes)
## Problem

The AI-Q deep-research runtime on AgentCore shipped with an Amazon Bedrock Guardrail created by default (`infra/lib/aiq-stack.ts`: `if (props.guardrail !== false)`, driven by `guardrail: app.node.tryGetContext('guardrail') !== 'off'` in `infra/bin/aiq.ts`) and applied fail-closed by the runtime to every research question before any model or tool call (`guardrails.apply(req.question, "INPUT")` in `app.py`). The policy carried the `PROMPT_ATTACK` content filter at `InputStrength: 'HIGH'`; in Bedrock Guardrails, HIGH strength blocks MEDIUM- and HIGH-confidence detections. Deep-research briefs are imperative, instruction-shaped text ("deeply research X … build me a documentation package"), which is exactly the shape the prompt-attack classifier treats as an instruction override. The only knob was the all-or-nothing `-c guardrail=off`: no per-filter strength, and no way to observe what the policy would block without blocking real users.

## Symptoms

- In Open WebUI: `**AI-Q error** (invalid_request): This research request was blocked by the content policy. Please rephrase it.` with `_aiq-job:job_4229e304…:failed_`. The job failed 0.3 s after creation (created 18:24:52.816Z, terminal error 18:24:53.158Z; evidence `58-guardrail-user-false-positive.txt`).
- Job journal held exactly two events: `guardrail {"source":"INPUT","action":"GUARDRAIL_INTERVENED","reasons":["content:PROMPT_ATTACK:MEDIUM:BLOCKED"]}` then a terminal `error` with `"guardrail": true, "retryable": false`. No `route`, `tool.call`, or `usage` events — the prompt never reached a model.
- The reason string shows a MEDIUM-confidence detection being blocked: HIGH strength behaving as documented, not a service fault.
- Same signature 15 hours earlier on runtime v15 (evidence `56a-chart-test-guardrail-false-positive.txt`): an operator smoke prompt demanding a chart ("you MUST generate a bar chart PNG … with the chart-generation skill") got the identical `invalid_request` message ~3 s after `job.accepted`.
- The blocked message named no filter, so neither user nor operator could tell from the chat what fired.

## What Didn't Work

1. **Telling users to rephrase.** That is how the 56a false positive was handled: reword the test prompt and move on. It fixed nothing; it moved the problem to the next user, who was the owner with a legitimate account-research brief 15 hours later. A research assistant whose users must learn to avoid imperative sentences is broken.
2. **Keeping `PROMPT_ATTACK=HIGH` as the "strict" default and improving the rephrase hint.** The false-positive rate is a property of the filter against this workload's prompt corpus, not of the wording. Any default that blocks MEDIUM-confidence detections on instruction-shaped input keeps firing.
3. **Flipping the runtime env out of band** (`update-agent-runtime` to drop `AIQ_GUARDRAIL_ID`). Rejected: it drifts from CDK (the next `cdk deploy` re-adds the guardrail), `update-*` APIs are full-replace so an ad-hoc call risks wiping unspecified runtime fields, and it would leave the guardrail resource and the `bedrock:ApplyGuardrail` grant in place.

## Solution

Commit `e5f3797`: content policy off by default; when on, a three-way mode with per-filter strengths set at deploy time.

**CDK (`infra/lib/aiq-stack.ts`, `infra/bin/aiq.ts`).** Before:

```ts
if (props.guardrail !== false) {
  ... FiltersConfig: [
    { Type: 'PROMPT_ATTACK', InputStrength: 'HIGH', OutputStrength: 'NONE' },
    { Type: 'HATE', InputStrength: 'HIGH', OutputStrength: 'HIGH' }, ...
```

After:

```ts
export const GUARDRAIL_MODES = ['off', 'audit', 'enforce'] as const;
export const DEFAULT_GUARDRAIL_FILTERS: Record<GuardrailFilterType, GuardrailStrength> = {
  PROMPT_ATTACK: 'NONE', HATE: 'MEDIUM', INSULTS: 'MEDIUM', SEXUAL: 'MEDIUM', VIOLENCE: 'LOW', MISCONDUCT: 'LOW',
};
...
const guardrailMode: GuardrailMode = props.guardrailMode ?? (props.guardrail === true ? 'enforce' : 'off');
if (!GUARDRAIL_MODES.includes(guardrailMode)) throw new Error(`guardrailMode must be one of ...`);
const guardrailEnv: Record<string, string> = { AIQ_GUARDRAIL_MODE: guardrailMode };
if (guardrailMode !== 'off') {
  const strengths = { ...DEFAULT_GUARDRAIL_FILTERS, ...(props.guardrailFilters ?? {}) };
  // every strength validated against GUARDRAIL_STRENGTHS, else throw
  const filters = (Object.keys(strengths) as GuardrailFilterType[])
    .filter((type) => strengths[type] !== 'NONE')
    .map((type) => ({ Type: type, InputStrength: strengths[type], OutputStrength: type === 'PROMPT_ATTACK' ? 'NONE' : strengths[type] }));
  if (filters.length === 0) throw new Error(`guardrailMode=${guardrailMode} needs at least one filter strength above NONE`);
  // Guardrail + GuardrailVersion, bedrock:ApplyGuardrail grant, AIQ_GUARDRAIL_ID/VERSION env — only inside this block
}
new cdk.CfnOutput(this, 'GuardrailMode', { value: guardrailMode });
```

`bin/aiq.ts` maps `-c guardrailMode=off|audit|enforce` and `-c guardrailPromptAttack|guardrailHate|guardrailInsults|guardrailSexual|guardrailViolence|guardrailMisconduct=NONE|LOW|MEDIUM|HIGH`; legacy `-c guardrail=on` still means `enforce`. `off` creates no `AWS::Bedrock::Guardrail`/`GuardrailVersion`, grants no `bedrock:ApplyGuardrail`, and sets only `AIQ_GUARDRAIL_MODE=off`.

**Runtime (`guardrails.py`, `app.py`, `metrics.py`).** Before, presence of `AIQ_GUARDRAIL_ID` meant enforce and `blocked` was simply `action == "GUARDRAIL_INTERVENED"`. After:

```python
def mode() -> str:
    m = os.environ.get("AIQ_GUARDRAIL_MODE", "").strip().lower()
    if m in MODES: return m
    return "enforce" if os.environ.get("AIQ_GUARDRAIL_ID", "").strip() else "off"  # legacy: ID alone = enforce

class GuardrailResult:
    intervened = property(lambda s: s.action == "GUARDRAIL_INTERVENED")  # would block, regardless of mode
    enforced   = property(lambda s: s.mode == "enforce")
    blocked    = property(lambda s: s.intervened and s.enforced)         # blocked for real

def apply(text, source, client=None) -> GuardrailResult:
    m = mode(); cfg = configured()
    if m == "off" or not cfg:
        return GuardrailResult("DISABLED", text, mode=m)                 # no ApplyGuardrail call at all
    ...
    if action == "GUARDRAIL_INTERVENED":
        if m == "enforce":
            new_text = f"{joined or 'This request was blocked by the content policy.'} Flagged: {human_reasons(reasons)}."
        else:
            log.info("guardrail audit: %s would be blocked (%s)", source, ...)  # text untouched
    elif joined and source == "OUTPUT" and joined != text[:MAX_TEXT]:
        action = "MODIFIED"
        if m == "enforce": new_text = joined                             # PII rewrite only when enforcing
```

`app.py` now fails the job only when `gr_in.blocked or (gr_in.action == "ERROR" and gr_in.enforced)`, rewrites the report only when `gr_out.enforced`, journals every non-trivial assessment as a `guardrail` event carrying `mode`, `enforced`, `reasons`, emits `metrics.emit_guardrail_metric(...)` (EMF metric `GuardrailAssessments`, dimensions `RunId, GuardrailMode, Source, Action`), and adds `guardrail: guardrails.describe()` to `/health`. `human_reasons` turns `content:PROMPT_ATTACK:HIGH:BLOCKED` into `prompt attack (HIGH confidence)`.

**Pipe (`aiq_agentcore_pipe.py` v0.2.0)** renders a `guardrail` event with `enforced: false` as `Content policy (input): guardrail intervened (audit mode, not enforced) · <up to 3 reasons>`.

**Operator recipe** (`07-operator-runbook.md`, "Content policy: turn on, measure, then enforce"):

```bash
# 1) Measure: audit journals what *would* be blocked; nobody is blocked (same image, ~4 min deploy)
deploy.sh … -c guardrailMode=audit                 # + optional -c guardrailHate=HIGH etc.
#    watch CloudWatch AIQ/AgentCore GuardrailAssessments (RunId, GuardrailMode, Source, Action)
#    or journal events "type":"guardrail" → data.mode/enforced/reasons
# 2) Enforce only what audit proved acceptable; PROMPT_ATTACK stays NONE unless the FP rate is acceptable to you
deploy.sh … -c guardrailMode=enforce -c guardrailPromptAttack=NONE -c guardrailViolence=MEDIUM
# 3) Back out: deletes the guardrail resource; jobs never call ApplyGuardrail
deploy.sh … -c guardrailMode=off
```

**Verification (evidence `59-…`, `60-…`).** v16 (existing image, `guardrailMode=off`) deployed 18:57:08Z→18:58:45Z, ~34 min after the report, with `AWS::Bedrock::GuardrailVersion` and `AWS::Bedrock::Guardrail` `DELETE_COMPLETE`. v17 (image `e5f37971ae3b`) deployed 19:05:40Z→19:06:49Z; `get-agent-runtime` shows `version 17, READY, AIQ_GUARDRAIL_MODE=off, AIQ_GUARDRAIL_ID=null`, no guardrail resources left in the account, `/health` → `"guardrail": {"mode": "off", "guardrail_id": null, "guardrail_version": null}`. The owner's exact prompt re-submitted at 19:01:53Z ran as a deep job: 52 sources retrieved, 10/10 citations verified, 199 LLM calls, 11 searches, 24 pages, completed 19:10:20Z (usage.seconds 490.9), 1,810-word report, `guardrail events: 0`. Tests: runtime 51 passed, CDK 8 passed, pipe 10 passed.

## Why This Works

- The false positive was a policy-default problem, not a service bug: `PROMPT_ATTACK` at HIGH blocked a MEDIUM-confidence detection on an imperative brief. Defaulting `PROMPT_ATTACK` to NONE (and omitting NONE filters from `FiltersConfig`) removes the one filter whose signal is anti-correlated with this workload while keeping hate/insults/sexual/violence/misconduct available.
- `off` removes the whole mechanism — no resource, no IAM grant, no env ID, no `apply_guardrail` call — so the request path has nothing to misfire and no ~200 ms to pay. `test_mode_off_skips_the_service_even_with_an_id` proves the runtime stays silent even if an ID leaks into the env.
- `audit` decouples measurement from enforcement. Because `blocked` is `intervened and enforced`, the same assessment yields a journal event and a metric but never a failed job or a rewritten report, so the false-positive rate is measured on real prompts before anyone is blocked.
- Mode and strengths flow CDK context → stack env → runtime: declarative, reviewable, idempotent, and a ~4-minute stack deploy with no image rebuild. Legacy inputs (`guardrail: true`, `AIQ_GUARDRAIL_ID` without a mode) still map to `enforce`, so nothing silently weakens on upgrade.
- When enforcement does fire, the message names the filter and confidence and points at the operator knob, so a future block is diagnosable from the chat transcript alone.

## Prevention

- Content policies for instruction-shaped workloads (research briefs, coding tasks, agent instructions) default to `off` or `audit`, never `enforce`; `PROMPT_ATTACK` defaults to NONE and is raised only on audit evidence.
- Before switching to `enforce`, run the workload's own prompt corpus (smoke prompts plus real user prompts) through `audit` and require zero `GUARDRAIL_INTERVENED` on known-good prompts, gated on the `GuardrailAssessments` metric or `guardrail` journal events.
- Every policy knob is a CDK prop/context value validated at synth (bad mode, bad strength, and all-NONE each throw); never an out-of-band `update-agent-runtime` env edit.
- Keep the CDK tests that assert: default synth has zero `AWS::Bedrock::Guardrail`/`GuardrailVersion`, no `bedrock:ApplyGuardrail` in any IAM policy, and `AIQ_GUARDRAIL_MODE=off`; audit synth omits `PROMPT_ATTACK`; the legacy flag still maps to enforce.
- Blocked messages must name the filter and confidence (`Flagged: prompt attack (HIGH confidence).`) and the remedy path; "please rephrase" alone is a defect.
- `/health` exposes the live policy mode so an operator can confirm what the running runtime does without reading CloudFormation.
- Treat a smoke-test false positive as a bug in the default, not in the test prompt; do not rephrase around it.
- Journal and meter every non-trivial assessment with `mode`/`enforced`/`reasons` in every mode, so enforcement decisions stay auditable after the fact.

## Related Issues

- Evidence `56a-chart-test-guardrail-false-positive.txt`: the earlier operator-side false positive on v15 (chart-generation prompt), same signature, papered over by rephrasing.
- Evidence `58-…`, `59-…`, `60-…`: the owner's report, the retest, and the v16/v17 deploys.
- `07-operator-runbook.md` § "Content policy: turn on, measure, then enforce".
- Memory "ApplyGuardrail PII = OUTPUT only — ANONYMIZE no-ops on INPUT": audit/enforce assess both INPUT and OUTPUT, but PII anonymisation only ever rewrites the OUTPUT report (and now only when enforcing), and `PROMPT_ATTACK` is INPUT-only by service definition (the stack forces its `OutputStrength` to NONE).
- Memory "AWS update-* APIs are full-replace": why the out-of-band env flip was rejected.
- Same store, see also: `../architecture-patterns/native-tool-loop-managed-web-capabilities.md` — default-closed managed capabilities with evidence-driven acceptance; the contrast case (a content filter that must default *off* for instruction-shaped input).
- Same store, siblings from the same run: `agentcore-code-interpreter-as-aiq-sandbox-provider.md`, `../tooling-decisions/aiq-on-bedrock-nemotron-hybrid-model-roles.md`.
- Workspace-root store precedent: `docs/solutions/architecture-patterns/bedrock-mantle-metering-enforcement-verified-landscape.md` — enforcement posture: never platform-default fail-closed; canary both the block path and the capture path.
- Cross-repo precedent (`fleetdeck/docs/solutions/integration-issues/fail-open-modules-need-image-and-liveness-checks.md`): an off / fail-open posture needs a build-time off-state assertion plus a live behavioural proof — exactly the CDK default-off test plus the re-submitted owner prompt here.
- Cross-repo precedent (`fleetdeck/docs/solutions/integration-issues/agentcore-custom-runtime-deploy-gotchas.md`): `update-agent-runtime` is full-replace, which is why the out-of-band env flip was rejected.
- Operator docs live in the research package, not this repo: `research/aiq-agentcore-openwebui-20260915/fable51-79d40d45/07-operator-runbook.md` (§ Content policy) and `04-target-architecture.md` (ADR-11, amended).

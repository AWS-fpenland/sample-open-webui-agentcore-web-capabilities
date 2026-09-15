---
title: "Managed web capabilities inside a pinned native tool loop"
module: agentcore-web-capabilities
date: "2026-09-15"
problem_type: architecture_pattern
component: tooling
severity: high
tags:
  - open-webui
  - agentcore
  - native-tool-loop
  - application-attestation
  - browser-isolation
  - fail-closed
  - live-validation
---

# Managed web capabilities inside a pinned native tool loop

## Context

The reusable pattern is to retain the application's existing streaming tool loop
while adding narrowly authorized managed search and page reading. This is an
experimental explicit-tool canary, **not native Web Search toggle integration**.
Use the [operator contract](../../WEB_CANARY_OPERATIONS.md) for architecture,
deployment, limits and rollback; this note captures the integration traps.

Compatibility is source-specific: unmodified Open WebUI v0.11.3,
[SHA `2a960a59fe1dbbd35282f0556b3666d81102e781`](https://github.com/open-webui/open-webui/tree/2a960a59fe1dbbd35282f0556b3666d81102e781).
Revalidate these assumptions on upgrade rather than matching tool names alone.

## Guidance

### 1. Separate native-loop compatibility from native-toggle coverage

- **Failure → cause:** an external provider does not make every native web path
  identity-aware. At this pin, search can forward a signed user assertion but the
  external loader cannot; the toggle also exposes builtin `fetch_url`, and some
  attachment paths bypass the loader.
- **Fix:** keep global native search off; attach explicit `search_web` and
  `fetch_url` Tools plus a mandatory model-scoped Filter to private canary models.
  Reject unsupported chat attachments; do not claim global ingestion interception.
- **Prevent/test:** inspect the pinned [tool selection](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/utils/tools.py),
  [builtin tools](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/tools/builtin.py)
  and [external search hook](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/retrieval/web/external.py).
  Preserve [Filter rejection tests](../../../pipe/tests/test_agentcore_web_filter.py)
  and [native result roundtrips](../../../pipe/tests/test_web_tool_roundtrip.py).

### 2. Name the identity boundary honestly

- **Failure → cause:** treating a task-role invocation as end-user authentication
  confuses AWS caller identity with application identity.
- **Fix:** Open WebUI injects its authenticated subject into the Tool; Lambda
  accepts that **application attestation** from the IAM-authorized task and checks
  an exact subject allowlist. This is neither the original Cognito token/sub nor
  a per-user AWS role. All code with the task role shares this trust boundary.
- **Prevent/test:** reject body/header identity substitutions and empty allowlists;
  keep both sides default-closed. Review the [Tool](../../../pipe/agentcore_web_tools.py),
  [Lambda boundary](../../../web_capabilities/lambda_handler.py) and
  [authorization tests](../../../web_capabilities/tests/test_lambda_handler.py).

### 3. Exercise the actual serving lifecycle

- **Failure → cause:** a chat helper without a real authenticated socket session,
  or expecting singular `task_id`, does not exercise the UI's background native
  loop. Separately, successful model creation can leave the serving catalog stale.
- **Fix:** connect authenticated Socket.IO, submit the actual session ID with chat
  metadata, consume returned `task_ids`, and observe correlated events through
  completion. After creating models, refresh `/api/models` and require both IDs;
  fail closed by disabling the Tool if refresh fails, preserving created objects.
- **Prevent/test:** check the pinned [chat API](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/main.py)
  and [streaming handler](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/utils/middleware.py).
  The [configurator](../../../scripts/configure-web-canary.py) has
  [refresh/order/failure regressions](../../../pipe/tests/test_configure_web_canary.py);
  those are not substitutes for a real socket/UI acceptance run or restart check.

### 4. Keep resource ownership and capacity gates explicit

- **Failure → cause:** filling a CloudFormation schema gap with a second target
  owner risks orphaned resources when custom-resource creation fails. A pending
  quota-request history entry can also misrepresent already-effective capacity.
- **Fix:** let native `CfnGatewayTarget` own creation/readiness/deletion; a dependent
  provider only pins the supplied target to Web Search `1.2.0`, preserves settings,
  and verifies READY. Its Delete is a no-op. Before adding the isolated Browser
  VPC, compare effective regional VPC quota with current count, not request history.
  Inspect actual retained resources and resolve failed-stack state before retrying;
  never delete unrelated VPCs or relax isolation to obtain capacity.
- **Prevent/test:** retain [version-provider lifecycle tests](../../../gateway/web-search-provisioner/test_index.py)
  and [native ownership assertions](../../../infra/test/web-capabilities.test.ts).
  Review the [provider contract](../../../gateway/web-search-provisioner/index.py)
  and operator preflight; offline synth cannot establish live regional headroom.

### 5. Narrow Browser repairs without weakening the boundary

- **Failure → cause:** an approved page failed because the broker treated every
  `Content-Disposition` as a download. After that repair, an out-of-policy external
  stylesheet still caused the whole extraction to fail.
- **Fix:** accept only explicit `inline` within existing MIME/byte limits and strip
  filename metadata. Reject attachments, unknown dispositions and ambiguous
  duplicate/list headers before body reads. Abort optional out-of-policy stylesheet
  GETs without DNS/HTTP access; disclose omitted resources and partial rendering.
  Keep main navigation, scripts and non-GET requests fail-closed; do not broaden
  the domain allowlist or add public Browser fallback.
- **Prevent/test:** preserve [inline/header regressions](../../../web_capabilities/tests/test_http_fetch.py)
  and [optional-resource, denied-script/method and cleanup tests](../../../web_capabilities/tests/test_brokered_browser.py).
  The [broker implementation](../../../web_capabilities/brokered_browser.py) and
  [isolation assertions](../../../infra/test/web-browser-isolation.test.ts) are
  complementary: verify live routes, SG egress and DNS Firewall fail-closed state.
  Customer VPC controls do **not** constrain microVM loopback or browser-process
  compromise; the managed sandbox remains trusted. Byte/request caps do not bound
  all transport buffers or JavaScript allocations. Curated-page success is not
  proof of a general hostile-site sandbox.

### 6. Count attempts; inspect execution rather than narration

- **Failure → cause:** a completed chat can contain a Tool error; a repeated
  correlation ID can still launch paid work. A combined answer described serial
  search-then-fetch even though calls were parallel, and claimed snippets lacked
  text that the search results actually contained.
- **Fix:** reserve quota before paid work, retain failed/uncertain attempts, and
  re-admit every replay. No reset, refund or automatic retry to make acceptance
  pass. Disclose operator diagnostics outside application quotas separately.
  Compare actual call order, arguments, correlated outputs and source contents;
  distinguish search snippets, static reads and rendered text. Require termination
  evidence even after errors, not merely an attempted stop call.
- **Prevent/test:** retain [replay and paid-call gating tests](../../../web_capabilities/tests/test_lambda_handler.py),
  [uncertain-transaction tests](../../../web_capabilities/tests/test_quota.py) and
  [cleanup failure tests](../../../web_capabilities/tests/test_brokered_browser.py).
  Live acceptance must prove result-dependent serial execution separately and
  compare the same fixture across static/rendered routes and each model lane.

### 7. Publish provenance, not an optimistic status summary

- **Failure → cause:** an earlier private clone is not a GitHub fork; a broker
  authentication failure need not mean the owner-authorized CLI cannot publish.
  A queued workflow or expired local watch is not a failed workflow, and a local
  pass is not a hosted pass.
- **Fix:** verify `gh` identity and create a separate genuine public fork; preserve
  the old private project. Check fork ancestry, visibility, branch and PR head
  before publishing a privacy-scanned history. Track each hosted run's conclusion
  independently; never bypass checks to resolve a queue.
- **Prevent/test:** explain fixture-dependent totals. The original hosted contracts
  reported **1175 passed, 1 skipped, 2 subtests passed**, versus local **1176 passed
  plus 2 subtests**: the optional pinned-upstream Filter source fixture was absent
  on that runner. See the [source-injection test](../../../pipe/tests/test_agentcore_web_filter.py),
  [contract workflow](../../../.github/workflows/web-capabilities.yml) and
  [documentation workflow](../../../.github/workflows/docs.yml). Later fixture
  improvements do not retroactively change an earlier run's evidence.

The contract workflow now sparse-checks out the exact upstream source SHA and
sets test-only `OWUI_TEST_FILTER_SOURCE`. A configured missing fixture fails
instead of skipping; local runs without that setting retain the optional fixture
behavior. This makes the hosted hook contract reproducible without importing or
installing the entire application. The checkout disables credential persistence;
the test setting is not a production environment variable.

## Evidence boundary

At the 2026-09-15 19:03:31 UTC checkpoint, deployed source was `1ddf3b8` and
[upstream PR #5](https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock/pull/5)
was public. Both original hosted workflows had concluded **SUCCESS**; the earlier
documentation queue observation was superseded, not a failure that needed repair.
This historical checkpoint does not certify later edits or deployments.

Observed canary results included search, ordinary fetching, a combined parallel
search/render run, cited UI output and terminated Browser sessions. **Serial
result-dependent search→fetch, Responses-lane Browser execution and restart checks
were still pending**; neither model narration nor successful CI proves them.
Keep live identities, resource/session/request identifiers, credentials and raw
evidence private. Publish source pins, test contracts, bounded observations and
explicit gaps—not the deployment diary.

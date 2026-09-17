---
title: AgentCore Code Interpreter as the AI-Q sandbox provider — entry point must be a class, readFiles/writeFiles reject absolute paths, and the sandbox has no internet
date: 2026-09-16
category: integration-issues
module: aiq-agentcore
problem_type: integration_issue
component: assistant
symptoms:
  - "`build_provider_class() takes 0 positional arguments but 2 were given` on workflow construction, breaking shallow and deep alike (runtime v11)"
  - "Code Interpreter `readFiles` / `writeFiles` return `Invalid file path: potential path traversal detected` for every absolute path; relative spellings hit `File ... not found`"
  - "Artifact harvest silently lossy: `find` lists `/tmp/aiq/<job>/aiq-artifacts/chart.png` but `download_files` returns 0 bytes `file_not_found`"
  - "`pip install tabulate` fails with `No matching distribution found` and `ModuleNotFoundError: No module named 'tabulate'` — the interpreter session has no outbound internet"
  - "Session audit by name finds nothing: sessions are UUID-named, not `job_*`"
root_cause: wrong_api
resolution_type: code_fix
severity: high
tags: [agentcore-code-interpreter, aiq, sandbox-provider, deepagents, entry-points, path-traversal, base64-shim, network-mode]
related_components: [tooling, testing_framework]
---

# AgentCore Code Interpreter as the AI-Q sandbox provider — entry point must be a class, readFiles/writeFiles reject absolute paths, and the sandbox has no internet

## Problem

NVIDIA AI-Q runs its deep-research skills (data-table analysis, chart generation) in a job-scoped sandbox behind a provider-neutral contract; the built-in providers are Modal and OpenShell. `aiq/runtime/src/aiq_agentcore/sandbox_agentcore.py` adds a third, `agentcore_code_interpreter`, backed by Amazon Bedrock AgentCore Code Interpreter (`aws.codeinterpreter.v1`) through the `aiq.sandbox_providers` entry-point group, with no upstream edits. Three upstream assumptions collided with the managed interpreter:

1. Upstream's registry (`upstream/aiq/src/aiq_agent/agents/deep_researcher/sandbox/registry.py`) does `provider_cls = entry_point.load()` and later `provider_cls(config, job_id)` — the entry-point target must be a class.
2. The deepagents `BaseSandbox` contract and the skills use **absolute paths**: the workdir is `<config.workdir>/<job_id>` (`/tmp/aiq/<job>` per `aiq/runtime/configs/aiq_bedrock.yml`), artifacts go in `<workdir>/aiq-artifacts`, and `skills/visualization/chart-generation/SKILL.md` tells the model to "put absolute paths in every command". The interpreter's `readFiles`/`writeFiles` tool actions reject absolute paths outright.
3. Upstream's Modal config pip-installs `tabulate` for `DataFrame.to_markdown()` (`configs/config_domain_routing_and_skills.yml:192`). The interpreter session has no outbound internet, so a pip bootstrap can never succeed — while the provider claimed `network: open`.

A fourth constraint shaped the fix: the public `deep_research_sandbox` model is `extra="forbid"` and unknown `providers.<name>` blocks are dropped silently, so provider settings must arrive as environment variables (`research-tracks/sandbox-provider/sandbox-provider.md`).

## Symptoms

Evidence root: `research/aiq-agentcore-openwebui-20260915/fable51-79d40d45/`.

- **Runtime v11 (image e18445a30856):** workflow construction failed for both shallow and deep with `build_provider_class() takes 0 positional arguments but 2 were given` (`evidence/eval/hybrid-nova-orchestrator-v11/results.json`, two occurrences). The eval gate caught it within a minute; service was restored by redeploying with `-c sandbox=off` (v12) (`06-validation-and-evaluation.md`, "Defect found by the live gate").
- **Path rejection** (`evidence/commands/55-code-interpreter-probe2.txt`): `readFiles /tmp/aiq/x.txt` → `Invalid file path: potential path traversal detected`; `writeFiles` with an absolute path → the same error; relative `tmp/aiq/x.txt` and `x.txt` → `File ... not found`. The shell's cwd is `/opt/amazon/genesis1p-tools/var`, so no relative spelling reaches `/tmp/aiq`. In the first probe (`54-code-interpreter-api-probe.txt`) upload reported success and `find` listed `/tmp/aiq/probe/aiq-artifacts/chart.png`, yet every `download_files` call returned `0 bytes file_not_found` — the artifact harvest was silently lossy.
- **No internet** (`54-...`): `ModuleNotFoundError: No module named 'tabulate'`; `pip install` → `ERROR: No matching distribution found for tabulate`, exit 1; `site-packages` not writable. pandas 2.3.1 and matplotlib 3.9.0 are preinstalled.
- **False-negative session check** (`52-code-interpreter-sessions.txt`: "sessions named after AI-Q jobs: 0"). The provider names the session `self.sandbox_name[:100]`; upstream's `_scoped_name(job_id)` returns the job id unchanged, and on the synchronous path the deepagents runtime uses `str(uuid4())` when it has no async job id (`deepagents_runtime.py:211`). Sessions are UUID-named, so a `job_*` filter never matches.
- Provider log lines (`sandbox.provider.constructed`, `sandbox.execute`) never reached the runtime CloudWatch log group even with `AIQ_LOG_LEVEL=INFO` (`app.py:45`, `logging.basicConfig`) — still open.

## What Didn't Work

- **Entry point → factory.** `pyproject.toml` originally pointed at `aiq_agentcore.sandbox_agentcore:build_provider_class`, chosen so importing the module never required upstream AI-Q. `entry_point.load()` succeeded — so the registry's "a bad plugin must not break built-ins" guard never fired — and the crash surfaced later in `create_sandbox_backend`, taking down shallow and deep alike because the workflow constructs the provider whenever a sandbox is configured.
- **`writeFiles` first, python shim as fallback** (state at commit 518e7b8). `writeFiles` failed on every absolute path, so the fallback was the real path; it passed base64 on argv (argument-length ceiling for large files); and `download_files` still used `readFiles`, which errored on every absolute path and was mapped to `file_not_found`. The unit test `test_upload_uses_writefiles_then_python_fallback` asserted a `writeFiles` success the real service never returns.
- **`network_mode` default `PUBLIC`, YAML `network: open`, default `AIQ_AGENTCORE_CI_PACKAGES=tabulate`.** `_prepare_workspace` ran `pip install` every session, failed with a `log.warning`, and the config misdescribed the real egress policy.

## Solution

Commits 518e7b8 and eb2371a (entry point + build asserts) and 63589ee (shim transfer, network default, journaling).

**1. A real class at module level** (`sandbox_agentcore.py`):

```python
try:
    AgentCoreCodeInterpreterProvider = build_provider_class()
except Exception as _e:  # adapter-only environments (unit tests) lack upstream AI-Q
    AgentCoreCodeInterpreterProvider = None
```

`aiq/runtime/pyproject.toml`: `agentcore_code_interpreter = "aiq_agentcore.sandbox_agentcore:AgentCoreCodeInterpreterProvider"`. `register()` returns `False` when the class is `None`. `aiq/runtime/Dockerfile` (lines 64–65) asserts at build time that the class exists and `isinstance(P, type)`, imports `aiq_agentcore.nat_plugins.sandbox`, and asserts `'agentcore_code_interpreter' in registered_providers()`.

**2. File transfer over `executeCommand`.** Upload:

```python
code = ("import base64,os,sys\np=sys.argv[1]\nos.makedirs(os.path.dirname(p) or '.', exist_ok=True)\n"
        "open(p,'wb').write(base64.b64decode(sys.stdin.read()))\nprint('ok')")
cmd = f"python3 -c {shlex.quote(code)} {shlex.quote(path)} <<'AIQ_B64_EOF'\n{b64}\nAIQ_B64_EOF"
```

Download runs a shim that prints `__AIQ_NOT_FOUND__` or `__AIQ_IS_DIR__` (exit 0) or the file's base64; the caller maps these to deepagents' `file_not_found` / `is_directory`, decodes after `"".join(stdout.split())`, and reports `invalid_path` on a decode failure.

**3. Honest network policy.** `_settings()`:

```python
"network_mode": os.environ.get("AIQ_AGENTCORE_CI_NETWORK_MODE", "SANDBOX").upper(),
"bootstrap_packages": [p for p in os.environ.get("AIQ_AGENTCORE_CI_PACKAGES", "").split(",") if p.strip()],
```

`infra/bin/aiq.ts` defaults the env var to `SANDBOX`; `aiq_bedrock.yml` sets `network: blocked`; `capabilities` derives `supports_network_policy=self._settings["network_mode"] == "SANDBOX"`; pip bootstrap runs only when packages are listed and the mode is not `SANDBOX`.

**4. Execute plumbing.** Commands whose `timeout` exceeds `sync_cap` (default 840 s, under the 15-minute synchronous cap) are wrapped as `timeout 840 bash -c <quoted>`; the boto client's `read_timeout` is `sync_cap + 60`; stdout and stderr are joined without a doubled newline; `exitCode` comes from `structuredContent`, falling back to `isError`.

**5. Proof channel.** `engine_aiq.py` journals a `tool.result` event on `TOOL_END` for `execute`/`write_file`/`task`, and `_create_session` notes a `status` event through the run context. That proved the plumbing on v15 (`56-sandbox-diagnosis-v15.txt`): seq 330 "Sandbox session started (AgentCore Code Interpreter)", seq 468–469 `execute` → `[Command succeeded with exit code 0]`, matching interpreter session `01M2M48ZX8C7Z2GKHB8WK1D03K` created 03:30:56Z.

## Why This Works

- The registry contract is `load()` → `cls(config, job_id)` → `verify_capabilities(config, backend.capabilities)`; a class satisfies all three, and the guarded module-level construction keeps the module importable in adapter-only test environments (`test_sandbox_agentcore.py` uses `pytest.importorskip("deepagents")`).
- The traversal guard lives in the `readFiles`/`writeFiles` tool actions, not in the sandbox filesystem: the probe's `ls` shows a shell-created 3-byte `x.txt` that `readFiles /tmp/aiq/x.txt` then refused, while the base64 shim read it back (`b64 fallback: aGkK`). `executeCommand` runs a real shell as the session user, so `python3` reads and writes any path the shell can. Base64 makes binary PNG/CSV safe across the text channel and survives the `\r\n` the interpreter injects into output; the quoted heredoc avoids shell expansion and argv limits; the exit-0 sentinels keep "missing" and "directory" distinguishable from each other and from a genuine tool error (`isError`).
- Upstream's gate (`capabilities.py:79-84`) raises `CapabilityError` when `network.mode == "blocked"` and the provider lacks `supports_network_policy`. Deriving the flag from the real interpreter network mode means `blocked` runs only when egress truly is blocked, and a `PUBLIC` interpreter paired with `blocked` fails closed instead of pretending.

## Prevention

- Keep the Dockerfile asserts; a wrong entry-point target now fails `docker build`, not the deployment.
- When a "fallback" turns out to be the only path that works, promote it and delete the primary; make the test fake model the real service (it now returns the traversal error for `writeFiles` and never expects success).
- Before enabling a managed sandbox, run the probe pattern from `54`/`55`: session start, absolute and relative `readFiles`/`writeFiles`, `pip`, `site-packages` writability, and a `find` + download round-trip.
- Filter Code Interpreter sessions by `createdAt` window, not by `job_*` name.
- Derive capability flags from actual settings; never hard-code `supports_network_policy=True`.
- Provider-specific settings go in `AIQ_AGENTCORE_CI_*` env vars; the public YAML cannot carry them.
- Keep the eval harness as the deploy gate and `-c sandbox=off` as the kill switch.

## Related Issues

- `06-validation-and-evaluation.md` P12 — PASSED-LIVE (plumbing) / NOT-REACHED (artifact): the writer model only echoed through `execute` and never called the chart skill, so nothing landed in `aiq-artifacts` — model behaviour, not plumbing.
- Open: provider `log.info` lines absent from the runtime log group; the journal is the proof today.
- Open: skills that import `tabulate` (`data-table-analysis/SKILL.md:137`) cannot run until the interpreter image provides it; there is no pip route.
- `research-tracks/sandbox-provider/sandbox-provider.md` — the design track (no config channel, absolute-path contract, 15-minute sync cap).

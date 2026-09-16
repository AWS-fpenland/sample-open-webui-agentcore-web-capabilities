# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AI-Q sandbox provider backed by **Amazon Bedrock AgentCore Code Interpreter**.

Upstream AI-Q runs deep-research *skills* (data-table analysis, lightweight calculation, chart generation)
in a job-scoped sandbox through a provider-neutral contract (`aiq_agent.agents.deep_researcher.sandbox`).
The built-in providers are Modal and OpenShell; this module registers a third one,
``agentcore_code_interpreter``, using the designed extension point (`register_sandbox_provider`) — no
upstream edits.

Mapping (one interpreter session per job):
  _create_session  → StartCodeInterpreterSession(name=job id, sessionTimeoutSeconds)
  execute(cmd)     → InvokeCodeInterpreter executeCommand  (stdout/stderr/exitCode from structuredContent)
  upload_files     → InvokeCodeInterpreter writeFiles      (falls back to a python heredoc via executeCode)
  download_files   → InvokeCodeInterpreter readFiles       (blob or text resources)
  close/terminate  → StopCodeInterpreterSession

Artifacts: AI-Q's ArtifactManager needs the upstream job service; instead this provider harvests
``<workdir>/<job>/aiq-artifacts`` on close (bounded: ≤20 files, ≤10 MB each) into an in-process registry
that the engine publishes to S3 and as ``artifact`` events. Settings come from environment variables
because the public sandbox config forbids provider-specific keys (see research-tracks/sandbox-provider).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 200_000
ARTIFACT_MAX_FILES = 20
ARTIFACT_MAX_BYTES = 10 * 1024 * 1024
ARTIFACT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".csv", ".json", ".md", ".txt", ".pdf")


@dataclass
class HarvestedArtifact:
    name: str
    path: str
    content: bytes
    kind: str


_HARVEST: dict[str, list[HarvestedArtifact]] = {}
_HARVEST_LOCK = threading.Lock()


def take_artifacts(job_id: str) -> list[HarvestedArtifact]:
    with _HARVEST_LOCK:
        return _HARVEST.pop(job_id, [])


def _kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return {"png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "csv": "dataset", "json": "dataset",
            "md": "document", "txt": "text", "pdf": "document"}.get(ext.lstrip("."), "other")


def _settings() -> dict[str, Any]:
    return {
        "identifier": os.environ.get("AIQ_AGENTCORE_CI_IDENTIFIER", "aws.codeinterpreter.v1"),
        "region": (os.environ.get("AIQ_AGENTCORE_CI_REGION") or os.environ.get("AIQ_REGION")
                   or os.environ.get("AWS_REGION", "us-east-1")),
        "network_mode": os.environ.get("AIQ_AGENTCORE_CI_NETWORK_MODE", "SANDBOX").upper(),
        "session_timeout": int(os.environ.get("AIQ_AGENTCORE_CI_SESSION_TIMEOUT_SECONDS", "3600")),
        "sync_cap": int(os.environ.get("AIQ_AGENTCORE_CI_SYNC_EXECUTE_CAP_SECONDS", "840")),
        "bootstrap_packages": [p for p in os.environ.get("AIQ_AGENTCORE_CI_PACKAGES", "").split(",") if p.strip()],
    }


def _collect(stream: Any) -> dict[str, Any]:
    """Drain an InvokeCodeInterpreter event stream into {text, structured, resources, is_error}."""
    texts: list[str] = []
    structured: dict[str, Any] = {}
    resources: list[dict[str, Any]] = []
    is_error = False
    for event in stream or []:
        result = event.get("result") if isinstance(event, dict) else None
        if not result:
            continue
        is_error = is_error or bool(result.get("isError"))
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            structured.update(sc)
        for block in result.get("content") or []:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "resource" and isinstance(block.get("resource"), dict):
                resources.append(block["resource"])
    return {"text": "\n".join(t for t in texts if t), "structured": structured, "resources": resources, "is_error": is_error}


class AgentCoreCodeInterpreterSandbox:
    """deepagents ``BaseSandbox``-shaped session adapter (execute/upload_files/download_files/id/close)."""

    enable_capture_offload = False

    def __init__(self, *, client, identifier: str, session_id: str, sync_cap: int):
        self._client = client
        self._identifier = identifier
        self._session_id = session_id
        self._sync_cap = sync_cap
        self._closed = False

    @property
    def id(self) -> str:
        return self._session_id

    def _invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.invoke_code_interpreter(codeInterpreterIdentifier=self._identifier,
                                                    sessionId=self._session_id, name=name, arguments=arguments)
        return _collect(resp.get("stream"))

    def execute(self, command: str, *, timeout: int | None = None):
        from deepagents.backends.protocol import ExecuteResponse

        if timeout is not None and timeout > self._sync_cap:
            command = f"timeout {int(self._sync_cap)} bash -c {shlex.quote(command)}"
        t0 = time.monotonic()
        res = self._invoke("executeCommand", {"command": command})
        sc = res["structured"]
        stdout = str(sc.get("stdout") or "")
        stderr = str(sc.get("stderr") or "")
        output = stdout if not stderr else (stdout + ("" if (not stdout or stdout.endswith("\n")) else "\n") + stderr)
        if not output:
            output = res["text"]
        exit_code = sc.get("exitCode")
        if exit_code is None:
            exit_code = 1 if res["is_error"] else 0
        truncated = False
        if len(output) > MAX_OUTPUT_CHARS:
            output, truncated = output[:MAX_OUTPUT_CHARS] + "\n…[output truncated]", True
        log.debug(json.dumps({"event": "sandbox.execute", "session": self._session_id, "exit": exit_code,
                              "seconds": round(time.monotonic() - t0, 2)}))
        return ExecuteResponse(output=output, exit_code=int(exit_code), truncated=truncated)

    def upload_files(self, files):
        """Write bytes to absolute paths through a python shim (`writeFiles` only accepts session-relative paths)."""
        from deepagents.backends.protocol import FileUploadResponse

        out = []
        code = ("import base64,os,sys\np=sys.argv[1]\nos.makedirs(os.path.dirname(p) or '.', exist_ok=True)\n"
                "open(p,'wb').write(base64.b64decode(sys.stdin.read()))\nprint('ok')")
        for path, content in files:
            b64 = base64.b64encode(content).decode()
            # stdin via heredoc keeps very large payloads off the argv limit
            cmd = f"python3 -c {shlex.quote(code)} {shlex.quote(path)} <<'AIQ_B64_EOF'\n{b64}\nAIQ_B64_EOF"
            try:
                res = self._invoke("executeCommand", {"command": cmd})
                ok = not res["is_error"] and int(res["structured"].get("exitCode", 0) or 0) == 0
            except Exception as e:  # noqa: BLE001
                log.debug("upload shim failed for %s (%s)", path, e.__class__.__name__)
                ok = False
            out.append(FileUploadResponse(path=path, error=None if ok else "invalid_path"))
        return out

    def download_files(self, paths):
        """Read absolute paths through a python shim that prints base64 (`readFiles` rejects absolute paths)."""
        from deepagents.backends.protocol import FileDownloadResponse

        out = []
        code = ("import base64,os,sys\np=sys.argv[1]\n"
                "if not os.path.exists(p): print('__AIQ_NOT_FOUND__'); sys.exit(0)\n"
                "if os.path.isdir(p): print('__AIQ_IS_DIR__'); sys.exit(0)\n"
                "sys.stdout.write(base64.b64encode(open(p,'rb').read()).decode())")
        for path in paths:
            try:
                res = self._invoke("executeCommand", {"command": f"python3 -c {shlex.quote(code)} {shlex.quote(path)}"})
            except Exception as e:  # noqa: BLE001
                log.debug("download shim failed for %s (%s)", path, e.__class__.__name__)
                out.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                continue
            stdout = (res["structured"].get("stdout") or res["text"] or "").strip()
            if res["is_error"] or "__AIQ_NOT_FOUND__" in stdout:
                out.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            elif "__AIQ_IS_DIR__" in stdout:
                out.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            else:
                try:
                    out.append(FileDownloadResponse(path=path, content=base64.b64decode("".join(stdout.split()))))
                except Exception:  # noqa: BLE001
                    out.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.stop_code_interpreter_session(codeInterpreterIdentifier=self._identifier, sessionId=self._session_id)
        except Exception as e:  # noqa: BLE001 — TTL reclaims the session anyway
            if "ResourceNotFound" not in e.__class__.__name__ and "ResourceNotFound" not in str(e):
                log.warning("stop_code_interpreter_session failed for %s: %s", self._session_id, e.__class__.__name__)


def build_provider_class():
    """Create the provider class lazily so importing this module never requires upstream AI-Q."""
    from aiq_agent.agents.deep_researcher.sandbox.base import SandboxProvider
    from aiq_agent.agents.deep_researcher.sandbox.capabilities import SandboxCapabilities

    class AgentCoreCodeInterpreterProvider(SandboxProvider):
        provider_name = "agentcore_code_interpreter"

        def __init__(self, config, job_id: str) -> None:
            super().__init__(config, job_id)
            self._settings = _settings()  # no AWS call here (compliance: constructor is side-effect free)
            self._client = None
            self._session_ref = None
            log.info(json.dumps({"event": "sandbox.provider.constructed", "job_id": job_id, "workdir": str(self.workdir),
                                 "artifact_dir": str(self.artifact_dir), "identifier": self._settings["identifier"]}))

        def execute(self, command: str, *, timeout: int | None = None):
            log.info(json.dumps({"event": "sandbox.execute", "job_id": self.job_id, "chars": len(command or ""),
                                 "timeout": timeout, "preview": (command or "")[:120]}))
            try:
                result = super().execute(command, timeout=timeout)
            except Exception as e:  # noqa: BLE001 — log then re-raise (deepagents turns it into a tool error)
                log.error(json.dumps({"event": "sandbox.execute.failed", "job_id": self.job_id, "error": repr(e)[:300]}))
                raise
            log.info(json.dumps({"event": "sandbox.execute.done", "job_id": self.job_id, "exit": result.exit_code,
                                 "out_chars": len(result.output or "")}))
            return result

        @property
        def capabilities(self) -> SandboxCapabilities:
            return SandboxCapabilities(
                supports_network_policy=self._settings["network_mode"] == "SANDBOX",
                supports_network_allowlist=False,
                supports_resource_limits=False,
                supports_artifact_download=True,
                supports_cleanup=True,
                supports_terminate=True,
            )

        def is_recoverable_error(self, exc: Exception) -> bool:
            name = exc.__class__.__name__
            resp = getattr(exc, "response", None)
            code = resp.get("Error", {}).get("Code") if isinstance(resp, dict) else None
            return "ResourceNotFound" in name or code == "ResourceNotFoundException"

        def _create_session(self):
            import boto3
            from botocore.config import Config

            s = self._settings
            self._client = boto3.client("bedrock-agentcore", region_name=s["region"],
                                        config=Config(read_timeout=s["sync_cap"] + 60, connect_timeout=10,
                                                      retries={"mode": "standard", "max_attempts": 2}))
            token = str(uuid.uuid5(uuid.NAMESPACE_URL, f"aiq-sandbox:{self.job_id}"))
            resp = self._client.start_code_interpreter_session(
                codeInterpreterIdentifier=s["identifier"], name=self.sandbox_name[:100],
                sessionTimeoutSeconds=max(60, min(int(s["session_timeout"]), 28800)), clientToken=token)
            session = AgentCoreCodeInterpreterSandbox(client=self._client, identifier=s["identifier"],
                                                      session_id=resp["sessionId"], sync_cap=s["sync_cap"])
            self._session_ref = session
            log.info(json.dumps({"event": "sandbox.session.started", "job_id": self.job_id, "session": resp["sessionId"],
                                 "identifier": s["identifier"]}))
            self._emit_event({"type": "sandbox.session", "data": {"status": "started", "session_id": resp["sessionId"],
                                                                  "provider": self.provider_name}})
            try:
                from .run_context import get_run_context

                ctx = get_run_context()
                if ctx:
                    ctx.note("status", {"description": "Sandbox session started (AgentCore Code Interpreter)", "done": False,
                                        "tool": "sandbox"})
            except Exception:  # noqa: BLE001
                pass
            return session

        def _prepare_workspace(self, session) -> None:  # mkdir + best-effort skill dependencies
            super()._prepare_workspace(session)
            pkgs = self._settings["bootstrap_packages"]
            if pkgs and self._settings["network_mode"] != "SANDBOX":
                try:
                    session.execute(f"python3 -m pip install -q {' '.join(shlex.quote(p) for p in pkgs)} 2>&1 | tail -1",
                                    timeout=120)
                except Exception as e:  # noqa: BLE001
                    log.warning("sandbox bootstrap packages failed: %s", e.__class__.__name__)

        def _harvest(self, session) -> None:
            try:
                cmd = f"find {shlex.quote(self.artifact_dir)} -type f 2>/dev/null | head -{ARTIFACT_MAX_FILES}"
                listing = session.execute(cmd, timeout=30)
            except Exception as e:  # noqa: BLE001
                log.debug("artifact listing failed: %s", e.__class__.__name__)
                return
            paths = [p for p in (listing.output or "").splitlines() if p.strip() and p.lower().endswith(ARTIFACT_EXTENSIONS)]
            if not paths:
                return
            found: list[HarvestedArtifact] = []
            for res in session.download_files(paths[:ARTIFACT_MAX_FILES]):
                if res.content and len(res.content) <= ARTIFACT_MAX_BYTES:
                    name = os.path.basename(res.path)
                    found.append(HarvestedArtifact(name=name, path=res.path, content=res.content, kind=_kind(name)))
            if found:
                with _HARVEST_LOCK:
                    _HARVEST.setdefault(self.job_id, []).extend(found)
                log.info(json.dumps({"event": "sandbox.artifacts.harvested", "job_id": self.job_id, "count": len(found)}))

        def close(self) -> None:
            session = self._session_ref
            if session is not None and not getattr(session, "_closed", False):
                self._harvest(session)
            super().close()
            self._emit_event({"type": "sandbox.session", "data": {"status": "closed", "provider": self.provider_name}})

        def _terminate_session(self, session) -> None:
            try:
                session.close()
            finally:
                self._emit_event({"type": "sandbox.session", "data": {"status": "terminated", "provider": self.provider_name}})

    return AgentCoreCodeInterpreterProvider


# Module-level class for the `aiq.sandbox_providers` entry point (the registry instantiates the target as
# `cls(config, job_id)`, so it must be a class, not a factory). None when upstream AI-Q is not installed.
try:
    AgentCoreCodeInterpreterProvider = build_provider_class()
except Exception as _e:  # noqa: BLE001 — adapter-only environments (unit tests) lack upstream AI-Q
    AgentCoreCodeInterpreterProvider = None  # type: ignore[assignment]
    log.debug("AgentCore sandbox provider class unavailable (%s)", _e.__class__.__name__)


def register() -> bool:
    if AgentCoreCodeInterpreterProvider is None:
        return False
    try:
        from aiq_agent.agents.deep_researcher.sandbox.registry import register_sandbox_provider
    except Exception as e:  # noqa: BLE001
        log.debug("sandbox provider not registered (%s)", e.__class__.__name__)
        return False
    register_sandbox_provider("agentcore_code_interpreter", AgentCoreCodeInterpreterProvider)
    return True

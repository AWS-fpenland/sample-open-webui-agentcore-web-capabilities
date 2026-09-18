# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Wire and storage contracts for AI-Q on AgentCore.

Everything the Open WebUI pipe, the AgentCore Runtime entrypoint, and the
durable stores agree on lives here as pure data models. No I/O.

Design rules (see docs/04-target-architecture.md):
- Owner identity is never a request field; it is derived from the verified JWT.
- Events are append-only with a per-job monotonic ``seq``. A cursor is just the
  last ``seq`` a client has seen; replay is ``seq > cursor``.
- Every citation refers to a source that was retrieved during the job; the
  source registry is part of the job record, not model output.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "aiq-agentcore/v1"

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class Principal(BaseModel):
    """Verified caller identity derived from the inbound Cognito access token."""

    model_config = ConfigDict(frozen=True)

    sub: str = Field(min_length=1, max_length=128)
    issuer: str
    client_id: str
    username: str | None = None
    email: str | None = None
    groups: tuple[str, ...] = ()
    token_expires_at: int

    @property
    def tenant_key(self) -> str:
        """Stable, opaque partition key for all per-user data.

        The raw ``sub`` is never used as a storage key so that key material in
        table scans / log lines cannot be joined back to a Cognito identity
        without the issuer.
        """
        digest = hashlib.sha256(f"{self.issuer}|{self.sub}".encode()).hexdigest()
        return f"u_{digest[:32]}"


# ---------------------------------------------------------------------------
# Modes and operations
# ---------------------------------------------------------------------------


class ResearchMode(str, Enum):
    AUTO = "auto"  # AI-Q intent router decides (meta / shallow / deep / clarify)
    SHALLOW = "shallow"  # force the bounded fast path
    DEEP = "deep"  # force the multi-phase deep path (no clarification)
    DEEP_CLARIFY = "deep_clarify"  # clarify → plan → approval → deep


class Op(str, Enum):
    CHAT = "chat"  # synchronous streamed turn (meta/shallow/clarify/router)
    SUBMIT = "submit"  # start an asynchronous deep-research job
    EVENTS = "events"  # replay + tail a job's events from a cursor
    STATUS = "status"  # one-shot job status
    CANCEL = "cancel"  # cooperative cancellation
    APPROVE = "approve"  # approve / revise a clarification plan
    INGEST = "ingest"  # add documents to a collection
    COLLECTIONS = "collections"  # list the caller's collections
    DELETE_COLLECTION = "delete_collection"
    HEALTH = "health"
    # phase 3 — Research Packages, exports, Model Lab, evaluation (11-architecture.md §10)
    PACKAGES_LIST = "packages.list"
    PACKAGES_GET = "packages.get"
    PACKAGES_UPDATE = "packages.update"
    PACKAGES_DELETE = "packages.delete"
    PACKAGES_COMPARE = "packages.compare"
    PACKAGES_RERUN = "packages.rerun"
    EXPORT = "export"
    ARTIFACT_URL = "artifact.url"
    MODELS = "models"
    MODELS_PREFS = "models.prefs"
    MODELS_VALIDATE = "models.validate"
    EVAL = "eval"
    EVAL_LIST = "eval.list"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=200_000)


class DocumentRef(BaseModel):
    """A caller-owned document to ingest. Content arrives base64 or by S3 key
    that the runtime itself previously issued; the pipe never chooses keys."""

    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    content_b64: str | None = Field(default=None, max_length=30_000_000)
    size_bytes: int | None = Field(default=None, ge=0, le=20_000_000)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        v = v.replace("\\", "/").split("/")[-1].strip()
        if not v or v in {".", ".."}:
            raise ValueError("invalid document name")
        return re.sub(r"[^A-Za-z0-9._ -]", "_", v)[:255]


Lane = Literal["converse", "mantle_chat", "mantle_messages"]
ROLES: tuple[str, ...] = ("router", "clarifier", "shallow", "planner", "researcher", "writer")
EXPORT_FORMATS: tuple[str, ...] = ("md", "html", "pdf", "docx", "pptx", "json", "csv", "bibtex", "ris", "csl", "zip")


class ModelChoice(BaseModel):
    """One model for one AI-Q role, on one lane. Validated against the signed Capability Matrix before use."""

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=3, max_length=128, pattern=r"^[a-z0-9][A-Za-z0-9._:\-]*$")
    lane: Lane = "converse"


class InvokeRequest(BaseModel):
    """The single payload shape accepted at ``POST /invocations``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION, alias="schema", validation_alias="schema")
    op: Op
    mode: ResearchMode = ResearchMode.AUTO
    messages: list[ChatMessage] = Field(default_factory=list)
    job_id: str | None = Field(default=None, pattern=r"^job_[a-f0-9]{32}$")
    after: int = Field(default=0, ge=0, description="Event cursor: stream events with seq > after")
    tail: bool = Field(default=True, description="For EVENTS: keep streaming until terminal")
    approval: Literal["approve", "revise", "reject"] | None = None
    revision: str | None = Field(default=None, max_length=8_000)
    collection: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
    data_sources: list[str] | None = Field(
        default=None, max_length=8, description="Explicit AI-Q data-source ids (web_search, documents, news, ...); None = default"
    )
    active_report_job_id: str | None = Field(
        default=None, pattern=r"^job_[a-f0-9]{32}$", description="Completed job whose report this turn asks about or edits"
    )
    documents: list[DocumentRef] = Field(default_factory=list, max_length=20)
    client_request_id: str | None = Field(
        default=None,
        max_length=128,
        description="Idempotency key chosen by the pipe (e.g. OWUI message id). Duplicate SUBMITs return the same job.",
    )
    conversation_id: str | None = Field(default=None, max_length=128, description="OWUI chat id, for audit only")
    # ---- phase 3 fields (all optional; each op validates the ones it needs) ----
    models: dict[str, ModelChoice] | None = Field(default=None, description="Per-role model overrides for this request")
    parent_job_id: str | None = Field(default=None, pattern=r"^job_[a-f0-9]{32}$")
    relation: Literal["rerun", "followup", "eval", "clarified", "edit", "ask"] | None = None
    other_job_id: str | None = Field(default=None, pattern=r"^job_[a-f0-9]{32}$")
    format: str | None = Field(default=None, pattern=r"^[a-z]{2,8}$")
    theme: Literal["dark", "light"] | None = None
    artifact_id: str | None = Field(default=None, pattern=r"^art_[a-f0-9]{32}$")
    tags: list[str] | None = Field(default=None, max_length=32)
    pinned: bool | None = None
    title: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    limit: int | None = Field(default=None, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=512)
    status: str | None = Field(default=None, max_length=32)
    tag: str | None = Field(default=None, max_length=48)
    q: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=32)
    question_ids: list[str] | None = Field(default=None, max_length=50)
    judge: ModelChoice | None = None
    eval_id: str | None = Field(default=None, pattern=r"^eval_[a-f0-9]{16}$")

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out = []
        for t in v:
            t = t.strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,47}", t):
                raise ValueError(f"invalid tag {t!r}")
            out.append(t)
        return list(dict.fromkeys(out))

    @field_validator("models")
    @classmethod
    def _roles(cls, v: dict[str, ModelChoice] | None) -> dict[str, ModelChoice] | None:
        if v is None:
            return None
        bad = [r for r in v if r not in ROLES]
        if bad:
            raise ValueError(f"unknown model role(s): {', '.join(bad)}")
        return v

    @field_validator("messages")
    @classmethod
    def _bounded(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if len(v) > 200:
            raise ValueError("too many messages")
        return v


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLARIFYING = "clarifying"  # waiting for the user to answer / approve
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class Source(BaseModel):
    """A source actually retrieved during the job (web page, search hit, or document chunk)."""

    source_id: str = Field(pattern=r"^src_[a-f0-9]{16}$")
    url: str | None = None
    title: str | None = None
    kind: Literal["web_search", "web_page", "document"] = "web_search"
    retrieved_at: str  # ISO-8601 UTC
    tool: str  # e.g. "agentcore_web_search", "bedrock_knowledge_base"
    snippet: str | None = Field(default=None, max_length=4_000)
    document_key: str | None = None  # S3 key for document sources (owner-scoped)
    content_sha256: str | None = None

    @staticmethod
    def make_id(url_or_key: str) -> str:
        return "src_" + hashlib.sha256(url_or_key.encode()).hexdigest()[:16]


class Citation(BaseModel):
    """A citation as it appears in a report, resolved against the source registry."""

    marker: str  # e.g. "[3]"
    source_id: str | None  # None => could not be tied to a retrieved source
    url: str | None = None
    verified: bool
    match_level: Literal["exact", "normalized", "host_path", "host", "unmatched"] = "unmatched"


class JobRecord(BaseModel):
    job_id: str
    tenant_key: str
    mode: ResearchMode
    status: JobStatus
    question: str = Field(max_length=20_000)
    created_at: str
    updated_at: str
    runtime_session_id: str | None = None
    client_request_id: str | None = None
    conversation_id: str | None = None
    last_seq: int = 0
    cancel_requested: bool = False
    heartbeat_at: str | None = None
    plan: dict[str, Any] | None = None
    report_key: str | None = None  # S3 key of the final report (owner-scoped prefix)
    ledger_key: str | None = None  # S3 key of the citation ledger
    error: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)  # input_tokens, output_tokens, searches, pages, retrievals
    expires_at: int | None = None  # DynamoDB TTL epoch seconds
    # phase 3
    models: dict[str, Any] | None = None  # resolved roles {role: {model_id, lane, human_name, source}}
    parent_job_id: str | None = None
    relation: str | None = None  # root | rerun | followup | eval | clarified
    package_sk: str | None = None  # PKG#<created_at>#<job_id> projection key
    title: str | None = None
    cost_usd: float | None = None
    eval_id: str | None = None


# ---------------------------------------------------------------------------
# Events (the replayable journal)
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    JOB_ACCEPTED = "job.accepted"
    STATUS = "status"  # human-readable progress line
    ROUTE = "route"  # intent router decision (mode chosen, reason)
    PLAN = "plan"  # research plan (deep)
    PLAN_APPROVAL_REQUIRED = "plan.approval_required"
    CLARIFICATION = "clarification"  # clarifier questions for the user
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    SOURCE = "source"  # a Source added to the registry
    DELTA = "delta"  # streamed answer/report text
    REPORT = "report"  # final report (full text or S3 pointer)
    CITATIONS = "citations"  # verification results
    USAGE = "usage"
    WARNING = "warning"
    GUARDRAIL = "guardrail"
    ARTIFACT = "artifact"
    ERROR = "error"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    HEARTBEAT = "heartbeat"
    PACKAGE = "package"  # package summary at terminal state (or after backfill)


class Event(BaseModel):
    """One journal record. ``seq`` is assigned by the store, never by callers."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    seq: int = Field(ge=0)
    ts: str
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None  # dedupes side-effecting events on retry

    def is_terminal(self) -> bool:
        return (
            self.type in {EventType.COMPLETED, EventType.FAILED_ALIAS, EventType.CANCELLED}
            if hasattr(EventType, "FAILED_ALIAS")
            else self.type in {EventType.COMPLETED, EventType.CANCELLED}
            or (self.type == EventType.ERROR and bool(self.data.get("terminal")))
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    TOOL_FAILED = "tool_failed"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class ApiError(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    request_id: str | None = None


def new_job_id(seed: str | None = None) -> str:
    """Job ids are unguessable; when a seed (idempotency key + tenant) is given the
    id is deterministic so duplicate submits collapse."""
    import secrets

    raw = seed.encode() if seed else secrets.token_bytes(32)
    return "job_" + hashlib.sha256(raw).hexdigest()[:32]

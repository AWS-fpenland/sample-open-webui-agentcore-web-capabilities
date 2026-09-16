# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-job context shared between the engine and the NAT tool plugins.

The engine sets a ``RunContext`` in a ContextVar before running the workflow;
tools read the verified tenant, the selected collection, and record every
source they retrieve so the adapter can journal ``source`` events and verify
citations against what was actually fetched (not against model output).
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable

from .contracts import Source

Emit = Callable[[str, dict[str, Any]], None]


@dataclass
class RunContext:
    job_id: str
    tenant_key: str
    collection: str | None = None
    sources: dict[str, Source] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=lambda: {"searches": 0, "pages": 0, "retrievals": 0})
    emit: Emit | None = None  # engine-provided sink for adapter-level events (source, tool.call, ...)
    cancelled: asyncio.Event | None = None
    page_cache: dict[str, str] = field(default_factory=dict)  # normalized URL -> rendered page (per job)

    def record_source(self, src: Source) -> bool:
        new = src.source_id not in self.sources
        self.sources[src.source_id] = src
        if new and self.emit:
            self.emit("source", src.model_dump())
        return new

    def note(self, etype: str, data: dict[str, Any]) -> None:
        if self.emit:
            self.emit(etype, data)


_current: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("aiq_run_context", default=None)


def set_run_context(ctx: RunContext | None) -> contextvars.Token:
    return _current.set(ctx)


def reset_run_context(token: contextvars.Token) -> None:
    _current.reset(token)


def get_run_context() -> RunContext | None:
    return _current.get()

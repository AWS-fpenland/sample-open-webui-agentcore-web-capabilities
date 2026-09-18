# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF export: AgentCore Browser print of the styled HTML when a backend is injected, reportlab otherwise (ADR-26).

The backend is a plain ``async (html) -> pdf_bytes`` callable so the service layer stays free of AWS wiring; the
runtime passes ``functools.partial(print_html_to_pdf, region=...)``. A browser failure or timeout falls back to
reportlab (Track C §3: "reportlab fallback on browser error/timeout") and the returned metadata says which backend
produced the bytes and why, so the export record can be honest about fidelity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .bundle import PackageBundle, as_package
from .mdmodel import Package
from .render_html import render_html
from .render_pdf_reportlab import render_pdf_reportlab

log = logging.getLogger(__name__)

PdfBackend = Callable[[str], Awaitable[bytes]]


async def render_pdf(bundle: PackageBundle | Package, *, theme: str = "dark", pdf_backend: PdfBackend | None = None,
                     font_dir: str | None = None) -> tuple[bytes, dict]:
    """Return (pdf_bytes, meta). ``meta["backend"]`` is ``"browser"`` or ``"reportlab"`` (+ ``fallback_from``/``error``)."""
    pkg = as_package(bundle)
    fallback: dict = {}
    if pdf_backend is not None:
        html = render_html(pkg, theme=theme)
        t0 = time.perf_counter()
        try:
            data = await pdf_backend(html)
            if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(b"%PDF"):
                raise ValueError("pdf backend returned non-PDF bytes")
            return bytes(data), {"backend": "browser", "theme": "print", "html_theme": theme,
                                 "backend_ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001 — any backend failure degrades to the in-process renderer
            log.warning("export pdf: browser backend failed (%s); falling back to reportlab", e.__class__.__name__)
            fallback = {"fallback_from": "browser", "error": f"{e.__class__.__name__}: {e}"[:300]}
    data, meta = await asyncio.to_thread(render_pdf_reportlab, pkg, font_dir)
    return data, {**meta, "theme": "print", **fallback}

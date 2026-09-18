# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Research Package export renderers (ADR-26, 11-architecture.md §8).

One renderer per format, all fed by the same parsed model of the package's ``report.md`` + citation ledger + source
rows (``bundle.py`` → ``mdmodel.Package``):

- ``md``      report of record + YAML front-matter (manifest excerpt)
- ``html``    styled, self-contained HTML with the Workbench design tokens (dark/light, ``@media print`` → light + A4)
- ``pdf``     PRIMARY: AgentCore Browser print of the styled HTML (``render_pdf_browser``); FALLBACK: reportlab
- ``docx``    python-docx (headings, lists, tables, images, superscript citation links → bookmarked Sources)
- ``pptx``    python-pptx summary deck, labelled *derived* on every slide
- ``json``    the package (manifest + report + ledger + sources)
- ``csv``     sources table
- ``bibtex`` / ``ris`` / ``csl``  bibliographic formats from the cited sources
- ``zip``     everything above + ``manifest.json`` + ``artifacts/``

Ported from the measured Track C prototype (research-tracks/phase3-export/proto); behaviour intentionally unchanged.
The library never touches the filesystem or the network except ``print_html_to_pdf`` (an explicit AgentCore Browser
call the caller opts into by injecting it as ``pdf_backend``).
"""

from .bundle import PackageBundle, as_package, load_bundle
from .render_pdf_browser import print_html_to_pdf
from .service import (
    CONTENT_TYPES,
    EXPORTS_VERSION,
    FORMATS,
    RenderedExport,
    export_filename,
    render,
    render_all,
)

__all__ = [
    "CONTENT_TYPES",
    "EXPORTS_VERSION",
    "FORMATS",
    "PackageBundle",
    "RenderedExport",
    "as_package",
    "export_filename",
    "load_bundle",
    "print_html_to_pdf",
    "render",
    "render_all",
]

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""BibTeX / RIS / CSL-JSON / CSV from AI-Q Source records (pure python, no dependencies).

Port of research-tracks/phase3-export/citation_formats.py (validated with bibtexparser 2.0.1, rispy 0.10.0 and the CSL
input schema — see validation-output.txt); behaviour unchanged.

Input: objects with the aiq_agentcore.contracts.Source fields (source_id, url, title, kind, retrieved_at, tool, snippet,
document_key) plus optional ``n`` (report marker number) and ``verified``. Dicts or attribute objects both work.

Mapping decisions (settled here so every export agrees):
- web sources -> BibTeX ``@online``, RIS ``TY  - ELEC``, CSL ``type: webpage``; ``document`` kind -> ``@misc`` /
  ``TY  - GEN`` / ``document``
- access date = ``retrieved_at`` (the time the job actually read the source); no publication date is asserted (we do
  not know it)
- ``organization`` / ``PB`` / ``container-title`` = the URL host (the only author-ish fact we can assert)
- BibTeX key = host-label + retrieval year + disambiguating letter (e.g. ``kane2026``, ``awsdocs2026b``); stable across
  formats via ``id``
- ``note`` / ``N1`` carries provenance: research package id, source_id, marker, verification state, tool
- snippet (<=4000 chars in the model) goes to RIS ``AB`` and CSL ``abstract``, trimmed to 500 chars
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

CSV_COLUMNS = ["n", "source_id", "title", "url", "host", "kind", "retrieved_at", "tool", "verified", "match_level",
               "document_key", "snippet"]

_BIB_ESC = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\~{}",
            "^": r"\^{}"}


def _get(s: Any, k: str, default: Any = None) -> Any:
    if isinstance(s, dict):
        return s.get(k, default)
    return getattr(s, k, default)


def _host(url: str | None) -> str:
    if not url:
        return ""
    h = (urlparse(url).hostname or "").lower()
    return h.removeprefix("www.")


def _host_label(url: str | None) -> str:
    h = _host(url)
    if not h:
        return "doc"
    parts = h.split(".")
    if len(parts) >= 3 and parts[-3] in ("docs", "aws", "blog", "dev", "developer", "developers", "learn", "support"):
        label = parts[-3] + parts[-2]
    else:
        label = parts[-2] if len(parts) >= 2 else parts[0]
    return re.sub(r"[^a-z0-9]", "", label) or "web"


def _date_parts(iso: str | None) -> list[int] | None:
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
    return [int(m.group(1)), int(m.group(2)), int(m.group(3))] if m else None


def _bib_escape(s: str) -> str:
    return "".join(_BIB_ESC.get(ch, ch) for ch in (s or ""))


def _title(s: Any) -> str:
    return _get(s, "title") or _host(_get(s, "url")) or _get(s, "document_key") or _get(s, "source_id") or "Untitled source"


def _provenance(s: Any, package_id: str | None) -> str:
    bits = []
    if package_id:
        bits.append(f"AI-Q research package {package_id}")
    if _get(s, "n") is not None:
        bits.append(f"cited as [{_get(s, 'n')}]")
    bits.append(f"source_id {_get(s, 'source_id')}")
    v = _get(s, "verified")
    if v is not None:
        bits.append("citation verified" if v else "citation unverified")
    if _get(s, "tool"):
        bits.append(f"retrieved via {_get(s, 'tool')}")
    return "; ".join(bits)


def bibtex_keys(sources: Iterable[Any]) -> list[str]:
    """Stable citation keys: host label + retrieval year, disambiguated with a/b/c... when they collide."""
    keys: list[str] = []
    seen: dict[str, int] = {}
    for s in sources:
        dp = _date_parts(_get(s, "retrieved_at"))
        base = _host_label(_get(s, "url")) + (str(dp[0]) if dp else "")
        seen[base] = seen.get(base, 0) + 1
        keys.append(base)
    counts: dict[str, int] = {}
    out = []
    for k in keys:
        if seen[k] > 1:
            counts[k] = counts.get(k, 0) + 1
            out.append(k + chr(ord("a") + counts[k] - 1) if counts[k] <= 26 else f"{k}{counts[k]}")
        else:
            out.append(k)
    return out


def to_bibtex(sources: list[Any], package_id: str | None = None) -> str:
    entries = []
    for s, key in zip(sources, bibtex_keys(sources)):
        kind = _get(s, "kind", "web_search")
        dp = _date_parts(_get(s, "retrieved_at"))
        fields = [("title", _bib_escape(_title(s)))]
        if _get(s, "url"):
            fields.append(("url", _get(s, "url")))
            fields.append(("organization", _bib_escape(_host(_get(s, "url")))))
        if dp:
            fields.append(("urldate", f"{dp[0]:04d}-{dp[1]:02d}-{dp[2]:02d}"))
        if kind == "document" and _get(s, "document_key"):
            fields.append(("howpublished", _bib_escape("Uploaded document " + _get(s, "document_key"))))
        fields.append(("note", _bib_escape(_provenance(s, package_id))))
        etype = "misc" if kind == "document" else "online"
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields)
        entries.append(f"@{etype}{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + "\n"


def to_ris(sources: list[Any], package_id: str | None = None) -> str:
    recs = []
    for s, key in zip(sources, bibtex_keys(sources)):
        kind = _get(s, "kind", "web_search")
        dp = _date_parts(_get(s, "retrieved_at"))
        lines = [f"TY  - {'GEN' if kind == 'document' else 'ELEC'}", f"ID  - {key}", f"TI  - {_title(s)}"]
        if _get(s, "url"):
            lines.append(f"UR  - {_get(s, 'url')}")
            lines.append(f"PB  - {_host(_get(s, 'url'))}")
        if dp:
            lines.append(f"Y2  - {dp[0]:04d}/{dp[1]:02d}/{dp[2]:02d}/")
        snippet = (_get(s, "snippet") or "").strip()
        if snippet:
            lines.append("AB  - " + re.sub(r"\s+", " ", snippet)[:500])
        lines.append("N1  - " + _provenance(s, package_id))
        lines.append("ER  - ")
        recs.append("\n".join(lines))
    return "\n".join(recs) + "\n"


def to_csl_json(sources: list[Any], package_id: str | None = None) -> list[dict]:
    out = []
    for s, key in zip(sources, bibtex_keys(sources)):
        kind = _get(s, "kind", "web_search")
        item: dict[str, Any] = {"id": key, "type": "document" if kind == "document" else "webpage", "title": _title(s)}
        if _get(s, "url"):
            item["URL"] = _get(s, "url")
            item["container-title"] = _host(_get(s, "url"))
        dp = _date_parts(_get(s, "retrieved_at"))
        if dp:
            item["accessed"] = {"date-parts": [dp]}
        if _get(s, "tool"):
            item["source"] = _get(s, "tool")
        snippet = (_get(s, "snippet") or "").strip()
        if snippet:
            item["abstract"] = re.sub(r"\s+", " ", snippet)[:500]
        item["note"] = _provenance(s, package_id)
        if _get(s, "n") is not None:
            item["citation-number"] = str(_get(s, "n"))
        out.append(item)
    return out


def to_csv_rows(sources: list[Any]) -> list[list[Any]]:
    """Rows in ``CSV_COLUMNS`` order (prototype layout; the ``csv`` export uses service.CSV_HEADER instead)."""
    rows = []
    for s in sources:
        rows.append([_get(s, "n"), _get(s, "source_id"), _get(s, "title") or "", _get(s, "url") or "",
                     _host(_get(s, "url")), _get(s, "kind", ""), _get(s, "retrieved_at", ""), _get(s, "tool", ""),
                     _get(s, "verified"), _get(s, "match_level") or "", _get(s, "document_key") or "",
                     re.sub(r"\s+", " ", (_get(s, "snippet") or ""))[:500]])
    return rows

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-tenant document collections on Amazon Bedrock Knowledge Bases (S3 Vectors).

Authoritative documents live in the run's private S3 bucket under
``documents/<tenant_key>/<collection>/<name>`` with a Bedrock metadata sidecar
(``<name>.metadata.json``) carrying ``tenant_key`` and ``collection``. The
Knowledge Base data source covers the ``documents/`` prefix; ingestion is a
Bedrock ingestion job (incremental sync; deleted objects are removed from the
index because the data source uses ``DataDeletionPolicy: DELETE``).

Retrieval always applies a metadata filter on the *verified* ``tenant_key`` (and
optionally ``collection``), so a user can never retrieve another user's chunks —
isolation is enforced server-side by the filter, not by prompt text.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .contracts import DocumentRef, Principal, Source
from .store import JobStore, now_iso

log = logging.getLogger(__name__)
_CFG = Config(retries={"mode": "standard", "max_attempts": 4}, connect_timeout=5, read_timeout=30)
ALLOWED_TYPES = {"text/plain", "text/markdown", "text/csv", "text/html", "application/pdf", "application/json",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream"}
MAX_DOC_BYTES = 20 * 1024 * 1024


def _agent() -> Any:
    return boto3.client("bedrock-agent", region_name=os.environ.get("AIQ_REGION"), config=_CFG)


def _agent_runtime() -> Any:
    return boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AIQ_REGION"), config=_CFG)


def kb_id() -> str:
    return os.environ["AIQ_KB_ID"]


def start_sync() -> dict[str, Any]:
    """Start an incremental ingestion job (idempotent enough: a concurrent job → report it instead)."""
    try:
        resp = _agent().start_ingestion_job(knowledgeBaseId=kb_id(), dataSourceId=os.environ["AIQ_KB_DATA_SOURCE_ID"],
                                            description=f"aiq sync {now_iso()}")
        job = resp["ingestionJob"]
        return {"ingestion_job_id": job["ingestionJobId"], "status": job["status"], "started": True}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ThrottlingException"):
            jobs = _agent().list_ingestion_jobs(knowledgeBaseId=kb_id(), dataSourceId=os.environ["AIQ_KB_DATA_SOURCE_ID"],
                                                maxResults=1, sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"})
            summaries = jobs.get("ingestionJobSummaries", [])
            if summaries:
                return {"ingestion_job_id": summaries[0]["ingestionJobId"], "status": summaries[0]["status"],
                        "started": False, "note": code}
        raise


def sync_status(ingestion_job_id: str) -> dict[str, Any]:
    job = _agent().get_ingestion_job(knowledgeBaseId=kb_id(), dataSourceId=os.environ["AIQ_KB_DATA_SOURCE_ID"],
                                     ingestionJobId=ingestion_job_id)["ingestionJob"]
    stats = job.get("statistics", {})
    return {"ingestion_job_id": ingestion_job_id, "status": job["status"],
            "scanned": stats.get("numberOfDocumentsScanned"), "indexed": stats.get("numberOfNewDocumentsIndexed"),
            "modified": stats.get("numberOfModifiedDocumentsIndexed"), "deleted": stats.get("numberOfDocumentsDeleted"),
            "failed": stats.get("numberOfDocumentsFailed"), "failure_reasons": job.get("failureReasons")}


async def ingest_documents(store: JobStore, principal: Principal, collection: str,
                           documents: list[DocumentRef]) -> AsyncIterator[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    stored: list[dict[str, Any]] = []
    for doc in documents:
        if not doc.content_b64:
            yield {"type": "document.rejected", "seq": 0, "data": {"name": doc.name, "reason": "no content"}}
            continue
        try:
            body = base64.b64decode(doc.content_b64, validate=True)
        except Exception:
            yield {"type": "document.rejected", "seq": 0, "data": {"name": doc.name, "reason": "invalid base64"}}
            continue
        if len(body) > MAX_DOC_BYTES:
            yield {"type": "document.rejected", "seq": 0, "data": {"name": doc.name, "reason": "too large"}}
            continue
        ctype = doc.content_type if doc.content_type in ALLOWED_TYPES else "application/octet-stream"
        key = await loop.run_in_executor(None, lambda d=doc, b=body, c=ctype: store.put_document(
            principal.tenant_key, collection, d.name, b, c, {"uploaded_at": now_iso()}))
        stored.append({"name": doc.name, "size": len(body), "key": key})
        yield {"type": "document.stored", "seq": 0, "data": {"name": doc.name, "size": len(body), "collection": collection}}
    if stored:
        sync = await loop.run_in_executor(None, start_sync)
        yield {"type": "collection.sync", "seq": 0, "data": {"collection": collection, "documents": len(stored), **sync}}


async def wait_for_sync(ingestion_job_id: str, timeout: float = 600, poll: float = 10) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + timeout
    while True:
        st = await loop.run_in_executor(None, sync_status, ingestion_job_id)
        if st["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            return st
        if time.monotonic() > deadline:
            st["timed_out"] = True
            return st
        await asyncio.sleep(poll)


def retrieve(principal_tenant_key: str, query: str, collection: str | None = None, top_k: int = 6) -> list[dict[str, Any]]:
    """Tenant-filtered retrieval. Returns [{text, score, name, collection, key, page}]."""
    tenant_filter: dict[str, Any] = {"equals": {"key": "tenant_key", "value": principal_tenant_key}}
    if collection:
        flt: dict[str, Any] = {"andAll": [tenant_filter, {"equals": {"key": "collection", "value": collection}}]}
    else:
        flt = tenant_filter
    resp = _agent_runtime().retrieve(
        knowledgeBaseId=kb_id(),
        retrievalQuery={"text": query[:1000]},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k, "filter": flt}},
    )
    out = []
    for r in resp.get("retrievalResults", []):
        meta = r.get("metadata", {}) or {}
        loc = (r.get("location", {}) or {}).get("s3Location", {}) or {}
        uri = loc.get("uri", "")
        # Defense in depth: never surface a chunk whose metadata disagrees with the caller's tenant.
        if meta.get("tenant_key") not in (None, principal_tenant_key) or (
            uri and f"documents/{principal_tenant_key}/" not in uri
        ):
            log.error("knowledge: dropped cross-tenant chunk (uri=%s)", uri)
            continue
        out.append({"text": (r.get("content", {}) or {}).get("text", ""), "score": r.get("score"),
                    "name": meta.get("document_name") or uri.rsplit("/", 1)[-1], "collection": meta.get("collection"),
                    "key": uri.split("/", 3)[-1] if uri.startswith("s3://") else uri,
                    "page": meta.get("x-amz-bedrock-kb-document-page-number")})
    return out


def format_for_agent(results: list[dict[str, Any]], tool_name: str = "knowledge_search") -> str:
    """Render retrieval results in the layout AI-Q's citation parser expects for knowledge tools:
    ``Citation:`` (filename + page) and ``Source:`` lines per chunk."""
    if not results:
        return "Search returned no results"
    blocks = []
    for r in results:
        page = f", p.{r['page']}" if r.get("page") else ""
        blocks.append(f"Citation: {r['name']}{page}\nSource: {r['name']} (collection {r.get('collection') or 'default'})\n"
                      f"{r['text'].strip()}")
    return "\n\n---\n\n".join(blocks)


def results_as_sources(results: list[dict[str, Any]]) -> list[Source]:
    out = []
    for r in results:
        out.append(Source(source_id=Source.make_id(r["key"] + f"#{r.get('page')}"), url=None, title=r["name"],
                          kind="document", retrieved_at=now_iso(), tool="bedrock_knowledge_base",
                          snippet=r["text"][:500], document_key=r["key"]))
    return out

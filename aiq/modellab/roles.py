# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Role-suitability mini-probes: does this model do what each AI-Q role needs?

AI-Q roles and what they demand (01-aiq-source-baseline §b, §c):
  router      classify intent/depth → strict JSON with known enum values, fast, cheap
  clarifier   ≤ 2 clarifying questions for an ambiguous brief, or "no clarification needed"
  shallow     a tool-calling loop: call web_search, then answer with [N] markers and a ## Sources section
  planner     a structured research plan: JSON with 3–6 sub-tasks, each with a query
  researcher  parallel tool calls (several searches in one turn) and cited notes
  writer      a ≥ 450-word report from supplied sources using ONLY valid [N] markers + ## Sources;
              separately: willingness to call a chart tool when asked for a chart (the phase-2 gap)

Scores are deterministic (0–100) from checkable properties, never from a judge model. A role is "suitable"
at ≥ 70, "usable" at 40–69, otherwise "unsuitable". The canned deep run (orchestrator probe) is not here —
it is exercised through the deployed runtime (re-run with model X) because it costs minutes per model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .probes import ConverseProber, MantleProber, _extract_json

ROLES = ("router", "clarifier", "shallow", "planner", "researcher", "writer")
_MARK = re.compile(r"\[(\d+)\]")

_SOURCES = [
    {
        "n": 1,
        "title": "Amazon S3 Vectors — Developer Guide",
        "url": "https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html",
        "text": "Amazon S3 Vectors provides purpose-built vector storage with native support for storing and querying vectors at scale, "  # noqa: E501
        "with sub-second query latency and a cost model based on stored vectors and queries rather than provisioned capacity.",
    },
    {
        "n": 2,
        "title": "Amazon Bedrock Knowledge Bases — What is it",
        "url": "https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html",
        "text": "Amazon Bedrock Knowledge Bases lets you connect foundation models to your data sources for retrieval-augmented generation, "  # noqa: E501
        "handling ingestion, chunking, embedding and retrieval with metadata filtering.",
    },
    {
        "n": 3,
        "title": "Amazon OpenSearch Serverless vector engine",
        "url": "https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html",
        "text": "The vector engine for OpenSearch Serverless offers similarity search with k-NN and hybrid queries; capacity is billed in "  # noqa: E501
        "OpenSearch Compute Units with a minimum footprint even when idle.",
    },
]
_SOURCE_BLOCK = "\n\n".join(
    f'<Document href="{s["url"]}">\n<title>{s["title"]}</title>\n{s["text"]}\n</Document>' for s in _SOURCES
)


@dataclass
class RoleResult:
    role: str
    score: int
    verdict: str
    ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    checks: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _verdict(score: int) -> str:
    return "suitable" if score >= 70 else ("usable" if score >= 40 else "unsuitable")


class _Lane:
    """Uniform chat interface over the lane probers: (system, messages, tools, max_tokens) → (text, tool_calls, usage)."""

    def __init__(self, lane: str, model_id: str, converse: ConverseProber | None, mantle: MantleProber | None):
        self.lane, self.model_id, self.converse, self.mantle = lane, model_id, converse, mantle

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
    ) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
        if self.lane == "converse":
            assert self.converse
            conv_msgs = []
            for m in messages:
                if m["role"] == "tool":
                    conv_msgs.append(
                        {
                            "role": "user",
                            "content": [{"toolResult": {"toolUseId": m["tool_call_id"], "content": [{"text": m["content"]}]}}],
                        }
                    )
                elif m["role"] == "assistant" and m.get("tool_calls"):
                    conv_msgs.append(
                        {
                            "role": "assistant",
                            "content": [
                                {"toolUse": {"toolUseId": c["id"], "name": c["name"], "input": c["input"]}}
                                for c in m["tool_calls"]
                            ],
                        }
                    )
                else:
                    conv_msgs.append({"role": m["role"], "content": [{"text": m["content"]}]})
            tc = (
                {
                    "tools": [
                        {"toolSpec": {"name": t["name"], "description": t["description"], "inputSchema": {"json": t["schema"]}}}
                        for t in tools
                    ]
                }
                if tools
                else None
            )
            r = self.converse._call(self.model_id, conv_msgs, system=system, max_tokens=max_tokens, tool_config=tc)
            blocks = ((r.get("output") or {}).get("message") or {}).get("content") or []
            text = "".join(b.get("text", "") for b in blocks if "text" in b)
            calls = [
                {"id": b["toolUse"]["toolUseId"], "name": b["toolUse"]["name"], "input": b["toolUse"].get("input") or {}}
                for b in blocks
                if "toolUse" in b
            ]
            u = r.get("usage") or {}
            return text, calls, {"input": u.get("inputTokens", 0), "output": u.get("outputTokens", 0)}
        assert self.mantle
        if self.lane == "mantle_chat":
            body: dict[str, Any] = {"model": self.model_id, "max_tokens": max_tokens, "messages": []}
            if system:
                body["messages"].append({"role": "system", "content": system})
            for m in messages:
                if m["role"] == "tool":
                    body["messages"].append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
                elif m["role"] == "assistant" and m.get("tool_calls"):
                    body["messages"].append(
                        {
                            "role": "assistant",
                            "content": m.get("content") or None,
                            "tool_calls": [
                                {
                                    "id": c["id"],
                                    "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c["input"])},
                                }
                                for c in m["tool_calls"]
                            ],
                        }
                    )
                else:
                    body["messages"].append({"role": m["role"], "content": m["content"]})
            if tools:
                body["tools"] = [
                    {
                        "type": "function",
                        "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]},
                    }
                    for t in tools
                ]
            r = self.mantle._post("/v1/chat/completions", body)
            if r.status_code != 200:
                raise RuntimeError(f"http_{r.status_code}: {r.text[:200]}")
            obj = r.json()
            msg = (obj.get("choices") or [{}])[0].get("message") or {}
            calls = []
            for c in msg.get("tool_calls") or []:
                try:
                    args = json.loads((c.get("function") or {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"id": c.get("id"), "name": (c.get("function") or {}).get("name"), "input": args})
            u = obj.get("usage") or {}
            return msg.get("content") or "", calls, {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)}
        if self.lane == "mantle_messages":
            body = {"model": self.model_id, "max_tokens": max_tokens, "anthropic_version": "bedrock-2023-05-31", "messages": []}
            if system:
                body["system"] = system
            for m in messages:
                if m["role"] == "tool":
                    body["messages"].append(
                        {
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}],
                        }
                    )
                elif m["role"] == "assistant" and m.get("tool_calls"):
                    body["messages"].append(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                                for c in m["tool_calls"]
                            ],
                        }
                    )
                else:
                    body["messages"].append({"role": m["role"], "content": m["content"]})
            if tools:
                body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
            r = self.mantle._post("/anthropic/v1/messages", body, headers={"anthropic-version": "2023-06-01"})
            if r.status_code != 200:
                raise RuntimeError(f"http_{r.status_code}: {r.text[:200]}")
            obj = r.json()
            text = "".join(c.get("text", "") for c in obj.get("content") or [] if c.get("type") == "text")
            calls = [
                {"id": c["id"], "name": c["name"], "input": c.get("input") or {}}
                for c in obj.get("content") or []
                if c.get("type") == "tool_use"
            ]
            u = obj.get("usage") or {}
            return text, calls, {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)}
        raise RuntimeError(f"role probes not implemented for lane {self.lane}")


_SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the web. Returns documents with href and text.",
    "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}
_CHART_TOOL = {
    "name": "create_chart",
    "description": "Render a bar or line chart from labelled numeric series and save it as PNG.",
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["bar", "line"]},
            "labels": {"type": "array", "items": {"type": "string"}},
            "values": {"type": "array", "items": {"type": "number"}},
        },
        "required": ["title", "kind", "labels", "values"],
    },
}


def _timed(role: str, fn):
    t0 = time.monotonic()
    try:
        score, checks, usage = fn()
        return RoleResult(
            role,
            score,
            _verdict(score),
            int((time.monotonic() - t0) * 1000),
            usage.get("input", 0),
            usage.get("output", 0),
            checks,
        )
    except Exception as e:  # noqa: BLE001
        return RoleResult(
            role, 0, "unsuitable", int((time.monotonic() - t0) * 1000), error=f"{e.__class__.__name__}: {str(e)[:300]}"
        )


def probe_router(lane: _Lane) -> RoleResult:
    def run():
        sys_p = (
            'Classify the user\'s message. Reply with ONLY a JSON object {"intent": "meta"|"research", '
            '"depth": "shallow"|"deep"|null}. meta = greetings/questions about you; research = needs facts. '
            "depth shallow = a single fact; deep = a report comparing or analysing several things."
        )
        cases = [
            ("hi there, what can you do?", "meta", None),
            ("what is the population of Lisbon?", "research", "shallow"),
            ("compare the vector database offerings of the three major clouds for a 100M-vector workload", "research", "deep"),
        ]
        ok = 0
        usage = {"input": 0, "output": 0}
        details = []
        for q, intent, depth in cases:
            text, _, u = lane.chat([{"role": "user", "content": q}], system=sys_p, max_tokens=200)
            usage["input"] += u["input"]
            usage["output"] += u["output"]
            obj = _extract_json(text)
            good = bool(obj) and obj.get("intent") == intent and (depth is None or obj.get("depth") == depth)
            ok += good
            details.append({"q": q[:40], "got": obj, "ok": good})
        return round(100 * ok / len(cases)), {"cases": details}, usage

    return _timed("router", run)


def probe_clarifier(lane: _Lane) -> RoleResult:
    def run():
        sys_p = (
            "You help scope research. If the request is ambiguous, ask AT MOST two short clarifying questions, each ending "
            "with '?', one per line, nothing else. If it is clear, reply exactly: NO_CLARIFICATION_NEEDED"
        )
        text1, _, u1 = lane.chat([{"role": "user", "content": "Research the market."}], system=sys_p, max_tokens=300)
        text2, _, u2 = lane.chat(
            [{"role": "user", "content": "What is the current CEO of Amazon's name?"}], system=sys_p, max_tokens=100
        )
        qs = [line for line in text1.splitlines() if line.strip().endswith("?")]
        c1 = 1 <= len(qs) <= 2 and len(text1) < 600
        c2 = "NO_CLARIFICATION_NEEDED" in text2
        score = 60 * c1 + 40 * c2
        return (
            score,
            {"questions": qs[:3], "clear_case": text2[:60]},
            {"input": u1["input"] + u2["input"], "output": u1["output"] + u2["output"]},
        )

    return _timed("clarifier", run)


def _shallow_like(lane: _Lane, role: str, parallel: bool) -> RoleResult:
    def run():
        sys_p = (
            "You are a research assistant. Use the web_search tool to find facts, then answer in Markdown. Cite every "
            "fact with numbered markers like [1] that refer to the documents you were given, and end with a '## Sources' "
            "section listing each number with its URL. Never cite a source you did not receive."
        )
        q = (
            "Compare Amazon S3 Vectors, Bedrock Knowledge Bases and OpenSearch Serverless for storing research embeddings."
            if parallel
            else "What is Amazon S3 Vectors and how is it priced?"
        )
        if parallel:
            sys_p += " Issue all the searches you need in ONE turn (several tool calls at once) before answering."
        msgs: list[dict[str, Any]] = [{"role": "user", "content": q}]
        text, calls, u = lane.chat(msgs, system=sys_p, tools=[_SEARCH_TOOL], max_tokens=800)
        usage = {"input": u["input"], "output": u["output"]}
        checks: dict[str, Any] = {
            "tool_calls_turn1": len(calls),
            "queries": [c["input"].get("query", "")[:50] for c in calls][:4],
        }
        if not calls:
            return 0, {**checks, "reason": "no tool call"}, usage
        msgs.append({"role": "assistant", "content": text, "tool_calls": calls})
        for c in calls:
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": _SOURCE_BLOCK})
        text2, calls2, u2 = lane.chat(msgs, system=sys_p, tools=[_SEARCH_TOOL], max_tokens=1200)
        usage["input"] += u2["input"]
        usage["output"] += u2["output"]
        if not text2 and calls2:  # one more tool round, then insist on an answer
            msgs.append({"role": "assistant", "content": text2, "tool_calls": calls2})
            for c in calls2:
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": _SOURCE_BLOCK})
            text2, _, u3 = lane.chat(msgs, system=sys_p, max_tokens=1200)
            usage["input"] += u3["input"]
            usage["output"] += u3["output"]
        markers = {int(m) for m in _MARK.findall(text2)}
        valid = markers and markers <= {1, 2, 3}
        has_sources = "## sources" in text2.lower()
        score = (
            30
            + 30 * bool(valid)
            + 25 * has_sources
            + (15 if (parallel and len(calls) >= 2) or (not parallel and len(text2) > 200) else 0)
        )
        checks.update(markers=sorted(markers), valid_markers=bool(valid), sources_section=has_sources, answer_chars=len(text2))
        return score, checks, usage

    return _timed(role, run)


def probe_shallow(lane: _Lane) -> RoleResult:
    return _shallow_like(lane, "shallow", parallel=False)


def probe_researcher(lane: _Lane) -> RoleResult:
    return _shallow_like(lane, "researcher", parallel=True)


def probe_planner(lane: _Lane) -> RoleResult:
    def run():
        sys_p = (
            'You plan research. Reply with ONLY JSON: {"topic": string, "subtasks": [{"title": string, "query": string, '
            '"source": "web"|"documents"}]} with 3 to 6 subtasks.'
        )
        text, _, u = lane.chat(
            [{"role": "user", "content": "Plan research on how universities are adopting managed vector databases."}],
            system=sys_p,
            max_tokens=900,
        )
        obj = _extract_json(text)
        subs = (obj or {}).get("subtasks") or []
        good_subs = [
            s
            for s in subs
            if isinstance(s, dict) and s.get("title") and s.get("query") and s.get("source") in ("web", "documents")
        ]
        score = 40 * bool(obj) + 30 * (3 <= len(subs) <= 6) + 30 * (len(good_subs) == len(subs) and len(subs) > 0)
        return score, {"subtasks": len(subs), "well_formed": len(good_subs)}, {"input": u["input"], "output": u["output"]}

    return _timed("planner", run)


def probe_writer(lane: _Lane) -> RoleResult:
    def run():
        sys_p = (
            "You write the final research report from the researcher's notes and sources. Rules: Markdown with a title and "
            "at least three ## sections; at least 450 words; cite facts with [N] markers that refer ONLY to the numbered sources "
            "given; end with a '## Sources' section listing N. Title, URL for each source. Do not invent sources."
        )
        notes = "\n".join(f"[{s['n']}] {s['title']} — {s['url']}\n{s['text']}" for s in _SOURCES)
        text, _, u = lane.chat(
            [{"role": "user", "content": f"Question: Which AWS vector store should a research team pick?\n\nSources:\n{notes}"}],
            system=sys_p,
            max_tokens=1800,
        )
        markers = {int(m) for m in _MARK.findall(text)}
        words = len(text.split())
        valid = bool(markers) and markers <= {1, 2, 3}
        score = 25 * (words >= 450) + 15 * (text.count("## ") >= 3) + 35 * valid + 25 * ("## sources" in text.lower())
        return (
            score,
            {
                "words": words,
                "sections": text.count("## "),
                "markers": sorted(markers),
                "valid_markers": valid,
                "sources_section": "## sources" in text.lower(),
            },
            {"input": u["input"], "output": u["output"]},
        )

    return _timed("writer", run)


def probe_chart_willingness(lane: _Lane) -> RoleResult:
    """Does the model call a chart tool when the brief asks for a chart? (phase-2 gap: writers only probed the tool)"""

    def run():
        sys_p = "You write research reports. Tools are available; use create_chart when a chart is requested."
        text, calls, u = lane.chat(
            [
                {
                    "role": "user",
                    "content": "Write a two-paragraph note on cloud vector-store idle costs and INCLUDE A BAR CHART "
                    "of these monthly idle costs: S3 Vectors 0, Bedrock KB 0, OpenSearch Serverless 700.",
                }
            ],
            system=sys_p,
            tools=[_CHART_TOOL],
            max_tokens=700,
        )
        called = any(c["name"] == "create_chart" for c in calls)
        args_ok = called and any(
            len(c["input"].get("labels", [])) == 3 and len(c["input"].get("values", [])) == 3
            for c in calls
            if c["name"] == "create_chart"
        )
        score = 70 * called + 30 * bool(args_ok)
        return (
            score,
            {"called": called, "args_ok": bool(args_ok), "calls": [c["name"] for c in calls]},
            {"input": u["input"], "output": u["output"]},
        )

    return _timed("chart", run)


ROLE_PROBES = {
    "router": probe_router,
    "clarifier": probe_clarifier,
    "shallow": probe_shallow,
    "planner": probe_planner,
    "researcher": probe_researcher,
    "writer": probe_writer,
    "chart": probe_chart_willingness,
}


def probe_roles(
    entry: dict[str, Any],
    converse: ConverseProber | None,
    mantle: MantleProber | None,
    roles: tuple[str, ...] = tuple(ROLE_PROBES),
) -> dict[str, RoleResult]:
    lane = _Lane(entry["lane"], entry["id"], converse, mantle)
    return {r: ROLE_PROBES[r](lane) for r in roles}

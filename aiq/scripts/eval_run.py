#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression / evaluation harness for AI-Q on AgentCore.

Runs a question set against the deployed runtime (same path Open WebUI uses), collects deterministic
metrics from the job journal (citations verified/unverified, sources, searches, pages, tokens, latency,
outcome) and, optionally, an LLM-as-judge score from Amazon Bedrock (Converse) for groundedness and
answer quality with a short rationale. Writes results.json + results.md. No secrets are written.

Usage:
  SMOKE_PASSWORD=... python aiq/scripts/eval_run.py --run-id <runId> --user-pool <pool> --client-id <owui client> \
     --username <user> --questions aiq/eval/questions.json --out evidence/eval [--judge-model nvidia.nemotron-super-3-120b]

questions.json: [{"id": "q1", "mode": "shallow|deep", "question": "...", "expects": ["keyword", ...]}]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from urllib.parse import quote

import boto3
import httpx

sys.path.insert(0, os.path.dirname(__file__))
from smoke import mint_token, runtime_arn  # noqa: E402

JUDGE_PROMPT = """You are grading a research answer. Score two dimensions from 1 (poor) to 5 (excellent).
groundedness: are the claims supported by the listed sources (titles/URLs) and cited with [n] markers?
quality: is the answer correct, complete for the question, well structured and free of speculation?
Return ONLY JSON: {"groundedness": <int>, "quality": <int>, "rationale": "<one sentence>"}.

QUESTION:
{question}

ANSWER (may be truncated):
{answer}

SOURCES:
{sources}
"""


def invoke(arn: str, region: str, token: str, payload: dict, session_id: str, timeout: float) -> list[dict]:
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "text/event-stream, application/json", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id}
    out = []
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=20.0)) as c, c.stream("POST", url, headers=headers, json=payload) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            line = raw.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, str):
                    obj = json.loads(obj)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def run_question(arn, region, token, q, timeout) -> dict:
    sid = f"eval-{q['id']}-{uuid.uuid4()}"
    t0 = time.time()
    mode = q.get("mode", "shallow")
    payload = {"op": "chat" if mode == "shallow" else "submit", "mode": mode,
               "messages": [{"role": "user", "content": q["question"]}],
               "client_request_id": f"eval-{q['id']}-{uuid.uuid4().hex[:8]}", "conversation_id": sid}
    events = invoke(arn, region, token, payload, sid, timeout)
    job_id = next((e.get("job_id") for e in events if e.get("job_id")), None)
    if mode != "shallow" and job_id:
        events += invoke(arn, region, token, {"op": "events", "job_id": job_id, "after": 0, "tail": True},
                         f"eval-tail-{uuid.uuid4()}", timeout)
    by = {}
    for e in events:
        by.setdefault(e.get("type"), []).append(e)
    report = (by.get("report") or [{}])[-1].get("data", {})
    cit = (by.get("citations") or [{}])[-1].get("data", {})
    usage = (by.get("usage") or [{}])[-1].get("data", {})
    err = (by.get("error") or [{}])[-1].get("data", {})
    text = report.get("text", "") or ""
    expects = q.get("expects") or []
    hits = [k for k in expects if k.lower() in text.lower()]
    return {"id": q["id"], "mode": mode, "job_id": job_id, "seconds": round(time.time() - t0, 1),
            "outcome": "completed" if by.get("completed") else ("failed" if err else "unknown"),
            "error": (err.get("error") or {}).get("message") if err else None,
            "sources": len(report.get("sources") or []), "citations_verified": cit.get("verified", 0),
            "citations_unverified": cit.get("unverified", 0),
            "markers_in_text": len(set(re.findall(r"\[(\d+)\]", text))),
            "expects_hit": f"{len(hits)}/{len(expects)}" if expects else None,
            "usage": {k: usage.get(k) for k in ("input_tokens", "output_tokens", "llm_calls", "searches", "pages", "retrievals")},
            "answer_excerpt": text[:600], "answer": text,
            "source_list": [f"{s.get('title') or ''} | {s.get('url') or s.get('document_key') or ''}" for s in (report.get("sources") or [])[:25]]}


def judge(bedrock, model: str, r: dict) -> dict:
    if not r.get("answer"):
        return {"groundedness": None, "quality": None, "rationale": "no answer"}
    prompt = JUDGE_PROMPT.format(question=r["id"], answer=r["answer"][:12000], sources="\n".join(r["source_list"]))
    resp = bedrock.converse(modelId=model, messages=[{"role": "user", "content": [{"text": prompt}]}],
                            inferenceConfig={"maxTokens": 300, "temperature": 0})
    txt = resp["output"]["message"]["content"][0]["text"]
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {"rationale": txt[:200]}
    except json.JSONDecodeError:
        return {"rationale": txt[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--user-pool", required=True)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-env", default="SMOKE_PASSWORD")
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--timeout", type=float, default=1500)
    ap.add_argument("--only", default=None, help="comma-separated question ids")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    token = mint_token(a.user_pool, a.client_id, a.username, os.environ[a.password_env], a.region)
    arn = runtime_arn(a.run_id, a.region)
    questions = json.load(open(a.questions))
    if a.only:
        keep = set(a.only.split(","))
        questions = [q for q in questions if q["id"] in keep]
    bedrock = boto3.client("bedrock-runtime", region_name=a.region) if a.judge_model else None
    results = []
    for q in questions:
        print(f"# running {q['id']} ({q.get('mode', 'shallow')})", file=sys.stderr)
        r = run_question(arn, a.region, token, q, a.timeout)
        r["question"] = q["question"]
        if bedrock:
            r["judge"] = judge(bedrock, a.judge_model, {**r, "id": q["question"]})
        results.append(r)
        print(json.dumps({k: v for k, v in r.items() if k not in ("answer", "source_list")}, ensure_ascii=False)[:600], file=sys.stderr)
    with open(os.path.join(a.out, "results.json"), "w") as f:
        json.dump({"run_id": a.run_id, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "judge_model": a.judge_model, "results": results}, f, indent=2, ensure_ascii=False)
    lines = ["| id | mode | outcome | s | sources | cit ok/bad | markers | expects | tokens in/out | searches/pages | judge g/q |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        u = r["usage"]
        j = r.get("judge") or {}
        lines.append(f"| {r['id']} | {r['mode']} | {r['outcome']} | {r['seconds']} | {r['sources']} | {r['citations_verified']}/{r['citations_unverified']} | "
                     f"{r['markers_in_text']} | {r['expects_hit'] or '-'} | {u.get('input_tokens')}/{u.get('output_tokens')} | {u.get('searches')}/{u.get('pages')} | "
                     f"{j.get('groundedness', '-')}/{j.get('quality', '-')} |")
    with open(os.path.join(a.out, "results.md"), "w") as f:
        f.write("\n".join(lines) + "\n\n" + "\n".join(f"- **{r['id']}** judge: {(r.get('judge') or {}).get('rationale', '')}" for r in results) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

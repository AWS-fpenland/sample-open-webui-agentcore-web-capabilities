// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Twelve realistic packages (mixed statuses, one re-run pair, one follow-up pair, tags, costs) with full
// schema-shaped manifests. Model prices come from the real matrix fixture (matrix.json) so costs are consistent.
import matrixJson from './matrix.json';
import { EU_AI_ACT_A, EU_AI_ACT_B, FOLLOWUP_FILTERING, NOVA_REGION, PRICING_QUICK, S3_VECTORS, SOC_LANDSCAPE } from './reports';
import type { Artifact, Citation, CostLine, JobStatus, LineageRelation, Manifest, Mode, ModelRole, ModelsResponse, PackageSummary, Role, RuntimeEvent, Source } from '../types';
import { ROLES } from '../types';

const MATRIX = matrixJson as unknown as ModelsResponse;
const TENANT = 'u_86d8f0c1a2b3c4d5e6f708192a3b4c5d';
const HEX = 'a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192';
export const jid = (seed: string) => `job_${(seed.replace(/[^a-f0-9]/g, '') + HEX).slice(0, 32)}`;
const sid = (seed: string, i: number) => `src_${(seed + i.toString(16).padStart(2, '0') + HEX).slice(0, 16)}`;
const sha = (seed: string) => (seed.replace(/[^a-f0-9]/g, '') + HEX + HEX).slice(0, 64);
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

function priceOf(id: string): { input: number; output: number } {
  const e = MATRIX.entries.find((x) => x.id === id) ?? MATRIX.entries.find((x) => x.base_model_id === id);
  return { input: e?.price.input_per_1m ?? 0.3, output: e?.price.output_per_1m ?? 2.5 };
}

function role(id: string, name: string, provider: string, nat: string, extra: Partial<ModelRole> = {}): ModelRole {
  return { model_id: id, human_name: name, provider, lane: 'bedrock_runtime', region: 'us-east-1', max_tokens: 16384, reasoning_effort: null, nat_llm_name: nat, ...extra };
}
export const M = {
  nano: (nat: string) => role('nvidia.nemotron-nano-3-30b', 'Nemotron Nano 3 30B', 'nvidia', nat, { max_tokens: 2048 }),
  nova2: (nat: string) => role('global.amazon.nova-2-lite-v1:0', 'Nova 2 Lite', 'amazon', nat),
  superN: (nat: string) => role('nvidia.nemotron-super-3-120b', 'NVIDIA Nemotron 3 Super 120B A12B', 'nvidia', nat, { reasoning_effort: 'medium' }),
  opus48: (nat: string) => role('global.anthropic.claude-opus-4-8', 'Claude Opus 4.8', 'anthropic', nat),
  kimi: (nat: string) => role('global.moonshotai.kimi-k3', 'Kimi K3', 'moonshot ai', nat),
  deepseek: (nat: string) => role('deepseek.v3.2', 'DeepSeek V3.2', 'deepseek', nat),
  terra: (nat: string) => role('global.openai.gpt-5.6-terra', 'GPT-5.6 Terra', 'openai', nat),
};
type RolesMap = Record<Role, ModelRole>;
export function rolesWith(writer: ModelRole, planner: ModelRole = M.nova2('planner_llm'), researcher: ModelRole = M.nova2('researcher_llm')): RolesMap {
  return { router: M.nano('router_llm'), clarifier: M.nano('clarifier_llm'), shallow: M.nano('shallow_llm'), planner, researcher, writer };
}

interface SourceSeed {
  url: string;
  title: string;
  kind?: Source['kind'];
}
const EU_SOURCES: SourceSeed[] = [
  { url: 'https://eur-lex.europa.eu/eli/reg/2024/1689/oj', title: 'Regulation (EU) 2024/1689 — Article 2(6), scope exclusions' },
  { url: 'https://eur-lex.europa.eu/eli/reg/2024/1689/oj#anx_III', title: 'Annex III — High-risk AI systems referred to in Article 6(2)' },
  { url: 'https://artificialintelligenceact.eu/implementation-timeline/', title: 'Implementation timeline' },
  { url: 'https://artificialintelligenceact.eu/recital/25/', title: 'Recital 25 — research and development exemption' },
  { url: 'https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai', title: 'Regulatory framework for AI' },
  { url: 'https://www.europarl.europa.eu/topics/en/article/20230601STO93804/eu-ai-act-first-regulation-on-artificial-intelligence', title: 'EU AI Act: first regulation on artificial intelligence' },
  { url: 'https://www.leru.org/publications', title: 'LERU position paper on the AI Act' },
  { url: 'https://www.edpb.europa.eu/our-work-tools/our-documents_en', title: 'EDPB statement on the AI Act and data protection' },
  { url: 'https://artificialintelligenceact.eu/article/26/', title: 'Article 26 — Obligations of deployers of high-risk AI systems' },
  { url: 'https://www.enisa.europa.eu/topics/artificial-intelligence', title: 'ENISA: AI cybersecurity' },
  { url: 'https://op.europa.eu/en/publication-detail/-/publication/ai-in-education', title: 'AI in education: policy brief' },
  { url: 'https://digital-strategy.ec.europa.eu/en/policies/guidelines-gpai-providers', title: 'Commission guidelines on GPAI providers (July 2025)' },
];
const VEC_SOURCES: SourceSeed[] = [
  { url: 'https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html', title: 'Limitations and restrictions — Amazon S3 Vectors' },
  { url: 'https://aws.amazon.com/opensearch-service/pricing/', title: 'Amazon OpenSearch Service pricing' },
  { url: 'https://aws.amazon.com/s3/pricing/', title: 'Amazon S3 pricing' },
  { url: 'https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html', title: 'Vector search collections — OpenSearch Serverless' },
  { url: 'https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html', title: 'Metadata filtering — Amazon S3 Vectors' },
  { url: 'https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.VectorDB.html', title: 'Using pgvector with Aurora PostgreSQL' },
  { url: 'https://aws.amazon.com/blogs/aws/introducing-amazon-s3-vectors/', title: 'Introducing Amazon S3 Vectors' },
  { url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html', title: 'Set up a knowledge base — Amazon Bedrock' },
];
const SOC_SOURCES: SourceSeed[] = [
  { url: 'https://www.gartner.com/en/documents/security-operations-2026', title: 'Security operations market guide' },
  { url: 'https://aws.amazon.com/blogs/security/', title: 'AWS Security Blog: agentic triage case study', kind: 'news' },
  { url: 'https://docs.aws.amazon.com/security-hub/latest/userguide/what-is-securityhub.html', title: 'What is AWS Security Hub' },
  { url: 'https://www.sans.org/white-papers/', title: 'SANS survey on AI in the SOC' },
  { url: 'https://www.crowdstrike.com/resources/', title: 'Vendor case studies' },
  { url: 'https://csrc.nist.gov/publications', title: 'NIST guidance on AI in security operations' },
];
const PRICING_SOURCES: SourceSeed[] = [
  { url: 'https://aws.amazon.com/bedrock/pricing/', title: 'Amazon Bedrock pricing' },
  { url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html', title: 'Supported foundation models' },
  { url: 'https://aws.amazon.com/about-aws/whats-new/', title: "What's New with AWS", kind: 'news' },
];
const REGION_SOURCES: SourceSeed[] = [
  { url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html', title: 'Model support by AWS Region' },
  { url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html', title: 'Supported cross-Region inference profiles' },
];

function padSources(seed: string, base: SourceSeed[], n: number, createdMin: number, durationSec: number): Source[] {
  const out: Source[] = [];
  for (let i = 0; i < n; i++) {
    const s = base[i % base.length];
    const nth = Math.floor(i / base.length);
    const url = nth === 0 ? s.url : `${s.url}${s.url.includes('#') ? '' : '#'}section-${nth}`;
    const tMin = createdMin - Math.min(durationSec / 60, 1 + (i * (durationSec / 60)) / Math.max(1, n));
    out.push({
      source_id: sid(seed, i),
      url,
      title: nth === 0 ? s.title : `${s.title} (§${nth})`,
      kind: s.kind ?? (i % 3 === 0 ? 'web_search' : 'web_page'),
      retrieved_at: minutesAgo(tMin),
      tool: i % 3 === 0 ? 'agentcore_web_search' : 'agentcore_fetch_page',
      snippet: `${s.title} — excerpt retrieved by the researchers.`,
      document_key: null,
      content_sha256: i % 3 === 0 ? null : sha(seed + i.toString(16)),
      cited_by: [],
    });
  }
  return out;
}

function markersIn(md: string): number[] {
  const set = new Set<number>();
  const body = md.split(/^## Sources/m)[0];
  for (const m of body.matchAll(/\[(\d{1,3})\]/g)) set.add(Number(m[1]));
  return [...set].sort((a, b) => a - b);
}

function citationsFor(md: string, sources: Source[], unverifiedMarkers: number[] = []): Citation[] {
  return markersIn(md).map((n) => {
    const src = sources[n - 1];
    if (!src || unverifiedMarkers.includes(n)) {
      return { marker: `[${n}]`, source_id: null, url: src?.url ?? `https://example.org/not-retrieved-${n}`, verified: false, match_level: 'unmatched', reason: 'url not in source registry' };
    }
    src.cited_by = [...(src.cited_by ?? []), `[${n}]`];
    return { marker: `[${n}]`, source_id: src.source_id, url: src.url ?? null, verified: true, match_level: n % 4 === 0 ? 'normalized' : 'exact', reason: null };
  });
}

function artifact(jobId: string, seed: string, filename: string, title: string, caption: string, minAgo: number, size = 48211): Artifact {
  return {
    artifact_id: `art_${(seed + HEX).slice(0, 32)}`,
    job_id: jobId,
    kind: 'image',
    mime_type: 'image/png',
    filename,
    sandbox_path: `/workspace/${jobId}/aiq-artifacts/${filename}`,
    storage_key: `tenants/${TENANT}/packages/${jobId}/artifacts/${filename}`,
    storage_uri: null,
    sha256: sha(seed),
    size_bytes: size,
    title,
    caption,
    inline: true,
    workflow: 'skill',
    source_tool_call_id: null,
    provenance: { command: 'python3 chart.py', sandbox_provider: 'agentcore_code_interpreter', package_snapshot: ['matplotlib==3.9.2', 'pandas==2.2.3'] },
    created_at: minutesAgo(minAgo),
    capture_phase: 'checkpoint',
    status: 'available',
    referenced_in_report: true,
  };
}

interface Seed {
  seed: string;
  title: string;
  question: string;
  mode: Mode;
  depth: Manifest['depth'];
  status: JobStatus;
  createdMin: number;
  durationSec: number | null;
  roles: RolesMap;
  selection?: Manifest['models']['selection'];
  tags: string[];
  pinned: boolean;
  project?: string;
  report: string | null;
  sources: SourceSeed[];
  nSources: number;
  unverified?: number[];
  artifacts?: { seed: string; filename: string; title: string; caption: string }[];
  exports?: string[];
  error?: string;
  usage: { in: number; out: number; calls: number; searches: number; pages: number };
  relation: LineageRelation;
  parentSeed?: string;
  rootSeed?: string;
  changes?: Manifest['lineage']['changes'];
  conversation?: string;
  clarification?: { q: string; a: string }[];
  progress?: { phase: string; pct: number };
}

export interface MockPackage {
  manifest: Manifest;
  report_md: string;
  /** Live simulation for running/queued packages (progress advances with wall-clock time). */
  live?: { startedAt: number; durationMs: number; startPct: number; finalStatus: JobStatus; sourcesTarget: number };
}

function costLines(roles: RolesMap, usage: Seed['usage']): { lines: CostLine[]; total: number; per_model: NonNullable<Manifest['usage']['per_model']> } {
  const split: [ModelRole, number, number][] = [
    [roles.router, 0.002, 0.005],
    [roles.planner, 0.08, 0.15],
    [roles.researcher, 0.86, 0.6],
    [roles.writer, 0.058, 0.245],
  ];
  const lines: CostLine[] = [];
  const per: Record<string, { input_tokens: number; output_tokens: number; calls: number }> = {};
  for (const [r, fi, fo] of split) {
    const inT = Math.round(usage.in * fi);
    const outT = Math.round(usage.out * fo);
    const p = priceOf(r.model_id);
    lines.push({ component: `bedrock:${r.model_id}:input_tokens`, unit: '1K tokens', quantity: inT / 1000, unit_price_usd: p.input / 1000, usd: (inT / 1e6) * p.input, model_id: r.model_id });
    lines.push({ component: `bedrock:${r.model_id}:output_tokens`, unit: '1K tokens', quantity: outT / 1000, unit_price_usd: p.output / 1000, usd: (outT / 1e6) * p.output, model_id: r.model_id });
    const acc = (per[r.model_id] ??= { input_tokens: 0, output_tokens: 0, calls: 0 });
    acc.input_tokens += inT;
    acc.output_tokens += outT;
    acc.calls += Math.max(1, Math.round(usage.calls * (fi + fo)));
  }
  lines.push({ component: 'agentcore:web_search:queries', unit: 'query', quantity: usage.searches, unit_price_usd: 0.007, usd: usage.searches * 0.007, model_id: null });
  lines.push({ component: 'agentcore:browser:sessions', unit: 'session', quantity: usage.pages, unit_price_usd: 0.0018, usd: usage.pages * 0.0018, model_id: null });
  lines.push({ component: 'agentcore:runtime:vcpu_hours', unit: 'vCPU-hour', quantity: 0.02, unit_price_usd: 0.0895, usd: 0.0018, model_id: null });
  for (const l of lines) l.usd = Math.round(l.usd * 10000) / 10000;
  const total = Math.round(lines.reduce((s, l) => s + l.usd, 0) * 10000) / 10000;
  return { lines, total, per_model: Object.entries(per).map(([model_id, v]) => ({ model_id, ...v })) };
}

function headingsOf(md: string): { level: number; text: string }[] {
  return [...md.matchAll(/^(#{1,3})\s+(.+)$/gm)].map((m) => ({ level: m[1].length, text: m[2].trim() }));
}

function build(s: Seed): MockPackage {
  const id = jid(s.seed);
  const md = s.report ?? '';
  const sources = padSources(s.seed, s.sources, s.nSources, s.createdMin, s.durationSec ?? 300);
  const citations = md ? citationsFor(md, sources, s.unverified) : [];
  const { lines, total, per_model } = costLines(s.roles, s.usage);
  const terminal = ['completed', 'failed', 'cancelled'].includes(s.status);
  const completedAt = terminal && s.durationSec !== null ? minutesAgo(s.createdMin - s.durationSec / 60) : null;
  const artifacts = (s.artifacts ?? []).map((a) => artifact(id, a.seed, a.filename, a.title, a.caption, s.createdMin - 3));
  const manifest: Manifest = {
    schema_version: 'aiq-agentcore/package/v1',
    package_id: id,
    job_id: id,
    tenant_key: TENANT,
    title: s.title,
    question: s.question,
    clarification: s.clarification ?? [],
    mode: s.mode,
    depth: s.depth,
    data_sources: ['web_search'],
    status: s.status,
    error: s.error ?? null,
    timing: {
      created_at: minutesAgo(s.createdMin),
      started_at: s.status === 'queued' ? null : minutesAgo(s.createdMin - 0.05),
      completed_at: completedAt,
      updated_at: completedAt ?? minutesAgo(Math.max(0, s.createdMin - 1)),
      duration_seconds: terminal ? s.durationSec : null,
      clarification_turns: s.clarification?.length ?? 0,
    },
    runtime: {
      adapter_version: '0.9.0',
      source_commit: 'e5f37971ae3b',
      upstream_ref: 'bf4e67d1564ef8d2ec8f65b5f9001e512befc095',
      runtime_version: 'v18',
      runtime_session_id: `aiq-job-${s.conversation ?? '2b6f7e2d-8c1a-4a0e-9c1d-0f1e2d3c4b5a'}-${id.slice(4, 12)}`,
      conversation_id: s.conversation ?? null,
      region: 'us-east-1',
    },
    models: { selection: s.selection ?? 'deploy_default', roles: { ...s.roles, orchestrator: s.roles.planner, source_router: s.roles.planner } },
    usage: {
      input_tokens: s.usage.in,
      output_tokens: s.usage.out,
      llm_calls: s.usage.calls,
      truncated_outputs: 0,
      searches: s.usage.searches,
      pages: s.usage.pages,
      retrievals: 0,
      sandbox_seconds: artifacts.length ? 212 : 0,
      browser_sessions: s.usage.pages,
      guardrail_text_units: 0,
      per_model,
    },
    cost: {
      currency: 'USD',
      total_usd: total,
      confidence: 'computed',
      price_source: { kind: 'aws_price_list_api', retrieved_at: '2026-09-17T23:16:09Z', offer_codes: ['AmazonBedrock', 'AmazonBedrockFoundationModels', 'AmazonBedrockAgentCore'], region: 'us-east-1' },
      lines,
    },
    report: {
      present: !!md,
      key: md ? `tenants/${TENANT}/packages/${id}/report.md` : null,
      sha256: md ? sha(s.seed + 'ee') : null,
      size_bytes: md ? md.length : null,
      word_count: md ? md.split(/\s+/).length : null,
      format: 'text/markdown',
      headings: headingsOf(md),
      summary: md ? md.split('\n\n')[2]?.slice(0, 280) ?? null : null,
      guardrail_action: null,
    },
    citations: {
      ledger_key: md ? `tenants/${TENANT}/packages/${id}/ledger.json` : null,
      verified: citations.filter((c) => c.verified).length,
      unverified: citations.filter((c) => !c.verified).length,
      items: citations,
    },
    sources,
    artifacts,
    lineage: {
      parent_package_id: s.parentSeed ? jid(s.parentSeed) : null,
      relation: s.relation,
      root_package_id: jid(s.rootSeed ?? s.seed),
      result_kind: null,
      changes: s.changes ?? { models_changed: [], data_sources_changed: false, question_changed: false },
    },
    organization: { tags: s.tags, pinned: s.pinned, project: s.project ?? null, notes: null },
    exports: (s.exports ?? []).map((f, i) => ({
      format: f,
      key: `tenants/${TENANT}/packages/${id}/exports/report.${f}`,
      sha256: sha(s.seed + f),
      size_bytes: 40000 + i * 37000,
      created_at: minutesAgo(Math.max(0, s.createdMin - 12)),
      derived: f === 'pptx',
      generator: f === 'pdf' ? 'aiq-export/0.1 (agentcore-browser print)' : f === 'docx' ? 'aiq-export/0.1 (python-docx)' : 'aiq-export/0.1',
      theme: f === 'pdf' ? 'print' : null,
    })),
    evals: [],
    knowledge: md ? { collection: '__packages', document_key: `documents/${TENANT}/__packages/${id}.md`, ingested_at: completedAt, ingestion_job_id: 'ABCDEFGHIJ' } : {},
    retention: { expires_at: null, s3_prefix: `tenants/${TENANT}/packages/${id}/`, deleted_at: null },
  };
  const pkg: MockPackage = { manifest, report_md: md };
  if (s.progress && (s.status === 'running' || s.status === 'queued')) {
    pkg.live = { startedAt: Date.now(), durationMs: s.status === 'queued' ? 150_000 : 95_000, startPct: s.progress.pct, finalStatus: 'completed', sourcesTarget: s.nSources + 9 };
  }
  return pkg;
}

const EU_Q = 'What obligations does the EU AI Act place on university research groups that build or deploy AI systems, and which exemptions apply?';
const VEC_Q = 'Compare Amazon S3 Vectors and OpenSearch Serverless as vector stores for a small (<1M vectors) RAG workload: cost, latency, filtering, limits.';

export const SEEDS: Seed[] = [
  {
    seed: '7c1e', title: 'Vector store options for a research lab (S3 Vectors vs OpenSearch vs Aurora)',
    question: 'Which vector store should a 12-person research lab pick for ~2M embeddings with bursty query load: S3 Vectors, OpenSearch Serverless or Aurora pgvector? Include cost at idle.',
    mode: 'deep', depth: 'deep', status: 'running', createdMin: 9, durationSec: null,
    roles: rolesWith(M.opus48('writer_llm')), selection: 'session_override', tags: ['infra', 'q3-review'], pinned: true, project: 'platform-eval',
    report: null, sources: VEC_SOURCES, nSources: 31, usage: { in: 1_240_000, out: 14_200, calls: 41, searches: 14, pages: 11 },
    relation: 'root', conversation: '9d2c1a4e-5f6b-4c7d-8e9f-0a1b2c3d4e5f', progress: { phase: 'researching', pct: 63 },
  },
  {
    seed: '9ab2', title: 'EU AI Act obligations for university research groups', question: EU_Q,
    mode: 'deep', depth: 'deep', status: 'completed', createdMin: 620, durationSec: 460,
    roles: rolesWith(M.superN('writer_llm')), selection: 'rerun_override', tags: ['policy'], pinned: false,
    report: EU_AI_ACT_B, sources: EU_SOURCES, nSources: 44, usage: { in: 2_310_000, out: 29_100, calls: 63, searches: 21, pages: 15 },
    relation: 'rerun', parentSeed: '4f10', rootSeed: '4f10', changes: { models_changed: ['writer'], data_sources_changed: false, question_changed: false },
    conversation: '2b6f7e2d-8c1a-4a0e-9c1d-0f1e2d3c4b5a',
  },
  {
    seed: '4f10', title: 'EU AI Act obligations for university research groups', question: EU_Q,
    mode: 'deep', depth: 'deep', status: 'completed', createdMin: 1900, durationSec: 372,
    roles: rolesWith(M.nova2('writer_llm')), tags: ['policy'], pinned: false,
    report: EU_AI_ACT_A, sources: EU_SOURCES, nSources: 38, exports: ['pdf', 'docx'],
    artifacts: [{ seed: '9c0d1e2f3a4b5c6d7e8f901a2b3c4d5e', filename: 'chart.png', title: 'Figure 1 — Obligation timeline by risk class', caption: 'Application dates of the AI Act by risk class, 2025–2027' }],
    usage: { in: 1_912_000, out: 18_930, calls: 57, searches: 19, pages: 12 }, relation: 'root', conversation: '2b6f7e2d-8c1a-4a0e-9c1d-0f1e2d3c4b5a',
  },
  {
    seed: '31d0', title: 'What changed in Bedrock pricing this month?', question: 'What changed in Amazon Bedrock pricing this month?',
    mode: 'auto', depth: 'shallow', status: 'completed', createdMin: 1560, durationSec: 38,
    roles: rolesWith(M.nano('writer_llm')), tags: [], pinned: false,
    report: PRICING_QUICK, sources: PRICING_SOURCES, nSources: 5, usage: { in: 18_400, out: 640, calls: 3, searches: 2, pages: 2 }, relation: 'root',
  },
  {
    seed: 'e77a', title: 'Agentic SOC market landscape 2026', question: 'Map the 2026 market for agentic SOC products: vendor archetypes, measured outcomes, and procurement questions.',
    mode: 'deep', depth: 'deep', status: 'completed', createdMin: 3100, durationSec: 611,
    roles: rolesWith(M.terra('writer_llm'), M.kimi('planner_llm')), selection: 'session_override', tags: ['market'], pinned: false, project: 'security',
    report: SOC_LANDSCAPE, sources: SOC_SOURCES, nSources: 52, exports: ['docx', 'pdf'],
    artifacts: [{ seed: '1a2b3c4d5e6f708192a3b4c5d6e7f809', filename: 'archetypes.png', title: 'Vendor archetypes by autonomy level', caption: 'Autonomy level vs. scope for the three archetypes' }],
    usage: { in: 2_870_000, out: 33_400, calls: 71, searches: 24, pages: 19 }, relation: 'root',
  },
  {
    seed: '0b3c', title: 'Compare LLM eval frameworks for RAG', question: 'Compare LLM evaluation frameworks for RAG pipelines.',
    mode: 'deep_clarify', depth: null, status: 'clarifying', createdMin: 2900, durationSec: null,
    roles: rolesWith(M.superN('writer_llm')), tags: [], pinned: false, report: null, sources: [], nSources: 0,
    usage: { in: 1_900, out: 210, calls: 1, searches: 0, pages: 0 }, relation: 'root',
    clarification: [{ q: 'Should the comparison focus on open-source frameworks, managed services (e.g. Bedrock Evaluations), or both? Any specific metrics (faithfulness, answer relevance, context recall)?', a: '' }],
    conversation: '5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e9f',
  },
  {
    seed: '55aa', title: 'Nemotron-only deep orchestration retry', question: 'Deep research with every role on Nemotron Nano: summarise the state of agent memory benchmarks.',
    mode: 'deep', depth: 'deep', status: 'failed', createdMin: 4300, durationSec: 96,
    roles: { router: M.nano('router_llm'), clarifier: M.nano('clarifier_llm'), shallow: M.nano('shallow_llm'), planner: M.nano('planner_llm'), researcher: M.nano('researcher_llm'), writer: M.nano('writer_llm') },
    selection: 'session_override', tags: ['experiment'], pinned: false, report: null, sources: [], nSources: 0,
    error: 'empty source registry: researchers produced no retrievable sources (planner emitted 0 sub-tasks)',
    usage: { in: 41_000, out: 2_100, calls: 6, searches: 0, pages: 0 }, relation: 'root',
  },
  {
    seed: '3f9a', title: 'S3 Vectors vs OpenSearch Serverless for small RAG workloads', question: VEC_Q,
    mode: 'deep', depth: 'deep', status: 'completed', createdMin: 1445, durationSec: 556,
    roles: rolesWith(M.superN('writer_llm')), tags: ['vector-stores', 'rag', 'cost'], pinned: true, project: 'platform-eval',
    report: S3_VECTORS, sources: VEC_SOURCES, nSources: 24, unverified: [], exports: ['pdf'],
    artifacts: [{ seed: '5e6f708192a3b4c5d6e7f8091a2b3c4d', filename: 'cost_by_scale.png', title: 'Monthly cost by vector count', caption: 'Estimated monthly cost, S3 Vectors vs OpenSearch Serverless, 10K to 1M vectors' }],
    usage: { in: 1_912_340, out: 31_877, calls: 57, searches: 11, pages: 14 }, relation: 'root', conversation: '2b6f7e2d-8c1a-4a0e-9c1d-0f1e2d3c4b5a',
  },
  {
    seed: 'c101', title: 'S3 Vectors filtering and metadata limits — follow-up', question: 'Focus on filtering and metadata limits of S3 Vectors for multi-tenant RAG.',
    mode: 'deep', depth: 'deep', status: 'completed', createdMin: 1380, durationSec: 244,
    roles: rolesWith(M.superN('writer_llm')), tags: ['vector-stores', 'rag'], pinned: false, project: 'platform-eval',
    report: FOLLOWUP_FILTERING, sources: VEC_SOURCES, nSources: 9, usage: { in: 640_000, out: 9_800, calls: 22, searches: 6, pages: 7 },
    relation: 'followup', parentSeed: '3f9a', rootSeed: '3f9a', changes: { models_changed: [], data_sources_changed: false, question_changed: true }, conversation: '2b6f7e2d-8c1a-4a0e-9c1d-0f1e2d3c4b5a',
  },
  {
    seed: 'd4e5', title: 'Bibliography of agentic memory papers 2025–2026', question: 'Build an annotated bibliography of agentic memory papers published in 2025–2026.',
    mode: 'deep', depth: 'deep', status: 'cancelled', createdMin: 5800, durationSec: 141,
    roles: rolesWith(M.deepseek('writer_llm')), tags: ['papers'], pinned: false, report: null, sources: VEC_SOURCES, nSources: 6,
    usage: { in: 310_000, out: 4_100, calls: 12, searches: 5, pages: 3 }, relation: 'root', error: 'cancelled by user',
  },
  {
    seed: 'a1b2', title: 'Is Nova 2 Lite available in eu-central-1?', question: 'Is Amazon Nova 2 Lite available in eu-central-1?',
    mode: 'shallow', depth: 'shallow', status: 'completed', createdMin: 7300, durationSec: 22,
    roles: rolesWith(M.nano('writer_llm')), tags: ['regions'], pinned: false, report: NOVA_REGION, sources: REGION_SOURCES, nSources: 2,
    usage: { in: 9_800, out: 310, calls: 2, searches: 1, pages: 1 }, relation: 'root',
  },
  {
    seed: 'f00d', title: 'State of MCP governance tooling', question: 'Survey MCP governance tooling: registries, allowlists, policy engines and audit — what exists in 2026 and what is missing?',
    mode: 'deep', depth: null, status: 'queued', createdMin: 1, durationSec: null,
    roles: rolesWith(M.superN('writer_llm')), tags: ['mcp', 'governance'], pinned: false, report: null, sources: VEC_SOURCES, nSources: 0,
    usage: { in: 0, out: 0, calls: 0, searches: 0, pages: 0 }, relation: 'root', progress: { phase: 'queued', pct: 0 },
  },
];

export function buildPackages(): Map<string, MockPackage> {
  const m = new Map<string, MockPackage>();
  for (const s of SEEDS) {
    const p = build(s);
    m.set(p.manifest.job_id, p);
  }
  return m;
}

export function summarize(p: MockPackage, progress?: { phase: string; pct: number }): PackageSummary {
  const m = p.manifest;
  const models = Object.fromEntries(ROLES.map((r) => [r, { model_id: m.models.roles[r].model_id, human_name: m.models.roles[r].human_name ?? null, lane: m.models.roles[r].lane }])) as PackageSummary['models'];
  return {
    job_id: m.job_id,
    title: m.title,
    question: m.question,
    mode: m.mode,
    depth: m.depth,
    status: m.status,
    created_at: m.timing.created_at,
    completed_at: m.timing.completed_at ?? null,
    models,
    cost_usd: m.cost.total_usd,
    counts: { sources: m.sources.length, citations_verified: m.citations.verified, citations_unverified: m.citations.unverified, artifacts: m.artifacts.length, exports: m.exports.length },
    tags: m.organization.tags,
    pinned: m.organization.pinned,
    lineage: { parent_package_id: m.lineage.parent_package_id ?? null, root_package_id: m.lineage.root_package_id, relation: m.lineage.relation },
    ...(progress ? { progress } : {}),
    error: m.error ?? null,
  };
}

/** Journal events for a live package, given the current progress percentage. */
export function journalFor(p: MockPackage, pct: number): RuntimeEvent[] {
  const id = p.manifest.job_id;
  const t0 = Date.parse(p.manifest.timing.created_at);
  const at = (k: number) => new Date(t0 + k * 1000).toISOString();
  const out: RuntimeEvent[] = [];
  let seq = 0;
  const push = (type: string, data: Record<string, unknown>, k: number) => out.push({ type, seq: ++seq, job_id: id, data: { ...data, at: at(k) } });
  push('status', { phase: 'queued', text: 'Job accepted; session aiq-job-… started' }, 0);
  if (pct <= 0) return out;
  push('route', { depth: 'deep', text: `Routing: deep research · clarifier off · models validated against matrix ${MATRIX.matrix.digest.slice(0, 8)}` }, 2);
  push('status', { phase: 'planning', text: `Planner (${p.manifest.models.roles.planner.human_name}) → 4 sub-tasks` }, 9);
  const nSrc = Math.min(p.manifest.sources.length, Math.floor((pct / 100) * p.manifest.sources.length));
  for (let i = 0; i < nSrc; i++) {
    const s = p.manifest.sources[i];
    if (i % 4 === 0) push('status', { phase: 'researching', text: `Researcher: web search "${(s.title ?? 'query').slice(0, 48)}"` }, 12 + i * 4);
    push('source', { source_id: s.source_id, url: s.url, title: s.title, kind: s.kind, tool: s.tool }, 13 + i * 4);
  }
  if (pct >= 70) push('artifact', { artifact_id: 'art_pending_chart', filename: 'chart.png', kind: 'image', text: 'Sandbox produced chart.png (checkpoint capture)' }, 200);
  if (pct >= 85) push('status', { phase: 'writing', text: `Writer (${p.manifest.models.roles.writer.human_name}) drafting the report` }, 230);
  if (pct >= 100) {
    push('citations', { verified: p.manifest.citations.verified, unverified: p.manifest.citations.unverified }, 280);
    push('usage', { input_tokens: p.manifest.usage.input_tokens, output_tokens: p.manifest.usage.output_tokens, seconds: p.manifest.timing.duration_seconds }, 281);
    push('completed', { text: 'Package written: manifest, report.md, ledger.json, sources.json' }, 282);
  }
  return out;
}

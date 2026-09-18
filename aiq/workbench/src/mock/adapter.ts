// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// MOCK adapter: same WorkbenchApi surface as the real runtime, backed by fixtures. Running packages advance with
// wall-clock time so the live progress views can be exercised; re-runs and evaluations create new live items.
import { ApiError, type WorkbenchApi } from '../api/client';
import type {
  Comparison,
  EvalRun,
  ExportFormat,
  JobStatus,
  Manifest,
  ModelsResponse,
  ModelsSelection,
  PackageSummary,
  RoleModelRef,
  Source,
} from '../types';
import { ROLES } from '../types';
import matrixJson from './matrix.json';
import { buildPackages, jid, journalFor, summarize, type MockPackage } from './packages';

const MATRIX = matrixJson as unknown as ModelsResponse;
const PREFS_KEY = 'aiq-wb-mock-prefs';
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const latency = () => sleep(180 + Math.random() * 320);
const notFound = (job_id: string) => new ApiError(`Package ${job_id} was not found in your library.`, 'not_found');

const EXT: Record<ExportFormat, string> = { md: 'md', html: 'html', pdf: 'pdf', docx: 'docx', pptx: 'pptx', json: 'json', csv: 'csv', bibtex: 'bib', ris: 'ris', csl: 'csl.json', zip: 'zip' };
const SIZE: Record<ExportFormat, number> = { md: 24_310, html: 98_112, pdf: 412_990, docx: 61_200, pptx: 188_400, json: 71_020, csv: 6_140, bibtex: 4_020, ris: 3_910, csl: 5_330, zip: 1_204_811 };

function svgChart(seed: string, title: string): string {
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const bars = Array.from({ length: 7 }, (_, i) => 30 + ((h >> (i * 4)) & 0xf) * 5);
  const rects = bars.map((v, i) => `<rect x="${60 + i * 60}" y="${190 - v}" width="40" height="${v}" rx="4" fill="url(#g)"/>`).join('');
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 240" width="520" height="240"><defs><linearGradient id="g" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#14b8a6"/><stop offset=".55" stop-color="#8b5cf6"/><stop offset="1" stop-color="#f59e0b"/></linearGradient></defs><rect width="520" height="240" rx="12" fill="#0f1522"/><text x="24" y="32" fill="#e8ecf3" font-family="system-ui" font-size="14" font-weight="600">${title.replace(/[<&>]/g, '')}</text><line x1="50" y1="190" x2="500" y2="190" stroke="#a5adbd" stroke-opacity=".4"/>${rects}<text x="24" y="222" fill="#7c8596" font-family="system-ui" font-size="10">mock artifact preview · rendered from fixture data</text></svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

function nowIso() {
  return new Date().toISOString();
}

export function createMockApi(): WorkbenchApi {
  const packages = buildPackages();
  let prefs: ModelsSelection | null = null;
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    prefs = raw ? (JSON.parse(raw) as ModelsSelection) : null;
  } catch {
    prefs = null;
  }
  const evals: EvalRun[] = [
    {
      eval_id: 'eval_20260918_01',
      created_at: new Date(Date.now() - 26 * 3600_000).toISOString(),
      models: { planner: { model_id: 'global.amazon.nova-2-lite-v1:0', lane: 'converse' }, researcher: { model_id: 'global.amazon.nova-2-lite-v1:0', lane: 'converse' }, writer: { model_id: 'nvidia.nemotron-super-3-120b', lane: 'converse' } },
      status: 'completed',
      judge: { model_id: 'global.anthropic.claude-opus-4-8', lane: 'converse' },
      rows: ['q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7'].map((q, i) => ({
        question_id: q,
        job_id: jid(`e${i}0${i}`),
        status: 'completed' as JobStatus,
        cost_usd: [0.21, 0.34, 0.18, 0.41, 0.27, 0.22, 0.3][i],
        seconds: [402, 511, 366, 598, 455, 389, 470][i],
        citations_verified: [17, 22, 12, 31, 19, 14, 24][i],
        citations_unverified: [0, 1, 0, 0, 0, 0, 1][i],
        judge_score: [4.2, 3.8, 4.5, 4.0, 3.6, 4.4, 4.1][i],
      })),
    },
  ];

  /** Advance live packages (running/queued/eval) with wall-clock time. */
  function tick(p: MockPackage): PackageSummary {
    if (!p.live) return summarize(p);
    const m = p.manifest;
    const elapsed = Date.now() - p.live.startedAt;
    const pct = Math.min(100, p.live.startPct + (elapsed / p.live.durationMs) * (100 - p.live.startPct));
    if (m.status === 'queued' && elapsed > 4000) {
      m.status = 'running';
      m.timing.started_at = nowIso();
    }
    if (pct >= 100) {
      m.status = p.live.finalStatus;
      m.timing.completed_at = nowIso();
      m.timing.updated_at = m.timing.completed_at;
      m.timing.duration_seconds = Math.round((Date.parse(m.timing.completed_at) - Date.parse(m.timing.created_at)) / 1000);
      if (!m.report.present) {
        // a completed re-run/queued job gets its parent's report if any, otherwise a generic one
        const parent = m.lineage.parent_package_id ? packages.get(m.lineage.parent_package_id) : undefined;
        p.report_md = parent?.report_md ?? `# ${m.title}\n\n## Summary\n\nThe run completed [1].\n\n## Sources\n\n[1] ${m.sources[0]?.url ?? 'https://docs.aws.amazon.com/'} — ${m.sources[0]?.title ?? 'Source'}\n`;
        m.report = { ...m.report, present: true, key: `tenants/${m.tenant_key}/packages/${m.job_id}/report.md`, size_bytes: p.report_md.length, word_count: p.report_md.split(/\s+/).length };
        if (parent) {
          m.citations = JSON.parse(JSON.stringify(parent.manifest.citations));
          m.sources = JSON.parse(JSON.stringify(parent.manifest.sources));
          m.artifacts = parent.manifest.artifacts.map((a) => ({ ...a, job_id: m.job_id }));
          m.usage = { ...parent.manifest.usage, input_tokens: Math.round(parent.manifest.usage.input_tokens * 1.08) };
          m.cost = { ...parent.manifest.cost, total_usd: Math.round(parent.manifest.cost.total_usd * 1.31 * 10000) / 10000 };
        }
      }
      p.live = undefined;
      return summarize(p);
    }
    const phase = m.status === 'queued' ? 'queued' : pct < 15 ? 'planning' : pct < 70 ? 'researching' : pct < 85 ? 'analysing' : 'writing';
    // sources arrive while researching
    const target = Math.floor((pct / 100) * p.live.sourcesTarget);
    while (m.sources.length < target && m.sources.length < p.live.sourcesTarget) {
      const i = m.sources.length;
      const s: Source = {
        source_id: `src_${(i.toString(16).padStart(2, '0') + m.job_id.slice(4) + '0000000000000000').slice(0, 16)}`,
        url: `https://docs.aws.amazon.com/live-source-${i + 1}/`,
        title: `Live source ${i + 1} retrieved while running`,
        kind: i % 3 === 0 ? 'web_search' : 'web_page',
        retrieved_at: nowIso(),
        tool: i % 3 === 0 ? 'agentcore_web_search' : 'agentcore_fetch_page',
        snippet: null,
        document_key: null,
        content_sha256: null,
        cited_by: [],
      };
      m.sources.push(s);
    }
    m.cost.total_usd = Math.round(((pct / 100) * 1.38 + 0.002) * 10000) / 10000;
    m.timing.updated_at = nowIso();
    return summarize(p, { phase, pct: Math.round(pct) });
  }

  function all(): PackageSummary[] {
    return [...packages.values()].map(tick).sort((a, b) => (b.pinned === a.pinned ? Date.parse(b.created_at) - Date.parse(a.created_at) : 0));
  }

  function refName(ref: RoleModelRef | undefined): string {
    return MATRIX.entries.find((e) => e.id === ref?.model_id)?.name ?? ref?.model_id ?? '—';
  }

  function compare(a: MockPackage, b: MockPackage): Comparison {
    const heads = (m: Manifest) => (m.report.headings ?? []).filter((h) => h.level === 2 && !/^sources$/i.test(h.text)).map((h) => h.text);
    const norm = (t: string) => t.replace(/^\d+[a-z]?\.\s*/i, '').toLowerCase();
    const ha = heads(a.manifest);
    const hb = heads(b.manifest);
    const titles = [...ha, ...hb.filter((t) => !ha.some((x) => norm(x) === norm(t)))];
    const sections = titles.map((title) => ({ title, in_a: ha.some((x) => norm(x) === norm(title)), in_b: hb.some((x) => norm(x) === norm(title)) }));
    const byUrl = (m: Manifest) => new Map(m.sources.map((s) => [s.url ?? s.source_id, s]));
    const ma = byUrl(a.manifest);
    const mb = byUrl(b.manifest);
    const shared: Source[] = [];
    const only_a: Source[] = [];
    const only_b: Source[] = [];
    for (const [k, s] of ma) (mb.has(k) ? shared : only_a).push(s);
    for (const [k, s] of mb) if (!ma.has(k)) only_b.push(s);
    const isEu = a.manifest.title?.startsWith('EU AI Act') && b.manifest.title?.startsWith('EU AI Act');
    const claims: Comparison['claims'] = isEu
      ? [
          { a: '"most high-risk obligations apply from August 2026"', b: '"high-risk obligations under Annex III apply from 2 August 2026; Annex I systems from 2 August 2027"', markers_a: ['[3]'], markers_b: ['[3]', '[5]'] },
          { a: null, b: '"sandbox participation does not waive documentation duties"', markers_a: [], markers_b: ['[12]'] },
          { a: null, b: '"fine-tuning above a compute threshold makes the downstream party a provider too"', markers_a: [], markers_b: ['[12]'] },
          { a: '"Open-source components released without commercial intent enjoy a separate, narrower carve-out"', b: null, markers_a: ['[4]'], markers_b: [] },
        ]
      : [
          { a: a.manifest.report.summary ?? null, b: b.manifest.report.summary ?? null, markers_a: ['[1]'], markers_b: ['[1]'] },
        ];
    return {
      a: tick(a),
      b: tick(b),
      sections,
      sources: { shared, only_a, only_b },
      claims,
      run: {
        cost_a: a.manifest.cost.total_usd,
        cost_b: b.manifest.cost.total_usd,
        seconds_a: a.manifest.timing.duration_seconds ?? 0,
        seconds_b: b.manifest.timing.duration_seconds ?? 0,
        citations_a: a.manifest.citations.verified,
        citations_b: b.manifest.citations.verified,
      },
    };
  }

  function validate(models: ModelsSelection) {
    const problems: { role: string; model_id: string; reason: string }[] = [];
    let low = 0.05;
    let high = 0.09;
    const volume: Record<string, [number, number]> = { router: [2_000, 200], clarifier: [3_000, 600], shallow: [20_000, 1_500], planner: [180_000, 20_000], researcher: [1_500_000, 25_000], writer: [40_000, 9_000] };
    for (const role of ROLES) {
      const ref = models[role];
      if (!ref) continue;
      const e = MATRIX.entries.find((x) => x.id === ref.model_id && (x.lane === ref.lane || !ref.lane)) ?? MATRIX.entries.find((x) => x.id === ref.model_id);
      if (!e) {
        problems.push({ role, model_id: ref.model_id, reason: 'not in the capability matrix' });
        continue;
      }
      if (!e.offered) problems.push({ role, model_id: ref.model_id, reason: `excluded: ${e.exclusion_reasons.join(', ')}` });
      else if (!e.roles_selectable[role]) problems.push({ role, model_id: ref.model_id, reason: `not selectable for ${role} (capability missing)` });
      const [vin, vout] = volume[role];
      const c = (vin / 1e6) * (e.price.input_per_1m ?? 0.5) + (vout / 1e6) * (e.price.output_per_1m ?? 2.5);
      low += c * 0.8;
      high += c * 1.4;
    }
    return { ok: problems.length === 0, problems, estimate_usd: { low: Math.round(low * 100) / 100, high: Math.round(high * 100) / 100 } };
  }

  return {
    mock: true,
    async packagesList(params) {
      await latency();
      let items = all();
      if (params.status) items = items.filter((p) => p.status === params.status);
      if (params.mode) items = items.filter((p) => p.mode === params.mode);
      if (params.tag) items = items.filter((p) => p.tags.includes(params.tag!));
      if (params.pinned) items = items.filter((p) => p.pinned);
      if (params.q) {
        const q = params.q.toLowerCase();
        items = items.filter((p) => (p.title ?? '').toLowerCase().includes(q) || p.question.toLowerCase().includes(q) || p.tags.some((t) => t.includes(q)) || p.job_id.includes(q));
      }
      const limit = Math.min(Math.max(params.limit ?? 8, 1), 50);
      const offset = params.cursor ? Number.parseInt(params.cursor, 10) || 0 : 0;
      const page = items.slice(offset, offset + limit);
      return { items: page, next_cursor: offset + limit < items.length ? String(offset + limit) : null };
    },
    async packagesGet(job_id) {
      await latency();
      const p = packages.get(job_id);
      if (!p) throw notFound(job_id);
      const summary = tick(p);
      const events_tail = p.live || p.manifest.status === 'running' ? journalFor(p, summary.progress?.pct ?? 100) : undefined;
      return { manifest: JSON.parse(JSON.stringify(p.manifest)) as Manifest, report_md: p.report_md, ...(events_tail ? { events_tail } : {}) };
    },
    async packagesUpdate(job_id, patch) {
      await latency();
      const p = packages.get(job_id);
      if (!p) throw notFound(job_id);
      if (patch.tags) {
        const bad = patch.tags.find((t) => !/^[a-z0-9][a-z0-9._-]{0,47}$/.test(t));
        if (bad) throw new ApiError(`Tag "${bad}" is invalid: lowercase letters, digits, . _ - only.`, 'invalid_request');
        p.manifest.organization.tags = [...new Set(patch.tags)];
      }
      if (patch.pinned !== undefined) p.manifest.organization.pinned = patch.pinned;
      if (patch.title !== undefined) p.manifest.title = patch.title.slice(0, 200);
      if (patch.notes !== undefined) p.manifest.organization.notes = patch.notes.slice(0, 4000);
      p.manifest.timing.updated_at = nowIso();
      return { manifest: JSON.parse(JSON.stringify(p.manifest)) as Manifest, report_md: p.report_md };
    },
    async packagesDelete(job_id) {
      await latency();
      const p = packages.get(job_id);
      if (!p) throw notFound(job_id);
      if (p.manifest.status === 'running') throw new ApiError('Stop the running job before deleting its package.', 'invalid_request');
      packages.delete(job_id);
      return { job_id };
    },
    async packagesCompare(job_id, other_job_id) {
      await latency();
      const a = packages.get(job_id);
      const b = packages.get(other_job_id);
      if (!a) throw notFound(job_id);
      if (!b) throw notFound(other_job_id);
      return compare(a, b);
    },
    async packagesRerun(job_id, models) {
      await latency();
      const parent = packages.get(job_id);
      if (!parent) throw notFound(job_id);
      if (models) {
        const v = validate(models);
        if (!v.ok) throw new ApiError(`Model selection rejected: ${v.problems.map((p) => `${p.role} → ${p.model_id} (${p.reason})`).join('; ')}`, 'invalid_request', undefined, v);
      }
      const newId = jid(Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, '0') + Date.now().toString(16));
      const m = JSON.parse(JSON.stringify(parent.manifest)) as Manifest;
      m.job_id = newId;
      m.package_id = newId;
      m.status = 'running';
      m.error = null;
      m.timing = { created_at: nowIso(), started_at: nowIso(), completed_at: null, updated_at: nowIso(), duration_seconds: null, clarification_turns: 0 };
      m.models.selection = 'rerun_override';
      const changed: string[] = [];
      for (const role of ROLES) {
        const ref = models?.[role];
        if (ref && ref.model_id !== m.models.roles[role].model_id) {
          const e = MATRIX.entries.find((x) => x.id === ref.model_id);
          m.models.roles[role] = { ...m.models.roles[role], model_id: ref.model_id, human_name: e?.name ?? ref.model_id, provider: e?.provider?.toLowerCase() ?? null, lane: ref.lane === 'mantle_chat' ? 'bedrock_mantle' : 'bedrock_runtime' };
          changed.push(role);
        }
      }
      m.models.roles.orchestrator = m.models.roles.planner;
      m.models.roles.source_router = m.models.roles.planner;
      m.lineage = { parent_package_id: job_id, relation: 'rerun', root_package_id: parent.manifest.lineage.root_package_id, result_kind: null, changes: { models_changed: changed, data_sources_changed: false, question_changed: false } };
      m.report = { present: false, key: null, sha256: null, size_bytes: null, word_count: null, format: 'text/markdown', headings: parent.manifest.report.headings, summary: null, guardrail_action: null };
      m.citations = { ledger_key: null, verified: 0, unverified: 0, items: [] };
      m.sources = [];
      m.artifacts = [];
      m.exports = [];
      m.cost = { ...m.cost, total_usd: 0, lines: [] };
      m.usage = { ...m.usage, input_tokens: 0, output_tokens: 0, llm_calls: 0, searches: 0, pages: 0 };
      m.organization = { ...m.organization, pinned: false };
      packages.set(newId, { manifest: m, report_md: '', live: { startedAt: Date.now(), durationMs: 80_000, startPct: 2, finalStatus: 'completed', sourcesTarget: Math.max(8, parent.manifest.sources.length) } });
      return { job_id: newId, parent_job_id: job_id, relation: 'rerun' };
    },
    async exportPackage(job_id, format, theme) {
      await sleep(500 + Math.random() * 900);
      const p = packages.get(job_id);
      if (!p) throw notFound(job_id);
      if (!p.manifest.report.present) throw new ApiError(`Package ${job_id} has no report yet (status: ${p.manifest.status}); nothing to export.`, 'invalid_state');
      const slug = (p.manifest.title ?? 'report').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 48);
      const cached = p.manifest.exports.some((e) => e.format === format);
      return { format, filename: `${slug}${theme ? `.${theme}` : ''}.${EXT[format]}`, size: SIZE[format], sha256: (format + p.manifest.job_id.slice(4) + '0'.repeat(64)).replace(/[^a-f0-9]/g, '0').slice(0, 64), url: '#mock', expires_in: 600, cached, backend: 'mock' };
    },
    async artifactUrl(job_id, artifact_id) {
      await sleep(120);
      const p = packages.get(job_id);
      if (!p) throw notFound(job_id);
      const a = p.manifest.artifacts.find((x) => x.artifact_id === artifact_id);
      if (!a) throw new ApiError(`Artifact ${artifact_id} is not part of package ${job_id}.`, 'not_found');
      return { url: svgChart(artifact_id, a.title ?? a.filename), expires_in: 600 };
    },
    async models() {
      await latency();
      return { ...MATRIX, prefs };
    },
    async modelsPrefs(models) {
      await latency();
      const v = validate(models);
      if (!v.ok) throw new ApiError(`Defaults rejected: ${v.problems.map((p) => `${p.role} → ${refName(models[p.role as keyof ModelsSelection])} (${p.reason})`).join('; ')}`, 'invalid_request', undefined, v);
      prefs = models;
      try {
        localStorage.setItem(PREFS_KEY, JSON.stringify(models));
      } catch {
        /* ignore */
      }
      return { prefs };
    },
    async modelsValidate(models) {
      await sleep(150);
      return validate(models);
    },
    async evalStart(models, question_ids, judge) {
      await latency();
      const v = validate(models);
      if (!v.ok) throw new ApiError(`Evaluation rejected: ${v.problems.map((p) => `${p.role} → ${p.model_id} (${p.reason})`).join('; ')}`, 'invalid_request', undefined, v);
      const qs = question_ids?.length ? question_ids : ['q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7'];
      const eval_id = `eval_${new Date().toISOString().slice(0, 10).replace(/-/g, '')}_${String(evals.length + 1).padStart(2, '0')}`;
      const job_ids = qs.map((q) => jid(Math.floor(Math.random() * 0xffffffff).toString(16) + q));
      evals.unshift({
        eval_id,
        created_at: nowIso(),
        models,
        status: 'running',
        judge: judge ? { model_id: judge, lane: 'converse' } : null,
        rows: qs.map((q, i) => ({ question_id: q, job_id: job_ids[i], status: 'running', cost_usd: null, seconds: null, citations_verified: null, citations_unverified: null, judge_score: null })),
      });
      return { eval_id, job_ids };
    },
    async evalList() {
      await latency();
      for (const e of evals) {
        if (e.status !== 'running') continue;
        const ageSec = (Date.now() - Date.parse(e.created_at)) / 1000;
        let done = 0;
        e.rows.forEach((r, i) => {
          if (ageSec > 12 + i * 9) {
            r.status = 'completed';
            r.cost_usd = Math.round((0.15 + i * 0.03) * 100) / 100;
            r.seconds = 380 + i * 21;
            r.citations_verified = 14 + i * 2;
            r.citations_unverified = i === 3 ? 1 : 0;
            r.judge_score = e.judge ? Math.round((3.7 + (i % 3) * 0.3) * 10) / 10 : null;
            done++;
          }
        });
        if (done === e.rows.length) e.status = 'completed';
      }
      return { items: JSON.parse(JSON.stringify(evals)) as EvalRun[] };
    },
    async events(job_id, after) {
      await sleep(200);
      const p = packages.get(job_id);
      if (!p) throw notFound(job_id);
      const s = tick(p);
      return journalFor(p, s.progress?.pct ?? (p.manifest.status === 'completed' ? 100 : 0)).filter((e) => (e.seq ?? 0) > after);
    },
    async health() {
      await sleep(80);
      return { version: '0.9.0+mock', source_commit: 'mock', runtime_version: 'mock', matrix_digest: MATRIX.matrix.digest };
    },
  };
}

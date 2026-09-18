// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Model Lab: KPI tiles, capability matrix (filters, dots, scores, prices, exclusions), picker with estimate,
// leaderboard per role, evaluations with a disclosed judge.
import { useEffect, useMemo, useState } from 'react';
import { useApi } from '../api';
import { toApiError, type ApiError } from '../api/client';
import { Chip, StatusChip } from '../components/Chip';
import { Dialog } from '../components/Dialog';
import { KpiTile } from '../components/Kpi';
import { ModelName, laneLabel } from '../components/ModelName';
import { EstimateLine, RoleSelector, scoreClass, selectionFromDefaults } from '../components/RoleSelector';
import { EmptyState, ErrorBanner, InfoBanner, TableSkeleton } from '../components/States';
import { Tabs } from '../components/Tabs';
import { useToast } from '../components/Toast';
import { useConfig } from '../config';
import { copyText } from '../lib/clipboard';
import { ago, fmtDateTimeZ, ms, num, pct, usd } from '../lib/format';
import { useModels } from '../lib/models';
import { modelCommand, owuiHomeUrl } from '../lib/owui';
import { useAsync } from '../lib/useAsync';
import type { EvalRun, LabRole, MatrixEntry, ModelsSelection, ModelsValidated, Role } from '../types';
import { LAB_ROLES, ROLES, ROLE_LABEL } from '../types';

const DOTS: { key: keyof MatrixEntry['capabilities']; label: string }[] = [
  { key: 'plain', label: 'plain' },
  { key: 'stream', label: 'stream' },
  { key: 'tools', label: 'tools' },
  { key: 'json', label: 'JSON' },
  { key: 'long', label: 'long' },
  { key: 'reasoning', label: 'reasoning' },
];
const LANES = ['converse', 'mantle_chat', 'mantle_responses', 'mantle_messages'];
const QUESTIONS = [
  { id: 'q1', label: 'Factual quick answer' },
  { id: 'q2', label: 'Two-vendor comparison' },
  { id: 'q3', label: 'Policy obligations summary' },
  { id: 'q4', label: 'Market landscape with chart' },
  { id: 'q5', label: 'Technical limits look-up' },
  { id: 'q6', label: 'Ambiguous request (clarifier)' },
  { id: 'q7', label: 'Follow-up on a prior package' },
];

function Caps({ e }: { e: MatrixEntry }) {
  const title = (['plain', 'system', 'stream', 'tools', 'json', 'long', 'reasoning'] as const).map((k) => `${k}: ${e.capabilities[k].ok ? 'ok' : e.capabilities[k].error_code ?? 'failed'}${e.capabilities[k].ms ? ` (${e.capabilities[k].ms} ms)` : ''}`).join('\n');
  return (
    <span className="caps" title={title} aria-label={title.replace(/\n/g, ', ')}>
      {DOTS.map((d) => {
        const c = e.capabilities[d.key];
        const cls = c.ok ? 'cap-ok' : c.error_code === 'not_probed' || (!e.capabilities.plain.ok && d.key !== 'plain') ? 'cap-na' : 'cap-no';
        return <i key={d.key} className={`cap ${cls}`} />;
      })}
    </span>
  );
}

function Score({ e, role }: { e: MatrixEntry; role: LabRole }) {
  const r = e.roles[role];
  if (!e.offered || !r || r.verdict === 'not_evaluated') return <span className="score score-na" title={r?.verdict ?? 'not evaluated'}>—</span>;
  return (
    <span className={scoreClass(r.score, r.verdict)} title={r.verdict}>
      {r.score}
    </span>
  );
}

export default function ModelLabPage() {
  const api = useApi();
  const cfg = useConfig();
  const toast = useToast();
  const models = useModels();
  const [lane, setLane] = useState('');
  const [provider, setProvider] = useState('');
  const [offeredOnly, setOfferedOnly] = useState(false);
  const [role, setRole] = useState<LabRole | ''>('');
  const [q, setQ] = useState('');
  const [why, setWhy] = useState<string | null>(null);
  const [sel, setSel] = useState<ModelsSelection | null>(null);
  const [validation, setValidation] = useState<ModelsValidated | null>(null);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [leaderRole, setLeaderRole] = useState<LabRole>('writer');
  const [evalOpen, setEvalOpen] = useState(false);

  const data = models.data;
  useEffect(() => {
    if (data && !sel) setSel(selectionFromDefaults(data));
  }, [data, sel]);

  useEffect(() => {
    if (!sel) return;
    let cancelled = false;
    setValidating(true);
    const t = window.setTimeout(() => {
      api
        .modelsValidate(sel)
        .then((v) => !cancelled && setValidation(v))
        .catch((e: unknown) => !cancelled && toast.push('err', toApiError(e).message))
        .finally(() => !cancelled && setValidating(false));
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [api, sel, toast]);

  const providers = useMemo(() => [...new Set((data?.entries ?? []).map((e) => e.provider))].sort(), [data]);
  const rows = useMemo(() => {
    let xs = data?.entries ?? [];
    if (lane) xs = xs.filter((e) => e.lane === lane);
    if (provider) xs = xs.filter((e) => e.provider === provider);
    if (offeredOnly) xs = xs.filter((e) => e.offered);
    if (q.trim()) {
      const t = q.trim().toLowerCase();
      xs = xs.filter((e) => e.name.toLowerCase().includes(t) || e.id.toLowerCase().includes(t) || e.provider.toLowerCase().includes(t));
    }
    const sortRole: LabRole = role || 'writer';
    return [...xs].sort((a, b) => Number(b.offered) - Number(a.offered) || (b.roles[sortRole]?.score ?? 0) - (a.roles[sortRole]?.score ?? 0) || a.name.localeCompare(b.name));
  }, [data, lane, provider, offeredOnly, q, role]);

  const summary = data?.matrix.summary;
  const offeredN = summary?.offered ?? data?.entries.filter((e) => e.offered).length ?? 0;
  const excludedN = summary?.excluded ?? (data ? data.entries.length - offeredN : 0);

  const save = async () => {
    if (!sel) return;
    setSaving(true);
    try {
      const r = await api.modelsPrefs(sel);
      if (data) models.setData({ ...data, prefs: r.prefs });
      toast.push('ok', 'Saved as your defaults for new research (the runtime resolves request → your defaults → deployment defaults).');
    } catch (e) {
      toast.push('err', toApiError(e).message, 8000);
    } finally {
      setSaving(false);
    }
  };
  const useOnce = async () => {
    if (!sel) return;
    const cmd = modelCommand(sel);
    const ok = await copyText(cmd);
    toast.push(ok ? 'ok' : 'warn', ok ? <span>Copied <code>{cmd}</code> — paste it as the first line of your next chat message.</span> : <span>Could not copy. Command: <code>{cmd}</code></span>, 9000);
    window.open(owuiHomeUrl(cfg), '_blank', 'noopener');
  };

  const scoreCols: LabRole[] = role && role !== 'writer' && role !== 'planner' ? [role, 'writer'] : ['writer', 'planner'];

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Model Lab</h1>
          <div>Every model this account can reach, probed live on each lane. Nothing is offered that did not pass; nothing is substituted silently.</div>
        </div>
        <div className="actions">
          <button type="button" className="btn" onClick={() => setEvalOpen(true)} disabled={!data}>
            Run evaluation ▸
          </button>
          <button type="button" className="btn btn-primary" disabled title="Operator action: the probe sweep runs from the deployment scripts (aiq/modellab), not from the browser.">
            Re-probe catalogue
          </button>
        </div>
      </div>

      {models.error ? <ErrorBanner error={models.error} title="Could not load the capability matrix" onRetry={models.reload} /> : null}

      <div className="grid grid-4 kpi-grid">
        <KpiTile loading={!data} value={num(data?.entries.length)} label="lane entries discovered" hint={summary?.catalog_generated_at ? `catalogue ${fmtDateTimeZ(String(summary.catalog_generated_at))}` : undefined} />
        <KpiTile loading={!data} value={num(offeredN)} label="offered" bar={data ? (offeredN / Math.max(1, data.entries.length)) * 100 : 0} />
        <KpiTile loading={!data} value={num(excludedN)} label="excluded · with reasons" />
        <KpiTile loading={!data} value={summary?.sweep_cost_usd !== undefined ? usd(Number(summary.sweep_cost_usd)) : '—'} label="probe sweep cost" hint={data ? `probed ${fmtDateTimeZ(data.matrix.generated_at)} as ${String(data.matrix.probed_as).split('/').pop()}` : undefined} />
      </div>

      <div className="two-col lab">
        <div className="stack">
          <div className="glass filters" style={{ marginBottom: 0 }}>
            <input className="input" aria-label="Search models" placeholder="Search model, id, provider…" value={q} onChange={(e) => setQ(e.target.value)} />
            <select className="select" aria-label="Lane" value={lane} onChange={(e) => setLane(e.target.value)}>
              <option value="">Lane: any</option>
              {LANES.map((l) => (
                <option key={l} value={l}>
                  {laneLabel(l)}
                </option>
              ))}
            </select>
            <select className="select" aria-label="Provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="">Provider: any</option>
              {providers.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <select className="select" aria-label="Sort by role" value={role} onChange={(e) => setRole(e.target.value as LabRole | '')}>
              <option value="">Role: writer (default)</option>
              {LAB_ROLES.map((r) => (
                <option key={r} value={r}>
                  Role: {ROLE_LABEL[r].toLowerCase()}
                </option>
              ))}
            </select>
            <label className="check">
              <input type="checkbox" checked={offeredOnly} onChange={(e) => setOfferedOnly(e.target.checked)} /> offered only
            </label>
          </div>

          <div className="glass table-wrap">
            {!data && !models.error ? (
              <TableSkeleton rows={8} />
            ) : rows.length === 0 ? (
              <EmptyState title="No models match">Loosen the lane/provider filters or clear the search.</EmptyState>
            ) : (
              <table className="table keep-cols" aria-label="Capability matrix">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Lane</th>
                    <th title="plain · stream · tools · JSON · long · reasoning">plain · stream · tools · JSON · long · reasoning</th>
                    {scoreCols.map((c) => (
                      <th key={c} className="num">
                        {ROLE_LABEL[c]}
                      </th>
                    ))}
                    <th className="num">TTFT</th>
                    <th className="num">$ in / out per 1M</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((e) => {
                    const key = `${e.lane}::${e.id}`;
                    const est = e.price.source !== 'aws-published' && e.price.input_per_1m !== null;
                    const tip = e.offered ? `offered · ${e.attempts.length} attempt${e.attempts.length === 1 ? '' : 's'}` : `${e.exclusion_reasons.join('\n')}${e.raw_error ? `\n\n${e.raw_error}` : ''}`;
                    return (
                      <FragmentRows key={key}>
                        <tr className={e.offered ? '' : 'excluded'}>
                          <td>
                            <ModelName id={e.id} name={e.name} />
                            <div className="faint">{e.provider}</div>
                          </td>
                          <td className="nowrap">
                            {laneLabel(e.lane)}
                            {e.routing && e.routing !== 'in_region' ? <span className="faint"> · {e.routing}</span> : null}
                            <div className="faint">{e.region}</div>
                          </td>
                          <td>
                            <Caps e={e} />
                          </td>
                          {scoreCols.map((c) => (
                            <td key={c} className="num">
                              <Score e={e} role={c} />
                            </td>
                          ))}
                          <td className="num nowrap">{ms(e.latency.ttft_ms ?? e.latency.plain_ms)}</td>
                          <td className="num nowrap">
                            {e.price.input_per_1m === null ? (
                              <span className="faint">— unpriced</span>
                            ) : (
                              <>
                                {e.price.input_per_1m.toFixed(2)} / {e.price.output_per_1m?.toFixed(2)}
                                {est ? (
                                  <>
                                    {' '}
                                    <Chip tone="warn" title={`price source: ${e.price.source} (not AWS-published)`}>
                                      est.
                                    </Chip>
                                  </>
                                ) : null}
                              </>
                            )}
                          </td>
                          <td className="nowrap">
                            {e.offered ? (
                              <Chip tone="ok" title={tip}>
                                offered
                              </Chip>
                            ) : (
                              <Chip tone={e.exclusion_reasons.some((r) => r.startsWith('lane:')) && !e.exclusion_reasons.some((r) => r.startsWith('probe:plain')) ? 'warn' : 'err'} title={tip} onClick={() => setWhy(why === key ? null : key)} pressed={why === key}>
                                excluded {why === key ? '▴' : '▾'}
                              </Chip>
                            )}
                          </td>
                        </tr>
                        {why === key ? (
                          <tr className="excluded">
                            <td colSpan={7 + scoreCols.length - 2}>
                              <div className="reasons">
                                <div>
                                  <b>Why excluded:</b> {e.exclusion_reasons.map((r) => <code key={r} style={{ marginRight: 8 }}>{r}</code>)}
                                </div>
                                {e.raw_error ? <div style={{ marginTop: 4 }}>Raw error: {e.raw_error}</div> : null}
                                {e.attempts.length ? <div style={{ marginTop: 4 }}>Attempts: {e.attempts.map((a) => `${a.at ? fmtDateTimeZ(a.at) : '?'} → ${a.plain_ok ? 'ok' : a.error_code ?? 'failed'}`).join(' · ')}</div> : null}
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </FragmentRows>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
          <div className="faint">
            {rows.length} of {data?.entries.length ?? 0} entries · matrix digest <span className="mono">{data?.matrix.digest.slice(0, 12) ?? '—'}</span> · offered ⇔ plain + stream passed on a wired lane, not data-retention-gated, no intermittent access; tool roles additionally need <code>tools</code>.
          </div>
        </div>

        <aside className="aside">
          <div className="glass card">
            <div className="h">Models for my next research</div>
            {!data || !sel ? (
              <TableSkeleton rows={3} />
            ) : (
              <>
                <RoleSelector models={data} value={sel} onChange={setSel} validation={validation} disabled={saving} />
                <div style={{ marginTop: 12 }}>
                  <EstimateLine validation={validation} loading={validating} />
                </div>
                <div className="row" style={{ marginTop: 12 }}>
                  <button type="button" className="btn btn-primary grow" onClick={save} disabled={saving || (validation ? !validation.ok : false)}>
                    {saving ? <span className="spin" aria-hidden="true" /> : null} Save as my defaults
                  </button>
                  <button type="button" className="btn" onClick={useOnce} disabled={validation ? !validation.ok : false}>
                    Use once in chat
                  </button>
                </div>
                <div className="faint" style={{ marginTop: 8 }}>
                  Also settable in chat: <code>{sel.writer ? `/model writer=${sel.writer.model_id}` : '/models'}</code>
                  {data.prefs ? <div style={{ marginTop: 4 }}>Your saved defaults: {ROLES.filter((r) => data.prefs?.[r]).map((r) => `${r}=${models.nameFor(data.prefs![r]!.model_id)}`).join(' · ')}</div> : <div style={{ marginTop: 4 }}>No saved defaults yet — deployment defaults apply.</div>}
                </div>
              </>
            )}
          </div>

          <div className="glass card">
            <div className="h">Leaderboard</div>
            <Tabs tabs={LAB_ROLES.map((r) => ({ id: r, label: ROLE_LABEL[r] }))} value={leaderRole} onChange={setLeaderRole} label="Leaderboard role" />
            {data ? (
              <table className="leader" aria-label={`Leaderboard for ${ROLE_LABEL[leaderRole]}`}>
                <tbody>
                  {data.entries
                    .filter((e) => e.offered && e.roles[leaderRole] && e.roles[leaderRole].verdict !== 'not_evaluated')
                    .sort((a, b) => b.roles[leaderRole].score - a.roles[leaderRole].score || (a.latency.plain_ms ?? 1e9) - (b.latency.plain_ms ?? 1e9))
                    .filter((e, i, arr) => arr.findIndex((x) => x.id === e.id) === i)
                    .slice(0, 8)
                    .map((e, i) => (
                      <tr key={`${e.lane}::${e.id}`}>
                        <td className="faint">{i + 1}</td>
                        <td>
                          <ModelName id={e.id} name={e.name} showId={false} />
                          <div className="faint">{laneLabel(e.lane)}{e.price.input_per_1m !== null ? ` · $${e.price.input_per_1m}/${e.price.output_per_1m}` : ''}</div>
                        </td>
                        <td className="right">
                          <Score e={e} role={leaderRole} />
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            ) : (
              <TableSkeleton rows={4} />
            )}
            <div className="faint" style={{ marginTop: 6 }}>Deterministic checks (markers valid, Sources section, length, JSON shape). Judged quality lives in the evaluations below, with the judge disclosed.</div>
          </div>
        </aside>
      </div>

      <Evaluations onRun={() => setEvalOpen(true)} />

      {evalOpen && data ? (
        <EvalDialog
          open
          onClose={() => setEvalOpen(false)}
          onStarted={(id, n) => {
            setEvalOpen(false);
            toast.push('ok', `Evaluation ${id} started: ${n} question${n === 1 ? '' : 's'} dispatched as jobs with lineage.relation = eval.`);
          }}
        />
      ) : null}
    </>
  );
}

function FragmentRows({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

function Evaluations({ onRun }: { onRun: () => void }) {
  const api = useApi();
  const models = useModels(false);
  const evals = useAsync(() => api.evalList(), [api]);
  const [open, setOpen] = useState<string | null>(null);
  const anyRunning = evals.data?.items.some((e) => e.status === 'running' || e.status === 'queued') ?? false;
  useEffect(() => {
    if (!anyRunning) return;
    const t = window.setInterval(() => evals.reload(), 5000);
    return () => window.clearInterval(t);
  }, [anyRunning, evals]);

  const agg = (e: EvalRun) => {
    const done = e.rows.filter((r) => r.status === 'completed');
    const sum = (f: (r: EvalRun['rows'][number]) => number | null | undefined) => done.reduce((s, r) => s + (f(r) ?? 0), 0);
    const cv = sum((r) => r.citations_verified);
    const cu = sum((r) => r.citations_unverified);
    const judged = done.filter((r) => r.judge_score !== null && r.judge_score !== undefined);
    return { done: done.length, cost: sum((r) => r.cost_usd), secs: done.length ? sum((r) => r.seconds) / done.length : null, verified: cv + cu ? pct(cv, cv + cu) : '—', judge: judged.length ? (judged.reduce((s, r) => s + (r.judge_score ?? 0), 0) / judged.length).toFixed(2) : '—' };
  };

  return (
    <section className="glass card" style={{ marginTop: 16 }}>
      <div className="row between" style={{ marginBottom: 8 }}>
        <h2>
          Evaluations <span className="faint">({evals.data?.items.length ?? 0})</span>
        </h2>
        <button type="button" className="btn btn-sm" onClick={onRun}>
          Run evaluation ▸
        </button>
      </div>
      {evals.error ? (
        <ErrorBanner error={evals.error} onRetry={evals.reload} />
      ) : !evals.data ? (
        <TableSkeleton rows={2} />
      ) : evals.data.items.length === 0 ? (
        <EmptyState title="No evaluations yet" action={<button type="button" className="btn btn-primary" onClick={onRun}>Run the standard question set</button>}>
          Run the 7-question set against a roles configuration; each question becomes a job with lineage <code>eval</code>.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table keep-cols" aria-label="Evaluation runs">
            <thead>
              <tr>
                <th>Run</th>
                <th>Models</th>
                <th>Status</th>
                <th className="num">Questions</th>
                <th className="num">Cost</th>
                <th className="num">Avg time</th>
                <th className="num">Citations verified</th>
                <th className="num">Judged</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {evals.data.items.map((e) => {
                const a = agg(e);
                return (
                  <FragmentRows key={e.eval_id}>
                    <tr>
                      <td>
                        <div className="title mono">{e.eval_id}</div>
                        <div className="sub">{ago(e.created_at)}</div>
                      </td>
                      <td>
                        {(['writer', 'planner', 'researcher'] as Role[]).filter((r) => e.models[r]).map((r) => (
                          <div key={r} className="xs">
                            <span className="faint">{ROLE_LABEL[r]}:</span> <span className="model-name">{models.nameFor(e.models[r]!.model_id)}</span>
                          </div>
                        ))}
                      </td>
                      <td>
                        <StatusChip status={e.status} progress={e.status === 'running' ? { phase: 'running', pct: (a.done / Math.max(1, e.rows.length)) * 100 } : undefined} />
                      </td>
                      <td className="num">
                        {a.done}/{e.rows.length}
                      </td>
                      <td className="num">{usd(a.cost)}</td>
                      <td className="num">{a.secs === null ? '—' : `${Math.round(a.secs)} s`}</td>
                      <td className="num">{a.verified}</td>
                      <td className="num" title={e.judge ? `judge: ${models.nameFor(e.judge.model_id)} (${e.judge.model_id})` : 'deterministic metrics only'}>
                        {a.judge}
                        {e.judge ? <div className="faint">judge {models.nameFor(e.judge.model_id)}</div> : <div className="faint">no judge</div>}
                      </td>
                      <td className="actions">
                        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setOpen(open === e.eval_id ? null : e.eval_id)} aria-expanded={open === e.eval_id}>
                          {open === e.eval_id ? 'Hide' : 'Rows'}
                        </button>
                      </td>
                    </tr>
                    {open === e.eval_id ? (
                      <tr>
                        <td colSpan={9}>
                          {e.judge ? (
                            <div style={{ marginBottom: 8 }}>
                              <InfoBanner tone="warn">
                                Judge disclosed: <b>{models.nameFor(e.judge.model_id)}</b> ({e.judge.model_id}). Bias note: a judge from the same family as a candidate can favour it; compare judged scores with the deterministic metrics.
                              </InfoBanner>
                            </div>
                          ) : null}
                          <table className="table tight" aria-label={`Rows of ${e.eval_id}`}>
                            <thead>
                              <tr>
                                <th>Question</th>
                                <th>Job</th>
                                <th>Status</th>
                                <th className="num">Cost</th>
                                <th className="num">Seconds</th>
                                <th className="num">Verified</th>
                                <th className="num">Unverified</th>
                                <th className="num">Judge</th>
                              </tr>
                            </thead>
                            <tbody>
                              {e.rows.map((r) => (
                                <tr key={r.question_id}>
                                  <td>
                                    {r.question_id} <span className="faint">{QUESTIONS.find((x) => x.id === r.question_id)?.label ?? ''}</span>
                                  </td>
                                  <td className="mono xs">{r.job_id}</td>
                                  <td>
                                    <StatusChip status={r.status} />
                                  </td>
                                  <td className="num">{usd(r.cost_usd)}</td>
                                  <td className="num">{r.seconds ?? '—'}</td>
                                  <td className="num">{r.citations_verified ?? '—'}</td>
                                  <td className="num">{r.citations_unverified ?? '—'}</td>
                                  <td className="num">{r.judge_score ?? '—'}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </td>
                      </tr>
                    ) : null}
                  </FragmentRows>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EvalDialog({ open, onClose, onStarted }: { open: boolean; onClose: () => void; onStarted: (evalId: string, n: number) => void }) {
  const api = useApi();
  const models = useModels(false);
  const data = models.data!;
  const [sel, setSel] = useState<ModelsSelection>(() => selectionFromDefaults(data));
  const [judge, setJudge] = useState<string>('');
  const [qs, setQs] = useState<string[]>(QUESTIONS.map((q) => q.id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [validation, setValidation] = useState<ModelsValidated | null>(null);
  useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(() => {
      api.modelsValidate(sel).then((v) => !cancelled && setValidation(v)).catch(() => undefined);
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [api, sel]);
  const judges = data.entries.filter((e) => e.offered && e.roles_selectable.writer && e.capabilities.json.ok).filter((e, i, arr) => arr.findIndex((x) => x.id === e.id) === i).sort((a, b) => b.roles.writer.score - a.roles.writer.score);
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.evalStart(sel, qs, judge || undefined);
      onStarted(r.eval_id, r.job_ids.length);
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  };
  const estCost = validation ? { low: validation.estimate_usd.low * qs.length, high: validation.estimate_usd.high * qs.length } : null;
  return (
    <Dialog
      open={open}
      title="Run evaluation"
      subtitle="The standard question set runs as real jobs under the chosen roles; metrics are deterministic, the judge is optional and always disclosed."
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={start} disabled={busy || qs.length === 0 || (validation ? !validation.ok : false)}>
            {busy ? <span className="spin" aria-hidden="true" /> : '▸'} Start {qs.length} job{qs.length === 1 ? '' : 's'}
            {estCost ? ` · ~${usd(estCost.low)}–${usd(estCost.high)}` : ''}
          </button>
        </>
      }
    >
      <div className="grid grid-2">
        <div className="stack">
          <div style={{ fontWeight: 700 }}>Roles under test</div>
          <RoleSelector models={data} value={sel} onChange={setSel} validation={validation} disabled={busy} />
          <EstimateLine validation={validation} />
        </div>
        <div className="stack">
          <label className="field">
            <span>Judge (optional, disclosed)</span>
            <select className="select" value={judge} onChange={(e) => setJudge(e.target.value)}>
              <option value="">No judge — deterministic metrics only</option>
              {judges.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name} · {e.id}
                </option>
              ))}
            </select>
          </label>
          <div className="field">
            <span>Questions ({qs.length} of {QUESTIONS.length})</span>
            {QUESTIONS.map((q) => (
              <label key={q.id} className="check">
                <input type="checkbox" checked={qs.includes(q.id)} onChange={(e) => setQs((xs) => (e.target.checked ? [...xs, q.id] : xs.filter((x) => x !== q.id)))} /> {q.id} · {q.label}
              </label>
            ))}
          </div>
          {error ? <ErrorBanner error={error} title="Evaluation was not started" /> : null}
        </div>
      </div>
    </Dialog>
  );
}

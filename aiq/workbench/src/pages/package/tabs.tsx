// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Package tabs: Report, Sources, Artifacts, Run, Lineage, Journal.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useApi } from '../../api';
import { toApiError } from '../../api/client';
import { Chip, StatusChip } from '../../components/Chip';
import { ModelName, laneLabel } from '../../components/ModelName';
import { EmptyState, ErrorBanner, InfoBanner, Skeleton, TableSkeleton } from '../../components/States';
import { useToast } from '../../components/Toast';
import { ago, bytes, duration, fmtDate, fmtDateTimeZ, hostOf, num, shortId, shortSha, tokens, usd } from '../../lib/format';
import { renderMarkdown, splitSourcesSection } from '../../lib/markdown';
import { useAsync } from '../../lib/useAsync';
import type { Artifact, Manifest, PackageSummary, RuntimeEvent, Source } from '../../types';
import { ROLES, ROLE_LABEL, isTerminal } from '../../types';

// ---------------------------------------------------------------- Report
export function ReportTab({ manifest, reportMd }: { manifest: Manifest; reportMd: string }) {
  const api = useApi();
  const ref = useRef<HTMLElement>(null);
  const { body } = useMemo(() => splitSourcesSection(reportMd), [reportMd]);
  const html = useMemo(() => renderMarkdown(body), [body]);

  // resolve artifact placeholders (#art-<id>) to presigned URLs after mount
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    let cancelled = false;
    const imgs = [...root.querySelectorAll<HTMLImageElement>('img[src^="#art-"]')];
    for (const img of imgs) {
      const id = img.getAttribute('src')!.slice(5);
      const art = manifest.artifacts.find((a) => a.artifact_id === id);
      if (!art) {
        img.replaceWith(Object.assign(document.createElement('div'), { className: 'faint', textContent: `Artifact ${id} is not in this package.` }));
        continue;
      }
      api
        .artifactUrl(manifest.job_id, id)
        .then((r) => {
          if (cancelled) return;
          img.src = r.url;
          img.alt = art.caption ?? art.title ?? art.filename;
          img.title = `${art.filename} · ${bytes(art.size_bytes)}`;
        })
        .catch(() => {
          if (!cancelled) img.replaceWith(Object.assign(document.createElement('div'), { className: 'faint', textContent: `Could not load ${art.filename}.` }));
        });
    }
    return () => {
      cancelled = true;
    };
  }, [html, api, manifest]);

  if (!manifest.report.present || !reportMd) {
    return (
      <EmptyState title={manifest.status === 'completed' ? 'No report in this package' : `Report not written yet (${manifest.status})`}>
        {manifest.status === 'running' || manifest.status === 'queued' ? 'Follow progress in the Journal tab; the report appears here when the writer finishes.' : manifest.status === 'clarifying' ? 'The clarifier is waiting for your answer in chat.' : manifest.error ? `The run ended with: ${manifest.error}` : null}
      </EmptyState>
    );
  }
  const citeByMarker = new Map(manifest.citations.items.map((c) => [c.marker, c]));
  return (
    <article className="report">
      <p className="question">
        Question: <em>{manifest.question}</em>
      </p>
      {manifest.clarification?.length ? (
        <InfoBanner>
          Clarification: {manifest.clarification.map((c) => `${c.q} → ${c.a || '(pending)'}`).join(' · ')}
        </InfoBanner>
      ) : null}
      <section ref={ref} dangerouslySetInnerHTML={{ __html: html }} />
      <h2 id="sources" style={{ marginTop: 28 }}>
        Sources <span className="faint">({manifest.sources.length} · {manifest.citations.verified} verified · {manifest.citations.unverified} unverified)</span>
      </h2>
      <SourceList sources={manifest.sources} citeByMarker={citeByMarker} />
    </article>
  );
}

function SourceList({ sources, citeByMarker }: { sources: Source[]; citeByMarker: Map<string, Manifest['citations']['items'][number]> }) {
  // one row per marker (so #src-n anchors resolve), ordered by marker number; uncited sources listed after
  const rows: { marker: string; n: number; src: Source | null; verified: boolean | null; reason?: string | null }[] = [];
  for (const [marker, c] of citeByMarker) {
    const n = Number(marker.replace(/\D/g, ''));
    const src = sources.find((s) => s.source_id === c.source_id) ?? null;
    rows.push({ marker, n, src, verified: c.verified, reason: c.reason });
  }
  rows.sort((a, b) => a.n - b.n);
  const uncited = sources.filter((s) => !s.cited_by?.length && !rows.some((r) => r.src?.source_id === s.source_id));
  return (
    <div>
      {rows.map((r) => (
        <div className="src" id={`src-${r.n}`} key={r.marker}>
          <b>{r.marker}</b>
          <div>
            {r.src?.url ? (
              <a href={r.src.url} target="_blank" rel="noopener noreferrer">
                {r.src.title ?? r.src.url}
              </a>
            ) : (
              <span>{r.src?.title ?? citeByMarker.get(r.marker)?.url ?? 'unresolved'}</span>
            )}{' '}
            <span className="host">{hostOf(r.src?.url ?? citeByMarker.get(r.marker)?.url)}</span>
            {r.reason ? <div className="faint">{r.reason}</div> : null}
          </div>
          {r.verified ? <Chip tone="ok">verified</Chip> : <Chip tone="err" title={r.reason ?? 'unverified'}>unverified</Chip>}
        </div>
      ))}
      {uncited.length ? (
        <>
          <div className="faint" style={{ margin: '12px 0 4px' }}>
            Retrieved but not cited ({uncited.length})
          </div>
          {uncited.slice(0, 30).map((s) => (
            <div className="src" key={s.source_id}>
              <b>·</b>
              <div>
                {s.url ? (
                  <a href={s.url} target="_blank" rel="noopener noreferrer">
                    {s.title ?? s.url}
                  </a>
                ) : (
                  s.title
                )}{' '}
                <span className="host">{hostOf(s.url)}</span>
              </div>
              <Chip>{s.kind.replace('_', ' ')}</Chip>
            </div>
          ))}
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- Sources
export function SourcesTab({ manifest }: { manifest: Manifest }) {
  const verifiedByMarker = new Map(manifest.citations.items.map((c) => [c.marker, c.verified]));
  if (!manifest.sources.length) return <EmptyState title="No sources yet">{manifest.status === 'running' ? 'Sources appear here as the researchers retrieve them.' : 'This run retrieved nothing.'}</EmptyState>;
  return (
    <div className="table-wrap">
      <table className="table" aria-label="Sources">
        <thead>
          <tr>
            <th>#</th>
            <th>Source</th>
            <th>Kind</th>
            <th>Tool</th>
            <th>Retrieved</th>
            <th>Cited by</th>
            <th>Verified</th>
          </tr>
        </thead>
        <tbody>
          {manifest.sources.map((s, i) => {
            const markers = s.cited_by ?? [];
            const allOk = markers.length > 0 && markers.every((m) => verifiedByMarker.get(m));
            return (
              <tr key={s.source_id}>
                <td className="num">{i + 1}</td>
                <td>
                  <div className="title ellipsis" style={{ maxWidth: 460 }}>
                    {s.url ? (
                      <a href={s.url} target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none', color: 'inherit' }}>
                        {s.title ?? s.url}
                      </a>
                    ) : (
                      s.title
                    )}
                  </div>
                  <div className="sub">
                    <span>{hostOf(s.url)}</span>
                    {s.content_sha256 ? <span className="mono">{shortSha(s.content_sha256)}</span> : null}
                  </div>
                </td>
                <td>{s.kind.replace('_', ' ')}</td>
                <td className="mono xs">{s.tool}</td>
                <td className="nowrap" title={s.retrieved_at}>
                  {fmtDate(s.retrieved_at)}
                </td>
                <td>{markers.length ? markers.map((m) => <span key={m} className="mark" style={{ marginRight: 4 }}>{m}</span>) : <span className="faint">—</span>}</td>
                <td>{markers.length ? allOk ? <Chip tone="ok">verified</Chip> : <Chip tone="err">unverified</Chip> : <Chip>uncited</Chip>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- Artifacts
export function ArtifactsTab({ manifest }: { manifest: Manifest }) {
  if (!manifest.artifacts.length) return <EmptyState title="No artifacts">Charts and tables produced in the sandbox are captured here as they are created, with MIME, size and sha256.</EmptyState>;
  return (
    <div className="art-grid">
      {manifest.artifacts.map((a) => (
        <ArtifactCard key={a.artifact_id} a={a} jobId={manifest.job_id} />
      ))}
    </div>
  );
}

function ArtifactCard({ a, jobId }: { a: Artifact; jobId: string }) {
  const api = useApi();
  const toast = useToast();
  const isImage = a.kind === 'image' && a.mime_type.startsWith('image/');
  const preview = useAsync(() => api.artifactUrl(jobId, a.artifact_id), [api, jobId, a.artifact_id], isImage && a.status === 'available');
  const [busy, setBusy] = useState(false);
  const download = async () => {
    setBusy(true);
    try {
      const r = await api.artifactUrl(jobId, a.artifact_id);
      if (r.url.startsWith('data:')) {
        const link = document.createElement('a');
        link.href = r.url;
        link.download = a.filename;
        link.click();
      } else window.open(r.url, '_blank', 'noopener');
    } catch (e) {
      toast.push('err', toApiError(e).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="art-card">
      <div className="preview">{isImage ? preview.data ? <img src={preview.data.url} alt={a.caption ?? a.title ?? a.filename} /> : preview.error ? <span>preview unavailable</span> : <Skeleton w="60%" h={12} /> : <span>{a.mime_type}</span>}</div>
      <div className="body">
        <div className="title">{a.title ?? a.filename}</div>
        {a.caption ? <div className="muted xs">{a.caption}</div> : null}
        <div className="sub">
          <span className="mono">{a.filename}</span>
          <span>{bytes(a.size_bytes)}</span>
          <span className="mono" title={a.sha256}>
            sha256 {shortSha(a.sha256)}
          </span>
          <Chip>{a.capture_phase}</Chip>
          {a.referenced_in_report ? <Chip tone="info">in report</Chip> : null}
          {a.status !== 'available' ? <Chip tone="warn">{a.status}</Chip> : null}
        </div>
        <div className="row" style={{ marginTop: 6 }}>
          <button type="button" className="btn btn-sm" onClick={download} disabled={busy || a.status !== 'available'}>
            {busy ? <span className="spin" aria-hidden="true" /> : '⤓'} Download
          </button>
          <span className="faint">{a.workflow ?? 'sandbox'} · {fmtDate(a.created_at)}</span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Run
export function RunTab({ manifest }: { manifest: Manifest }) {
  const m = manifest;
  return (
    <div className="grid grid-2">
      <div>
        <h3 style={{ margin: '0 0 8px' }}>Models per role</h3>
        <table className="kv" aria-label="Models per role">
          <tbody>
            {ROLES.map((r) => {
              const role = m.models.roles[r];
              return (
                <tr key={r}>
                  <td>{ROLE_LABEL[r]}</td>
                  <td>
                    <ModelName id={role?.model_id} name={role?.human_name} lane={role?.lane} />
                    <div className="faint">
                      {role?.region ?? '—'}
                      {role?.max_tokens ? ` · max ${num(role.max_tokens)} tok` : ''}
                      {role?.reasoning_effort ? ` · reasoning ${role.reasoning_effort}` : ''}
                    </div>
                  </td>
                </tr>
              );
            })}
            <tr>
              <td>Selection</td>
              <td>{m.models.selection.replace('_', ' ')}</td>
            </tr>
          </tbody>
        </table>
        <h3 style={{ margin: '18px 0 8px' }}>Usage</h3>
        <table className="kv" aria-label="Usage">
          <tbody>
            <tr>
              <td>Tokens</td>
              <td>
                {tokens(m.usage.input_tokens)} in · {tokens(m.usage.output_tokens)} out{m.usage.llm_calls ? ` · ${m.usage.llm_calls} calls` : ''}
              </td>
            </tr>
            <tr>
              <td>Searches / pages</td>
              <td>
                {m.usage.searches} / {m.usage.pages}
                {m.usage.retrievals ? ` · ${m.usage.retrievals} retrievals` : ''}
              </td>
            </tr>
            <tr>
              <td>Sandbox / browser</td>
              <td>
                {duration(m.usage.sandbox_seconds ?? 0)} · {m.usage.browser_sessions ?? 0} sessions
              </td>
            </tr>
            <tr>
              <td>Wall clock</td>
              <td>{duration(m.timing.duration_seconds)}</td>
            </tr>
            {m.usage.per_model?.length ? (
              <tr>
                <td>Per model</td>
                <td>
                  {m.usage.per_model.map((pm) => (
                    <div key={pm.model_id} className="row-nowrap xs" style={{ justifyContent: 'space-between' }}>
                      <ModelName id={pm.model_id} inline showId={false} />
                      <span className="faint nowrap">
                        {tokens(pm.input_tokens)} / {tokens(pm.output_tokens)} · {pm.calls}×
                      </span>
                    </div>
                  ))}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div>
        <h3 style={{ margin: '0 0 8px' }}>
          Cost <span className="faint">{usd(m.cost.total_usd, 4)} · {m.cost.confidence ?? 'computed'}</span>
        </h3>
        {m.cost.lines.length ? (
          <table className="kv" aria-label="Cost lines">
            <tbody>
              {m.cost.lines.map((l) => (
                <tr key={l.component}>
                  <td className="mono xs" style={{ width: '58%' }}>
                    {l.component}
                    <div className="faint">
                      {num(Math.round(l.quantity * 1000) / 1000)} {l.unit} × ${l.unit_price_usd}
                    </div>
                  </td>
                  <td className="right">{usd(l.usd, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="faint">No cost lines yet.</div>
        )}
        <div className="faint" style={{ marginTop: 6 }}>
          Prices: {m.cost.price_source.kind.replace(/_/g, ' ')} · retrieved {fmtDateTimeZ(m.cost.price_source.retrieved_at)}
          {m.cost.price_source.offer_codes?.length ? ` · ${m.cost.price_source.offer_codes.join(', ')}` : ''}
        </div>
        <h3 style={{ margin: '18px 0 8px' }}>Runtime</h3>
        <table className="kv" aria-label="Runtime">
          <tbody>
            <tr>
              <td>Adapter</td>
              <td>
                {m.runtime.adapter_version}
                {m.runtime.source_commit ? <span className="mono"> · {m.runtime.source_commit}</span> : null}
              </td>
            </tr>
            <tr>
              <td>Runtime version</td>
              <td>{m.runtime.runtime_version ?? '—'}</td>
            </tr>
            <tr>
              <td>Upstream AI-Q</td>
              <td className="mono xs">{m.runtime.upstream_ref ? shortSha(m.runtime.upstream_ref, 12) : '—'}</td>
            </tr>
            <tr>
              <td>Region</td>
              <td>{m.runtime.region ?? '—'}</td>
            </tr>
            <tr>
              <td>Session</td>
              <td className="mono xs" style={{ wordBreak: 'break-all' }}>
                {m.runtime.runtime_session_id ?? '—'}
              </td>
            </tr>
            <tr>
              <td>Lanes</td>
              <td>{[...new Set(ROLES.map((r) => laneLabel(m.models.roles[r]?.lane)))].join(' · ')}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Lineage
export function LineageTab({ manifest }: { manifest: Manifest }) {
  const api = useApi();
  const rootId = manifest.lineage.root_package_id;
  const family = useAsync(async () => {
    const page = await api.packagesList({ limit: 50 });
    const items = page.items.filter((p) => p.lineage.root_package_id === rootId || p.job_id === rootId);
    if (!items.some((p) => p.job_id === manifest.job_id)) {
      items.push({
        job_id: manifest.job_id, title: manifest.title, question: manifest.question, mode: manifest.mode, depth: manifest.depth, status: manifest.status, created_at: manifest.timing.created_at, completed_at: manifest.timing.completed_at ?? null,
        models: Object.fromEntries(ROLES.map((r) => [r, { model_id: manifest.models.roles[r].model_id, human_name: manifest.models.roles[r].human_name ?? null, lane: manifest.models.roles[r].lane }])) as PackageSummary['models'],
        cost_usd: manifest.cost.total_usd, counts: { sources: manifest.sources.length, citations_verified: manifest.citations.verified, citations_unverified: manifest.citations.unverified, artifacts: manifest.artifacts.length, exports: manifest.exports.length },
        tags: manifest.organization.tags, pinned: manifest.organization.pinned, lineage: { parent_package_id: manifest.lineage.parent_package_id ?? null, root_package_id: rootId, relation: manifest.lineage.relation },
      });
    }
    return items;
  }, [api, rootId, manifest.job_id]);

  if (family.error) return <ErrorBanner error={family.error} onRetry={family.reload} />;
  if (!family.data) return <TableSkeleton rows={3} />;
  const items = family.data;
  const children = (id: string) => items.filter((p) => p.lineage.parent_package_id === id && p.job_id !== id).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const root = items.find((p) => p.job_id === rootId);
  const render = (p: PackageSummary, depth: number): JSX.Element => (
    <div key={p.job_id} style={{ marginLeft: depth * 18 }}>
      <div className={`node ${p.job_id === manifest.job_id ? 'me' : ''}`}>
        <span aria-hidden="true">{depth === 0 ? '●' : '↳'}</span>
        {p.job_id === manifest.job_id ? <b>{shortId(p.job_id)}</b> : <Link to={`/p/${p.job_id}`}>{shortId(p.job_id)}</Link>}
        <Chip tone={p.lineage.relation === 'root' ? 'neutral' : 'info'}>{p.lineage.relation}</Chip>
        <StatusChip status={p.status} progress={p.progress} />
        <span className="muted ellipsis" style={{ maxWidth: 360 }}>
          {p.title ?? p.question}
        </span>
        <span className="faint nowrap">
          {p.models.writer.human_name ?? p.models.writer.model_id} · {usd(p.cost_usd)} · {ago(p.created_at)}
        </span>
        {p.job_id !== manifest.job_id && p.status === 'completed' && manifest.status === 'completed' ? (
          <Link to={`/compare/${manifest.job_id}/${p.job_id}`} className="btn btn-sm btn-ghost">
            ⇄ compare
          </Link>
        ) : null}
      </div>
      {children(p.job_id).map((c) => render(c, depth + 1))}
    </div>
  );
  return (
    <div className="lineage">
      {root ? render(root, 0) : <div className="faint">Root package {shortId(rootId)} is not in the library (deleted?). Showing this package only.</div>}
      {!root ? render(items.find((p) => p.job_id === manifest.job_id)!, 0) : null}
      {manifest.lineage.changes && (manifest.lineage.changes.models_changed?.length || manifest.lineage.changes.question_changed || manifest.lineage.changes.data_sources_changed) ? (
        <div className="faint" style={{ marginTop: 10 }}>
          Changes vs parent: {manifest.lineage.changes.models_changed?.length ? `models (${manifest.lineage.changes.models_changed.join(', ')})` : null}
          {manifest.lineage.changes.question_changed ? ' · question' : ''}
          {manifest.lineage.changes.data_sources_changed ? ' · data sources' : ''}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- Journal
/** Merge two event lists by seq (events without seq are appended once). */
export function mergeEvents(a: RuntimeEvent[], b: RuntimeEvent[]): RuntimeEvent[] {
  const seen = new Set(a.map((e) => e.seq).filter((s): s is number => s !== undefined));
  const out = [...a];
  for (const e of b) {
    if (e.seq === undefined) {
      if (!a.includes(e)) out.push(e);
    } else if (!seen.has(e.seq)) {
      seen.add(e.seq);
      out.push(e);
    }
  }
  return out.sort((x, y) => (x.seq ?? Number.MAX_SAFE_INTEGER) - (y.seq ?? Number.MAX_SAFE_INTEGER));
}

/** Rough progress for a live job from its journal (phase → band; sources → within band). */
export function progressFromEvents(events: RuntimeEvent[], status: string): { phase: string; pct: number } | null {
  if (isTerminal(status)) return null;
  const lastStatus = [...events].reverse().find((e) => e.type === 'status' || e.type === 'route');
  const phase = String((lastStatus?.data as { phase?: string } | undefined)?.phase ?? status);
  const src = events.filter((e) => e.type === 'source').length;
  let pct = Math.min(70, 8 + src * 2);
  if (events.some((e) => e.type === 'artifact')) pct = Math.max(pct, 78);
  if (phase === 'writing') pct = 90;
  if (events.some((e) => e.type === 'citations')) pct = 96;
  if (!events.length) pct = status === 'queued' ? 0 : 3;
  return { phase, pct };
}

export function JournalTab({ manifest, initial, onTerminal }: { manifest: Manifest; initial: RuntimeEvent[]; onTerminal: () => void }) {
  const api = useApi();
  const [events, setEvents] = useState<RuntimeEvent[]>(initial);
  const [error, setError] = useState<string | null>(null);
  const [pollTick, setPollTick] = useState(0);
  const live = !isTerminal(manifest.status);
  const lastSeq = events.reduce((m, e) => Math.max(m, e.seq ?? 0), 0);
  const sawTerminal = events.some((e) => e.type === 'completed' || e.type === 'failed' || e.type === 'cancelled' || (e.type === 'error' && e.seq !== undefined));

  // header refreshes deliver a (possibly short) tail: merge, never shrink
  useEffect(() => setEvents((prev) => mergeEvents(prev, initial)), [initial]);

  useEffect(() => {
    if (!live || sawTerminal) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const more = await api.events(manifest.job_id, lastSeq);
        if (cancelled) return;
        if (more.length) setEvents((xs) => mergeEvents(xs, more));
        setError(null);
        if (more.some((e) => e.type === 'completed' || e.type === 'failed' || e.type === 'cancelled')) onTerminal();
      } catch (e) {
        if (!cancelled) setError(toApiError(e).message);
      } finally {
        if (!cancelled) setPollTick((t) => t + 1); // schedule the next poll even when nothing new arrived
      }
    };
    const t = window.setTimeout(poll, pollTick === 0 && !events.length ? 0 : 3000);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, manifest.job_id, live, sawTerminal, onTerminal, pollTick]);

  const lastStatus = [...events].reverse().find((e) => e.type === 'status' || e.type === 'route');
  const pct = progressFromEvents(events, manifest.status)?.pct ?? 100;

  return (
    <div className="stack">
      {live ? (
        <div className="glass card" style={{ padding: 14 }}>
          <div className="row between">
            <div className="row">
              <StatusChip status={manifest.status} />
              <span className="small">{String((lastStatus?.data as { text?: string } | undefined)?.text ?? 'Waiting for the first event…')}</span>
            </div>
            <span className="faint">polling every 3 s · seq {lastSeq}</span>
          </div>
          <div className="progress" style={{ marginTop: 10 }} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <i style={{ width: `${pct}%` }} />
          </div>
        </div>
      ) : null}
      {error ? <ErrorBanner error={error} title="Journal poll failed (will retry)" /> : null}
      {events.length === 0 ? (
        <EmptyState title="No journal events">{live ? 'The runtime has not emitted anything yet.' : 'The journal for this job has expired (30-day TTL) or was not recorded; the package itself is durable.'}</EmptyState>
      ) : (
        <div className="journal" role="log" aria-live={live ? 'polite' : 'off'}>
          {events.map((e, i) => {
            const d = (e.data ?? {}) as Record<string, unknown>;
            const text = String(d.text ?? d.title ?? d.message ?? (e.type === 'usage' ? `${tokens(d.input_tokens as number)} in · ${tokens(d.output_tokens as number)} out` : e.type === 'citations' ? `${d.verified} verified · ${d.unverified} unverified` : e.type === 'error' ? String((d.error as { message?: string } | undefined)?.message ?? JSON.stringify(d)) : ''));
            const tone = e.type === 'error' || e.type === 'failed' ? 'err' : e.type === 'completed' ? 'ok' : '';
            return (
              <div className={`ev ${tone}`} key={`${e.seq ?? i}-${e.type}`}>
                <span className="t">{e.seq ?? '·'}</span>
                <span className="ty">{e.type}</span>
                <span>
                  {text}
                  {e.type === 'source' && d.url ? (
                    <>
                      {' '}
                      <a href={String(d.url)} target="_blank" rel="noopener noreferrer" className="faint">
                        {hostOf(String(d.url))}
                      </a>
                    </>
                  ) : null}
                  {d.at ? <span className="faint"> · {fmtDate(String(d.at))}</span> : null}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

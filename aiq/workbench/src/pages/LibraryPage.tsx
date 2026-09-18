// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Library: KPI tiles, search + filters, cursor-paginated table, pin toggle, tag chips, Open / Re-run / Answer in chat.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useApi } from '../api';
import { ApiError, toApiError } from '../api/client';
import { Chip, StatusChip } from '../components/Chip';
import { KpiTile } from '../components/Kpi';
import { ModelName } from '../components/ModelName';
import { EmptyState, ErrorBanner, TableSkeleton } from '../components/States';
import { useToast } from '../components/Toast';
import { useConfig } from '../config';
import { ago, num, pct, shortId, usd, withinDays } from '../lib/format';
import { owuiHomeUrl, owuiNewResearchUrl } from '../lib/owui';
import { useSession } from '../session';
import type { JobStatus, ListParams, Mode, PackageSummary } from '../types';
import { MODE_LABEL } from '../types';
import { RerunDialog } from './package/RerunDialog';

const PAGE = 8;
const STATUSES: JobStatus[] = ['running', 'queued', 'clarifying', 'completed', 'failed', 'cancelled'];
const MODES: Mode[] = ['auto', 'shallow', 'deep', 'deep_clarify'];
const DATE_WINDOWS = [
  { id: '', label: 'Any date' },
  { id: '7', label: 'Last 7 days' },
  { id: '30', label: 'Last 30 days' },
  { id: '90', label: 'Last 90 days' },
];

export default function LibraryPage() {
  const api = useApi();
  const cfg = useConfig();
  const session = useSession();
  const toast = useToast();
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const q = sp.get('q') ?? '';
  const status = (sp.get('status') ?? '') as JobStatus | '';
  const mode = (sp.get('mode') ?? '') as Mode | '';
  const tag = sp.get('tag') ?? '';
  const pinned = sp.get('pinned') === '1';
  const days = sp.get('days') ?? '';
  const [draftQ, setDraftQ] = useState(q);

  const [items, setItems] = useState<PackageSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [tick, setTick] = useState(0);
  const [rerunOf, setRerunOf] = useState<PackageSummary | null>(null);

  const params = useMemo<ListParams>(() => ({ limit: PAGE, ...(q ? { q } : {}), ...(status ? { status } : {}), ...(mode ? { mode } : {}), ...(tag ? { tag } : {}), ...(pinned ? { pinned: true } : {}) }), [q, status, mode, tag, pinned]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .packagesList(params)
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setCursor(page.next_cursor);
      })
      .catch((e: unknown) => !cancelled && setError(toApiError(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [api, params, tick]);

  // live refresh while anything is running/queued (cheap: first page only)
  const anyLive = items.some((p) => p.status === 'running' || p.status === 'queued' || p.status === 'cancelling');
  useEffect(() => {
    if (!anyLive) return;
    const t = window.setInterval(() => {
      api
        .packagesList(params)
        .then((page) => setItems((prev) => (prev.length > page.items.length ? [...page.items, ...prev.slice(page.items.length)] : page.items)))
        .catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(t);
  }, [anyLive, api, params]);

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await api.packagesList({ ...params, cursor });
      setItems((xs) => [...xs, ...page.items]);
      setCursor(page.next_cursor);
    } catch (e) {
      toast.push('err', toApiError(e).message);
    } finally {
      setLoadingMore(false);
    }
  };

  const setParam = useCallback(
    (k: string, v: string) => {
      const next = new URLSearchParams(sp);
      if (v) next.set(k, v);
      else next.delete(k);
      setSp(next, { replace: true });
    },
    [sp, setSp],
  );

  const visible = useMemo(() => (days ? items.filter((p) => withinDays(p.created_at, Number(days))) : items), [items, days]);
  const allTags = useMemo(() => [...new Set(items.flatMap((p) => p.tags))].sort(), [items]);

  const kpi = useMemo(() => {
    const running = items.filter((p) => p.status === 'running' || p.status === 'queued');
    const spend30 = items.filter((p) => withinDays(p.created_at, 30)).reduce((s, p) => s + (p.cost_usd || 0), 0);
    const cv = items.reduce((s, p) => s + (p.counts?.citations_verified ?? 0), 0);
    const cu = items.reduce((s, p) => s + (p.counts?.citations_unverified ?? 0), 0);
    const avgPct = running.length ? running.reduce((s, p) => s + (p.progress?.pct ?? 0), 0) / running.length : 0;
    return { running, spend30, cv, cu, avgPct };
  }, [items]);

  const togglePin = async (p: PackageSummary) => {
    const next = !p.pinned;
    setItems((xs) => xs.map((x) => (x.job_id === p.job_id ? { ...x, pinned: next } : x)));
    try {
      await api.packagesUpdate(p.job_id, { pinned: next });
    } catch (e) {
      setItems((xs) => xs.map((x) => (x.job_id === p.job_id ? { ...x, pinned: p.pinned } : x)));
      toast.push('err', toApiError(e).message);
    }
  };

  const filtersActive = !!(q || status || mode || tag || pinned || days);

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Research Library</h1>
          <div>Every job becomes a durable package: report, sources, artifacts, run metadata, lineage.</div>
        </div>
        <div className="actions">
          <a className="btn btn-primary" href={owuiHomeUrl(cfg)} target="_blank" rel="noopener noreferrer">
            ＋ New research in chat
          </a>
        </div>
      </div>

      <div className="grid grid-4 kpi-grid">
        <KpiTile loading={loading && !items.length} value={num(items.length) + (cursor ? '+' : '')} label="packages" bar={Math.min(100, items.length * 2)} />
        <KpiTile
          loading={loading && !items.length}
          value={
            <>
              {kpi.running.length} <span className="sub" style={{ color: 'var(--warn)' }}>{kpi.running.length === 1 ? 'running' : 'running'}</span>
            </>
          }
          label="active jobs"
          progress={kpi.running.length ? kpi.avgPct : undefined}
          hint={kpi.running.length ? `${Math.round(kpi.avgPct)}% average progress` : 'nothing running'}
        />
        <KpiTile loading={loading && !items.length} value={usd(kpi.spend30)} label="spend · 30 days" hint="from AWS Price List rates" />
        <KpiTile loading={loading && !items.length} value={kpi.cv + kpi.cu ? pct(kpi.cv, kpi.cv + kpi.cu) : '—'} label="citations verified" hint={`${num(kpi.cv)} of ${num(kpi.cv + kpi.cu)} markers`} />
      </div>

      <form
        className="glass filters"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          setParam('q', draftQ.trim());
        }}
      >
        <input className="input" aria-label="Search packages" placeholder="Search titles, questions, tags…" value={draftQ} onChange={(e) => setDraftQ(e.target.value)} onBlur={() => draftQ.trim() !== q && setParam('q', draftQ.trim())} />
        <select className="select" aria-label="Status" value={status} onChange={(e) => setParam('status', e.target.value)}>
          <option value="">Status: any</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select className="select" aria-label="Mode" value={mode} onChange={(e) => setParam('mode', e.target.value)}>
          <option value="">Mode: any</option>
          {MODES.map((m) => (
            <option key={m} value={m}>
              {MODE_LABEL[m]}
            </option>
          ))}
        </select>
        <select className="select" aria-label="Tag" value={tag} onChange={(e) => setParam('tag', e.target.value)}>
          <option value="">Tag: any</option>
          {tag && !allTags.includes(tag) ? <option value={tag}>#{tag}</option> : null}
          {allTags.map((t) => (
            <option key={t} value={t}>
              #{t}
            </option>
          ))}
        </select>
        <select className="select" aria-label="Date range" value={days} onChange={(e) => setParam('days', e.target.value)}>
          {DATE_WINDOWS.map((w) => (
            <option key={w.id} value={w.id}>
              {w.label}
            </option>
          ))}
        </select>
        <Chip tone={pinned ? 'info' : 'neutral'} onClick={() => setParam('pinned', pinned ? '' : '1')} pressed={pinned} title="Only pinned packages">
          ⚑ pinned
        </Chip>
        {filtersActive ? (
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={() => {
              setDraftQ('');
              setSp(new URLSearchParams(), { replace: true });
            }}
          >
            Clear
          </button>
        ) : null}
      </form>

      {error ? (
        <ErrorBanner error={error} title="Could not load your library" onRetry={() => setTick((t) => t + 1)} />
      ) : (
        <div className="glass table-wrap">
          {loading && !items.length ? (
            <TableSkeleton rows={7} />
          ) : visible.length === 0 ? (
            <EmptyState
              title={filtersActive ? 'No packages match these filters' : 'Your library is empty'}
              action={
                filtersActive ? (
                  <button type="button" className="btn" onClick={() => setSp(new URLSearchParams(), { replace: true })}>
                    Clear filters
                  </button>
                ) : (
                  <a className="btn btn-primary" href={owuiHomeUrl(cfg)} target="_blank" rel="noopener noreferrer">
                    Start research in chat
                  </a>
                )
              }
            >
              {filtersActive ? 'Try a broader search or another status.' : 'Ask a question in Open WebUI; the package appears here the moment the job is accepted.'}
            </EmptyState>
          ) : (
            <table className="table" aria-label="Packages">
              <thead>
                <tr>
                  <th>Package</th>
                  <th>Status</th>
                  <th>Mode</th>
                  <th className="num">Sources</th>
                  <th>Writer</th>
                  <th className="num">Cost</th>
                  <th>Updated</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((p) => (
                  <Row key={p.job_id} p={p} onPin={() => togglePin(p)} onTag={(t) => setParam('tag', t)} onRerun={() => setRerunOf(p)} owuiUrl={p.status === 'clarifying' ? owuiNewResearchUrl(cfg, p.question) : null} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <div className="pager">
        <span>
          Showing {visible.length}
          {days && visible.length !== items.length ? ` of ${items.length} loaded` : ''} · newest first{session.mock ? ' · fixtures' : ''}
        </span>
        {cursor ? (
          <button type="button" className="btn btn-sm" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? <span className="spin" aria-hidden="true" /> : null} Load more
          </button>
        ) : items.length ? (
          <span>End of library</span>
        ) : null}
      </div>

      {rerunOf ? (
        <RerunDialog
          open
          jobId={rerunOf.job_id}
          title={rerunOf.title ?? rerunOf.question}
          current={rerunOf.models}
          onClose={() => setRerunOf(null)}
          onAccepted={(id) => {
            setRerunOf(null);
            navigate(`/p/${id}`);
          }}
        />
      ) : null}
    </>
  );
}

function Row({ p, onPin, onTag, onRerun, owuiUrl }: { p: PackageSummary; onPin: () => void; onTag: (t: string) => void; onRerun: () => void; owuiUrl: string | null }) {
  const navigate = useNavigate();
  const dim = p.status === 'failed' || p.status === 'cancelled';
  return (
    <tr className={dim ? 'dim' : ''}>
      <td>
        <div className="title">
          <Link to={`/p/${p.job_id}`} style={{ color: 'inherit', textDecoration: 'none' }}>
            {p.title ?? p.question}
          </Link>
        </div>
        <div className="sub">
          <button type="button" className="pin" aria-pressed={p.pinned} aria-label={p.pinned ? 'Unpin' : 'Pin'} onClick={onPin} title={p.pinned ? 'Pinned' : 'Pin'}>
            ⚑
          </button>
          <span className="mono">{shortId(p.job_id)}</span>
          {p.tags.map((t) => (
            <Chip key={t} onClick={() => onTag(t)} title={`Filter by #${t}`}>
              #{t}
            </Chip>
          ))}
          {p.lineage.relation !== 'root' && p.lineage.parent_package_id ? (
            <span>
              ↳ {p.lineage.relation} of{' '}
              <Link to={`/p/${p.lineage.parent_package_id}`} className="mono">
                {shortId(p.lineage.parent_package_id)}
              </Link>
            </span>
          ) : null}
          {p.counts.artifacts ? <span>· {p.counts.artifacts} chart{p.counts.artifacts === 1 ? '' : 's'}</span> : null}
          {p.counts.exports ? <span>· {p.counts.exports} export{p.counts.exports === 1 ? '' : 's'}</span> : null}
          {p.status === 'failed' && p.error ? <span style={{ color: 'var(--err)' }}>· {p.error.slice(0, 60)}</span> : null}
          {p.status === 'clarifying' ? <span>· clarification pending</span> : null}
        </div>
      </td>
      <td>
        <StatusChip status={p.status} progress={p.progress} />
      </td>
      <td>{MODE_LABEL[p.mode] ?? p.mode}</td>
      <td className="num">{(p.counts?.sources ?? 0)}</td>
      <td>
        <ModelName id={p.models?.writer?.model_id} name={p.models?.writer?.human_name} showId={false} />
      </td>
      <td className="num">{usd(p.cost_usd)}</td>
      <td className="nowrap" title={p.created_at}>
        {ago(p.completed_at ?? p.created_at)}
      </td>
      <td className="actions">
        {p.status === 'clarifying' && owuiUrl ? (
          <a className="btn btn-ghost btn-sm" href={owuiUrl} target="_blank" rel="noopener noreferrer">
            Answer in chat
          </a>
        ) : null}
        {p.status === 'failed' || p.status === 'cancelled' || p.status === 'completed' ? (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onRerun}>
            ↻ Re-run
          </button>
        ) : null}
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate(`/p/${p.job_id}`)}>
          Open
        </button>
      </td>
    </tr>
  );
}

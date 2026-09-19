// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Package viewer: header (chips, editable tags, pin), Export ▾ / Re-run with… / Compare / Continue in chat, six tabs.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useApi } from '../api';
import { toApiError } from '../api/client';
import { Chip, StatusChip } from '../components/Chip';
import { Dialog } from '../components/Dialog';
import { Menu } from '../components/Menu';
import { ErrorBanner, InfoBanner, Skeleton } from '../components/States';
import { Tabs } from '../components/Tabs';
import { TagEditor } from '../components/TagEditor';
import { useToast } from '../components/Toast';
import { useConfig } from '../config';
import { duration, pct, shortId, usd } from '../lib/format';
import { owuiChatUrl, owuiNewResearchUrl } from '../lib/owui';
import { useAsync } from '../lib/useAsync';
import type { ExportFormat, ExportResult, PackageResponse } from '../types';
import { MODE_LABEL, isTerminal } from '../types';
import { CompareDialog } from './package/CompareDialog';
import { RerunDialog } from './package/RerunDialog';
import { ArtifactsTab, JournalTab, LineageTab, ReportTab, RunTab, SourcesTab, progressFromEvents } from './package/tabs';

type Tab = 'report' | 'sources' | 'artifacts' | 'run' | 'lineage' | 'journal';
export const EXPORT_FORMATS: { id: ExportFormat; label: string; hint: string; derived?: boolean }[] = [
  { id: 'md', label: 'Markdown', hint: 'report.md + front-matter' },
  { id: 'html', label: 'Styled HTML', hint: 'dark + light + print' },
  { id: 'pdf', label: 'PDF', hint: 'A4, citations preserved' },
  { id: 'docx', label: 'Word (DOCX)', hint: 'headings, tables, figures' },
  { id: 'pptx', label: 'Slides (PPTX)', hint: 'derived', derived: true },
  { id: 'json', label: 'Package JSON', hint: 'manifest + report + ledger' },
  { id: 'csv', label: 'Sources CSV', hint: 'one row per source' },
  { id: 'bibtex', label: 'BibTeX', hint: '@online entries' },
  { id: 'ris', label: 'RIS', hint: 'reference manager' },
  { id: 'csl', label: 'CSL-JSON', hint: 'citation styles' },
  { id: 'zip', label: 'Everything (ZIP)', hint: 'all formats + artifacts/' },
];

export function openExport(res: ExportResult, toast: ReturnType<typeof useToast>) {
  if (!res.url) {
    toast.push('err', `Export ${res.filename} was rendered but no download link came back.`);
    return;
  }
  const w = window.open(res.url, '_blank', 'noopener');
  if (!w) toast.push('warn', `Pop-up blocked. Download link (valid ${Math.round(res.expires_in / 60)} min): ${res.url}`);
  else toast.push('ok', `${res.filename} · ${Math.round(res.size / 1024)} KB${res.cached ? ' · cached' : ''} · link valid ${Math.round(res.expires_in / 60)} min`);
}

export default function PackagePage() {
  const { jobId = '' } = useParams();
  const api = useApi();
  const cfg = useConfig();
  const toast = useToast();
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const tab = (sp.get('tab') as Tab) || 'report';
  const setTab = (t: Tab) => {
    const next = new URLSearchParams(sp);
    if (t === 'report') next.delete('tab');
    else next.set('tab', t);
    setSp(next, { replace: true });
  };
  const pkg = useAsync(() => api.packagesGet(jobId), [api, jobId]);
  const [rerun, setRerun] = useState(false);
  const [compare, setCompare] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  const [saving, setSaving] = useState(false);

  const manifest = pkg.data?.manifest;
  const live = manifest ? !isTerminal(manifest.status) : false;

  // refresh the header while the job is live (the Journal tab polls events; the header polls the package)
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => {
      api
        .packagesGet(jobId)
        .then((r) => pkg.setData(r))
        .catch(() => undefined);
    }, 6000);
    return () => window.clearInterval(t);
  }, [live, api, jobId, pkg]);

  const onTerminal = useCallback(() => pkg.reload(), [pkg]);

  const patch = async (p: Parameters<typeof api.packagesUpdate>[1]) => {
    if (!manifest) return;
    setSaving(true);
    try {
      const r: PackageResponse = await api.packagesUpdate(jobId, p);
      pkg.setData((prev) => (prev ? { ...prev, manifest: r.manifest } : r));
    } catch (e) {
      toast.push('err', toApiError(e).message);
    } finally {
      setSaving(false);
    }
  };

  const doExport = async (format: ExportFormat) => {
    setExporting(format);
    try {
      const res = await api.exportPackage(jobId, format);
      openExport(res, toast);
    } catch (e) {
      toast.push('err', `Export ${format.toUpperCase()} failed: ${toApiError(e).message}`, 8000);
    } finally {
      setExporting(null);
    }
  };

  const doDelete = async () => {
    try {
      await api.packagesDelete(jobId);
      toast.push('ok', `Deleted ${shortId(jobId)}`);
      navigate('/');
    } catch (e) {
      toast.push('err', toApiError(e).message);
      setConfirmDelete(false);
    }
  };

  const continueUrl = useMemo(() => {
    if (!manifest) return '#';
    return manifest.runtime.conversation_id ? owuiChatUrl(cfg, manifest.runtime.conversation_id) : owuiNewResearchUrl(cfg, manifest.question);
  }, [manifest, cfg]);

  if (pkg.error) {
    return (
      <>
        <div className="crumbs">
          <Link to="/">Library</Link> / <span className="mono">{shortId(jobId)}</span>
        </div>
        <ErrorBanner error={pkg.error} title={pkg.error.code === 'not_found' ? 'Package not found' : 'Could not load the package'} onRetry={pkg.reload} />
        <p className="muted small">
          <Link to="/">Back to the library</Link>
        </p>
      </>
    );
  }
  if (!manifest) {
    return (
      <>
        <div className="crumbs">
          <Link to="/">Library</Link> / <span className="mono">{shortId(jobId)}</span>
        </div>
        <div className="stack">
          <Skeleton h={34} w="60%" />
          <Skeleton h={22} w="80%" />
          <div className="glass card">
            <Skeleton h={18} w="40%" />
            <div style={{ height: 12 }} />
            <Skeleton h={14} />
            <div style={{ height: 8 }} />
            <Skeleton h={14} w="90%" />
          </div>
        </div>
      </>
    );
  }

  const m = manifest;
  const verifiedPct = pct(m.citations.verified, m.citations.verified + m.citations.unverified);
  const summaryProgress = progressFromEvents(pkg.data?.events_tail ?? [], m.status);
  const tabs = [
    { id: 'report' as Tab, label: 'Report' },
    { id: 'sources' as Tab, label: 'Sources', count: m.sources.length },
    { id: 'artifacts' as Tab, label: 'Artifacts', count: m.artifacts.length },
    { id: 'run' as Tab, label: 'Run' },
    { id: 'lineage' as Tab, label: 'Lineage' },
    { id: 'journal' as Tab, label: 'Journal', count: live ? 'live' : undefined },
  ];

  return (
    <>
      <div className="crumbs">
        <Link to="/">Library</Link> / <span className="mono">{shortId(m.job_id)}</span>
        {m.lineage.relation !== 'root' && m.lineage.parent_package_id ? (
          <>
            <span>·</span>
            <span>
              {m.lineage.relation} of <Link to={`/p/${m.lineage.parent_package_id}`}>{shortId(m.lineage.parent_package_id)}</Link>
            </span>
          </>
        ) : null}
      </div>
      <div className="topbar">
        <div className="grow">
          <h1>{m.title ?? m.question}</h1>
          <div className="row" style={{ marginTop: 8 }}>
            <StatusChip status={m.status} progress={summaryProgress ?? null} />
            <Chip>{MODE_LABEL[m.mode] ?? m.mode}{m.depth && m.depth !== m.mode ? ` → ${m.depth}` : ''}</Chip>
            <Chip title="sources · citations · verified">
              {m.sources.length} sources · {m.citations.verified + m.citations.unverified} citations · {verifiedPct} verified
            </Chip>
            {m.artifacts.length ? <Chip>{m.artifacts.length} chart{m.artifacts.length === 1 ? '' : 's'}</Chip> : null}
            <Chip title="cost · wall clock">
              {usd(m.cost.total_usd)} · {duration(m.timing.duration_seconds)}
            </Chip>
            <button type="button" className="pin" aria-pressed={m.organization.pinned} aria-label={m.organization.pinned ? 'Unpin package' : 'Pin package'} onClick={() => patch({ pinned: !m.organization.pinned })} disabled={saving}>
              ⚑
            </button>
            <TagEditor tags={m.organization.tags} onChange={(tags) => patch({ tags })} onTagClick={(t) => navigate(`/?tag=${encodeURIComponent(t)}`)} busy={saving} />
          </div>
        </div>
        <div className="actions">
          <Menu
            label="⤓ Export"
            busy={!!exporting}
            items={[
              ...EXPORT_FORMATS.map((f) => ({ id: f.id, label: f.label, hint: f.derived ? 'derived' : f.hint, disabled: !m.report.present, onSelect: () => doExport(f.id) })),
              { id: 'center', label: 'Export center…', hint: 'themes, connected apps', separatorBefore: true, onSelect: () => navigate(`/exports/${m.job_id}`) },
            ]}
          />
          <button type="button" className="btn" onClick={() => setRerun(true)}>
            ↻ Re-run with…
          </button>
          <button type="button" className="btn" onClick={() => setCompare(true)} disabled={m.status !== 'completed'} title={m.status !== 'completed' ? 'Compare needs a completed package' : undefined}>
            ⇄ Compare
          </button>
          <a className="btn btn-primary" href={continueUrl} target="_blank" rel="noopener noreferrer">
            ✎ Continue in chat
          </a>
          <Menu label="More" items={[{ id: 'delete', label: <span style={{ color: 'var(--err)' }}>Delete package…</span>, onSelect: () => setConfirmDelete(true) }]} />
        </div>
      </div>

      {m.status === 'failed' && m.error ? <div style={{ marginBottom: 16 }}><ErrorBanner error={m.error} title="This run failed" /></div> : null}
      {m.status === 'clarifying' ? (
        <div style={{ marginBottom: 16 }}>
          <InfoBanner>
            The clarifier asked: <em>{m.clarification?.[0]?.q ?? 'a clarifying question'}</em>{' '}
            <a href={continueUrl} target="_blank" rel="noopener noreferrer">
              Answer in chat ↗
            </a>
          </InfoBanner>
        </div>
      ) : null}

      <div className="two-col">
        <section className="glass card">
          <Tabs tabs={tabs} value={tab} onChange={setTab} label="Package sections" />
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === 'report' ? <ReportTab manifest={m} reportMd={pkg.data?.report_md ?? ''} /> : null}
            {tab === 'sources' ? <SourcesTab manifest={m} /> : null}
            {tab === 'artifacts' ? <ArtifactsTab manifest={m} /> : null}
            {tab === 'run' ? <RunTab manifest={m} /> : null}
            {tab === 'lineage' ? <LineageTab manifest={m} /> : null}
            {tab === 'journal' ? <JournalTab manifest={m} initial={pkg.data?.events_tail ?? []} onTerminal={onTerminal} /> : null}
          </div>
        </section>
        <aside className="aside">
          <div className="glass card">
            <div className="h">Run</div>
            <table className="kv" aria-label="Run summary">
              <tbody>
                <tr>
                  <td>Router</td>
                  <td className="model-name">{m.models.roles.router.human_name ?? m.models.roles.router.model_id}</td>
                </tr>
                <tr>
                  <td>Planner</td>
                  <td className="model-name">{m.models.roles.planner.human_name ?? m.models.roles.planner.model_id}</td>
                </tr>
                <tr>
                  <td>Researchers</td>
                  <td className="model-name">{m.models.roles.researcher.human_name ?? m.models.roles.researcher.model_id}</td>
                </tr>
                <tr>
                  <td>Writer</td>
                  <td className="model-name">{m.models.roles.writer.human_name ?? m.models.roles.writer.model_id}</td>
                </tr>
                <tr>
                  <td>Tokens</td>
                  <td>
                    {m.usage.input_tokens.toLocaleString()} in · {m.usage.output_tokens.toLocaleString()} out
                  </td>
                </tr>
                <tr>
                  <td>Searches / pages</td>
                  <td>
                    {m.usage.searches} / {m.usage.pages}
                  </td>
                </tr>
                <tr>
                  <td>Cost</td>
                  <td>
                    <b>{usd(m.cost.total_usd)}</b> <span className="faint">{m.cost.price_source.kind === 'aws_price_list_api' ? 'Price List' : m.cost.price_source.kind} {m.cost.price_source.retrieved_at.slice(0, 10)}</span>
                  </td>
                </tr>
                <tr>
                  <td>Runtime</td>
                  <td>
                    {m.runtime.runtime_version ?? m.runtime.adapter_version} <span className="mono faint">{m.runtime.source_commit ?? ''}</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <button type="button" className="btn btn-sm btn-ghost" style={{ marginTop: 8 }} onClick={() => setTab('run')}>
              Full run details →
            </button>
          </div>
          <div className="glass card">
            <div className="h">
              Exports <Link to={`/exports/${m.job_id}`} className="xs">Export center →</Link>
            </div>
            <div className="row">
              {EXPORT_FORMATS.filter((f) => f.id !== 'ris' && f.id !== 'csl').map((f) => {
                const done = m.exports.find((e) => e.format === f.id || (f.id === 'bibtex' && e.format === 'bibtex'));
                return (
                  <Chip key={f.id} tone={done ? 'ok' : 'neutral'} onClick={() => doExport(f.id)} title={done ? `exported ${done.created_at.slice(0, 10)} · click to download again` : `Export ${f.label}`}>
                    {f.id.toUpperCase()}
                    {done ? ' ✓' : ''}
                  </Chip>
                );
              })}
            </div>
          </div>
          {m.organization.notes ? (
            <div className="glass card">
              <div className="h">Notes</div>
              <div className="small muted">{m.organization.notes}</div>
            </div>
          ) : null}
        </aside>
      </div>

      {rerun ? (
        <RerunDialog
          open
          jobId={m.job_id}
          title={m.title ?? m.question}
          current={m.models.roles}
          onClose={() => setRerun(false)}
          onAccepted={(id) => {
            setRerun(false);
            toast.push('ok', `Re-run accepted: ${shortId(id)}`);
            navigate(`/p/${id}`);
          }}
        />
      ) : null}
      {compare ? <CompareDialog open jobId={m.job_id} rootId={m.lineage.root_package_id} onClose={() => setCompare(false)} /> : null}
      <Dialog
        open={confirmDelete}
        title="Delete this package?"
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirmDelete(false)}>
              Keep
            </button>
            <button type="button" className="btn btn-danger" onClick={doDelete}>
              Delete {shortId(m.job_id)}
            </button>
          </>
        }
      >
        <p className="small" style={{ margin: 0 }}>
          The report, sources, artifacts and exports of <b>{m.title ?? shortId(m.job_id)}</b> are removed from your library and storage. A tombstone keeps the id from being reused for 90 days. Re-runs and follow-ups keep their own packages.
        </p>
      </Dialog>
    </>
  );
}

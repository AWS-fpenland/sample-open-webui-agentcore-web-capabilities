// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Export center: every format as a card with Download, theme toggle for HTML/PDF, ZIP primary, connected apps.
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApi } from '../api';
import { toApiError } from '../api/client';
import { Chip } from '../components/Chip';
import { ErrorBanner, Skeleton } from '../components/States';
import { useToast } from '../components/Toast';
import { useConfig } from '../config';
import { bytes, shortId, shortSha } from '../lib/format';
import { useAsync } from '../lib/useAsync';
import type { ExportFormat, ExportResult, ExportTheme } from '../types';
import { openExport } from './PackagePage';

interface Card {
  id: ExportFormat;
  ic: string;
  title: string;
  meta: (n: { sources: number; artifacts: number; words: number | null }) => string;
  derived?: boolean;
  themed?: boolean;
  primary?: boolean;
  siblings?: ExportFormat[];
}
const CARDS: Card[] = [
  { id: 'md', ic: 'MD', title: 'Markdown', meta: (n) => `report.md · front-matter · ${n.words ? `${n.words.toLocaleString()} words` : '—'}` },
  { id: 'html', ic: 'HTML', title: 'Styled HTML', meta: () => 'dark + light + print · self-contained', themed: true },
  { id: 'pdf', ic: 'PDF', title: 'PDF', meta: () => 'A4 · citations + Sources preserved · browser print', themed: true },
  { id: 'docx', ic: 'DOCX', title: 'Word', meta: (n) => `headings · tables · ${n.artifacts} figure${n.artifacts === 1 ? '' : 's'} · endnotes` },
  { id: 'pptx', ic: 'PPTX', title: 'Slides', meta: () => 'key findings · one slide per section · chart · sources', derived: true },
  { id: 'json', ic: 'JSON', title: 'Package JSON', meta: () => 'manifest · report · ledger · sources' },
  { id: 'csv', ic: 'CSV', title: 'Sources CSV', meta: (n) => `${n.sources} rows · url, title, retrieved, cited by` },
  { id: 'bibtex', ic: 'BIB', title: 'BibTeX · RIS · CSL-JSON', meta: () => '@online entries · reference managers · citation styles', siblings: ['ris', 'csl'] },
  { id: 'zip', ic: 'ZIP', title: 'Everything', meta: () => 'all formats + manifest.json + artifacts/', primary: true },
];

export default function ExportsPage() {
  const { jobId = '' } = useParams();
  const api = useApi();
  const cfg = useConfig();
  const toast = useToast();
  const pkg = useAsync(() => api.packagesGet(jobId), [api, jobId]);
  const [theme, setTheme] = useState<ExportTheme>('dark');
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [results, setResults] = useState<Partial<Record<ExportFormat, ExportResult>>>({});

  const run = async (format: ExportFormat, themed?: boolean) => {
    setBusy(format);
    try {
      const r = await api.exportPackage(jobId, format, themed ? theme : undefined);
      setResults((xs) => ({ ...xs, [format]: r }));
      openExport(r, toast);
    } catch (e) {
      toast.push('err', `Export ${format.toUpperCase()} failed: ${toApiError(e).message}`, 8000);
    } finally {
      setBusy(null);
    }
  };

  const m = pkg.data?.manifest;
  const n = { sources: m?.sources.length ?? 0, artifacts: m?.artifacts.length ?? 0, words: m?.report.word_count ?? null };
  const noReport = m ? !m.report.present : false;
  const connected = cfg.connectedApps === true;

  return (
    <>
      <div className="crumbs">
        <Link to="/">Library</Link> / <Link to={`/p/${jobId}`}>{shortId(jobId)}</Link> / exports
      </div>
      <div className="topbar">
        <div className="grow">
          <h1>Export center</h1>
          <div>
            {m ? (
              <>
                <span className="mono">{shortId(m.job_id)}</span> · {m.title ?? m.question} · every file is rendered from the verified package and checked before download.
              </>
            ) : pkg.error ? null : (
              <Skeleton w="60%" h={16} />
            )}
          </div>
        </div>
        <div className="actions">
          <div className="toggle" role="group" aria-label="Theme for HTML and PDF">
            {(['dark', 'light', 'print'] as ExportTheme[]).map((t) => (
              <button key={t} type="button" aria-pressed={theme === t} onClick={() => setTheme(t)}>
                {t}
              </button>
            ))}
          </div>
        </div>
      </div>

      {pkg.error ? <ErrorBanner error={pkg.error} title="Could not load the package" onRetry={pkg.reload} /> : null}
      {noReport ? <div style={{ marginBottom: 16 }}><ErrorBanner error={`This package has no report (status: ${m?.status}); exports are available once the writer finishes.`} title="Nothing to export yet" /></div> : null}

      <div className="grid grid-3">
        {CARDS.map((c) => {
          const res = results[c.id];
          const prior = m?.exports.find((e) => e.format === c.id || (c.id === 'csl' && e.format === 'csl-json'));
          return (
            <div key={c.id} className={`fmt ${c.primary ? 'primary' : ''}`}>
              <div className="ic" aria-hidden="true">
                {c.ic}
              </div>
              <div className="grow" style={{ minWidth: 0 }}>
                <b>
                  {c.title}
                  {c.derived ? (
                    <>
                      {' '}
                      <Chip tone="warn" title="Adds content (slide structure) beyond the report; labelled derived in the manifest">
                        derived
                      </Chip>
                    </>
                  ) : null}
                  {c.themed ? (
                    <>
                      {' '}
                      <Chip>{theme}</Chip>
                    </>
                  ) : null}
                </b>
                <div className="meta">{m ? c.meta(n) : <Skeleton w="80%" h={12} />}</div>
                {res ? (
                  <div className="meta" style={{ color: 'var(--ok)' }}>
                    ✓ {res.filename} · {bytes(res.size)} · sha256 {shortSha(res.sha256)}
                    {res.cached ? ' · cached' : ''}
                    {res.backend ? ` · ${res.backend}` : ''}
                  </div>
                ) : prior ? (
                  <div className="meta">previously exported {prior.created_at.slice(0, 10)} · {bytes(prior.size_bytes)}</div>
                ) : null}
                {c.siblings ? (
                  <div className="row" style={{ marginTop: 6 }}>
                    {c.siblings.map((s) => (
                      <button key={s} type="button" className="btn btn-sm btn-ghost" onClick={() => run(s)} disabled={!m || noReport || busy !== null}>
                        {busy === s ? <span className="spin" aria-hidden="true" /> : '⤓'} {s === 'csl' ? 'CSL-JSON' : s.toUpperCase()}
                        {results[s] ? ' ✓' : ''}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
              <button type="button" className={`btn ${c.primary ? 'btn-primary' : 'btn-ghost'}`} onClick={() => run(c.id, c.themed)} disabled={!m || noReport || busy !== null} aria-label={`Download ${c.title}`}>
                {busy === c.id ? <span className="spin" aria-hidden="true" /> : '⤓'}
                {c.primary ? ' Download' : ''}
              </button>
            </div>
          );
        })}
      </div>

      <h2 style={{ margin: 'var(--s-6) 0 var(--s-3)' }}>
        Send to connected apps <Chip>under your identity</Chip>
      </h2>
      <div className="grid grid-3">
        {[
          { name: 'Notion', text: 'Create a page in your workspace with headings, paragraphs, bullets and a Sources table.', cta: 'Connect Notion ↗' },
          { name: 'Google Docs', text: 'Upload the DOCX to Drive and convert to a Google Doc you own.', cta: 'Connect Google ↗' },
          { name: 'Gmail', text: 'Email the PDF and a summary from your address.', cta: 'Connect Google ↗' },
        ].map((app) => (
          <div key={app.name} className="glass card">
            <b>{app.name}</b>
            <div className="muted small" style={{ margin: '6px 0 10px' }}>
              {app.text}
            </div>
            <button
              type="button"
              className="btn"
              disabled={!connected}
              aria-disabled={!connected}
              title={connected ? undefined : "coming from the owner's gateways"}
              onClick={() => toast.push('warn', `${app.name}: connected-app export runs through the owner's gateway under your own 3LO grant; that runtime op is not part of this build yet.`, 8000)}
            >
              {app.cta}
            </button>
            {!connected ? <div className="faint" style={{ marginTop: 6 }}>Coming from the owner's gateways (per-user 3LO; contract in 11-architecture §8).</div> : null}
          </div>
        ))}
      </div>
      <div className="faint" style={{ marginTop: 16 }}>Downloads are short-lived signed links (10 minutes) minted for you after your session is verified. No public storage is involved.</div>
    </>
  );
}

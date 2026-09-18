// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Compare two runs: headers, KPI row, sections table, claims diff, sources with in A / in B / cited by.
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useApi } from '../api';
import { Chip } from '../components/Chip';
import { ModelName } from '../components/ModelName';
import { ErrorBanner, Skeleton } from '../components/States';
import { duration, fmtDate, hostOf, shortId, usd } from '../lib/format';
import { useAsync } from '../lib/useAsync';
import type { PackageSummary, Source } from '../types';

function Header({ label, p, tone }: { label: string; p: PackageSummary; tone?: 'info' }) {
  return (
    <div className="glass card col">
      <h3>
        {label} · <Link to={`/p/${p.job_id}`}>{shortId(p.job_id)}</Link>
        <Chip tone={tone}>
          {p.lineage.relation !== 'root' ? `${p.lineage.relation} · ` : ''}
          <ModelName id={p.models.writer.model_id} name={p.models.writer.human_name} showId={false} inline /> writer
        </Chip>
      </h3>
      <div className="faint">
        {fmtDate(p.completed_at ?? p.created_at)} · {usd(p.cost_usd)} · {p.counts.sources} sources · {p.counts.citations_verified}/{p.counts.citations_verified + p.counts.citations_unverified} verified
      </div>
      <div className="muted small ellipsis" style={{ marginTop: 4 }}>
        {p.title ?? p.question}
      </div>
    </div>
  );
}

export default function ComparePage() {
  const { a = '', b = '' } = useParams();
  const api = useApi();
  const navigate = useNavigate();
  const cmp = useAsync(() => api.packagesCompare(a, b), [api, a, b]);

  if (cmp.error) {
    return (
      <>
        <div className="crumbs">
          <Link to="/">Library</Link> / compare
        </div>
        <ErrorBanner error={cmp.error} title="Could not compare these packages" onRetry={cmp.reload} />
      </>
    );
  }
  const c = cmp.data;
  if (!c) {
    return (
      <>
        <div className="crumbs">
          <Link to="/">Library</Link> / compare
        </div>
        <h1>Compare two runs</h1>
        <div className="grid grid-2" style={{ marginTop: 16 }}>
          <div className="glass card">
            <Skeleton h={22} w="70%" />
            <div style={{ height: 8 }} />
            <Skeleton h={12} w="50%" />
          </div>
          <div className="glass card">
            <Skeleton h={22} w="70%" />
            <div style={{ height: 8 }} />
            <Skeleton h={12} w="50%" />
          </div>
        </div>
      </>
    );
  }

  const onlyB = c.sections.filter((s) => s.in_b && !s.in_a).length;
  const onlyA = c.sections.filter((s) => s.in_a && !s.in_b).length;
  const ratio = c.run.cost_a > 0 ? c.run.cost_b / c.run.cost_a : null;
  const sameQuestion = c.a.question.trim() === c.b.question.trim();
  const rows: { src: Source; inA: boolean; inB: boolean }[] = [
    ...c.sources.shared.map((src) => ({ src, inA: true, inB: true })),
    ...c.sources.only_a.map((src) => ({ src, inA: true, inB: false })),
    ...c.sources.only_b.map((src) => ({ src, inA: false, inB: true })),
  ];

  return (
    <>
      <div className="crumbs">
        <Link to="/">Library</Link> / <Link to={`/p/${a}`}>{shortId(a)}</Link> ⇄ <Link to={`/p/${b}`}>{shortId(b)}</Link>
      </div>
      <div className="topbar">
        <div>
          <h1>Compare two runs</h1>
          <div>{sameQuestion ? 'Same question, different run. Sections, claims and sources side by side.' : 'Different questions — sections and sources are compared as-is.'}</div>
        </div>
        <div className="actions">
          <button type="button" className="btn" onClick={() => navigate(`/compare/${b}/${a}`)}>
            Swap
          </button>
          <Link className="btn" to={`/p/${a}`}>
            Open A
          </Link>
          <Link className="btn btn-primary" to={`/p/${b}`}>
            Open B
          </Link>
        </div>
      </div>

      <div className="grid grid-2">
        <Header label="A" p={c.a} />
        <Header label="B" p={c.b} tone="info" />
      </div>

      <div className="glass card" style={{ marginTop: 16 }}>
        <div className="grid grid-4">
          <div className="kpi">
            <b>+{c.sources.only_b.length}</b>
            <span>sources only in B</span>
            <div className="hint">−{c.sources.only_a.length} only in A</div>
          </div>
          <div className="kpi">
            <b>{c.sources.shared.length}</b>
            <span>shared sources</span>
          </div>
          <div className="kpi">
            <b>
              {onlyB ? `+${onlyB}` : ''}
              {onlyB && onlyA ? ' / ' : ''}
              {onlyA ? `−${onlyA}` : ''}
              {!onlyA && !onlyB ? '±0' : ''}
            </b>
            <span>sections in B vs A</span>
          </div>
          <div className="kpi">
            <b>{ratio === null ? '—' : `${ratio.toFixed(1)}×`}</b>
            <span>cost B / A</span>
            <div className="hint">
              {usd(c.run.cost_b)} vs {usd(c.run.cost_a)} · {duration(c.run.seconds_b)} vs {duration(c.run.seconds_a)} · {c.run.citations_b} vs {c.run.citations_a} citations verified
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <div className="glass card">
          <div style={{ fontWeight: 700, marginBottom: 6 }}>Sections</div>
          {c.sections.length === 0 ? <div className="faint">No section headings in either report.</div> : null}
          {c.sections.map((s) => (
            <div key={s.title} className={s.in_a && s.in_b ? 'same' : s.in_b ? 'diff-add' : 'diff-del'}>
              {s.in_a && s.in_b ? '' : s.in_b ? '＋ ' : '－ '}
              {s.title}
              <span className="faint"> — {s.in_a && s.in_b ? 'both' : s.in_b ? 'B only' : 'A only'}</span>
            </div>
          ))}
        </div>
        <div className="glass card">
          <div style={{ fontWeight: 700, marginBottom: 6 }}>Claims that differ</div>
          {c.claims.length === 0 ? <div className="faint">No differing claims were detected.</div> : null}
          {c.claims.map((cl, i) => (
            <div key={i}>
              {cl.a ? (
                <div className="diff-del">
                  A: {cl.a} {cl.markers_a.map((m) => <span key={m} className="mark">{m}</span>)}
                </div>
              ) : null}
              {cl.b ? (
                <div className="diff-add">
                  B{cl.a ? '' : ' adds'}: {cl.b} {cl.markers_b.map((m) => <span key={m} className="mark">{m}</span>)}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </div>

      <div className="glass card" style={{ marginTop: 16 }}>
        <div style={{ fontWeight: 700, marginBottom: 6 }}>
          Sources <span className="faint">({rows.length})</span>
        </div>
        <div className="table-wrap">
          <table className="table" aria-label="Sources in A and B">
            <thead>
              <tr>
                <th>Source</th>
                <th>In A</th>
                <th>In B</th>
                <th>Cited by</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ src, inA, inB }) => (
                <tr key={`${src.source_id}-${inA}-${inB}`}>
                  <td>
                    <div className="title ellipsis" style={{ maxWidth: 560 }}>
                      {src.url ? (
                        <a href={src.url} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit', textDecoration: 'none' }}>
                          {src.title ?? src.url}
                        </a>
                      ) : (
                        src.title
                      )}
                    </div>
                    <div className="sub">{hostOf(src.url)}</div>
                  </td>
                  <td>{inA ? '✓' : '—'}</td>
                  <td>{inB ? '✓' : '—'}</td>
                  <td>
                    {src.cited_by?.length ? (
                      <>
                        <span className="faint">{inA ? 'A' : 'B'}</span>
                        {src.cited_by.map((m) => (
                          <span key={m} className="mark">
                            {m}
                          </span>
                        ))}
                      </>
                    ) : (
                      <span className="faint">uncited</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

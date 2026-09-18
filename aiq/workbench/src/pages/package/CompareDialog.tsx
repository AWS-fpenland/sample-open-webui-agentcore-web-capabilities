// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useApi } from '../../api';
import { Dialog } from '../../components/Dialog';
import { ModelName } from '../../components/ModelName';
import { ErrorBanner, Skeleton } from '../../components/States';
import { fmtDate, shortId, usd } from '../../lib/format';
import { useAsync } from '../../lib/useAsync';
import type { PackageSummary } from '../../types';

export function CompareDialog({ open, jobId, rootId, onClose }: { open: boolean; jobId: string; rootId: string; onClose: () => void }) {
  const api = useApi();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const list = useAsync(() => api.packagesList({ limit: 50 }), [api], open);
  const candidates = useMemo(() => {
    const items = (list.data?.items ?? []).filter((p) => p.job_id !== jobId && p.status === 'completed');
    const term = q.trim().toLowerCase();
    const filtered = term ? items.filter((p) => (p.title ?? p.question).toLowerCase().includes(term) || p.job_id.includes(term)) : items;
    return [...filtered].sort((a, b) => Number(b.lineage.root_package_id === rootId) - Number(a.lineage.root_package_id === rootId));
  }, [list.data, jobId, rootId, q]);

  return (
    <Dialog open={open} title="Compare with…" subtitle="Same-question runs first (shared lineage), then any completed package." onClose={onClose}>
      <div className="stack">
        <input className="input" aria-label="Filter packages" placeholder="Filter by title or id…" value={q} onChange={(e) => setQ(e.target.value)} />
        {list.error ? (
          <ErrorBanner error={list.error} onRetry={list.reload} />
        ) : list.loading && !list.data ? (
          <>
            <Skeleton h={40} />
            <Skeleton h={40} />
          </>
        ) : candidates.length === 0 ? (
          <div className="empty">No other completed package to compare with.</div>
        ) : (
          <div className="table-wrap">
            <table className="table tight" aria-label="Candidates">
              <tbody>
                {candidates.map((p: PackageSummary) => (
                  <tr key={p.job_id}>
                    <td>
                      <div className="title">{p.title ?? p.question}</div>
                      <div className="sub">
                        <span className="mono">{shortId(p.job_id)}</span>
                        {p.lineage.root_package_id === rootId ? <span className="chip chip-info">same lineage · {p.lineage.relation}</span> : null}
                        <span>{fmtDate(p.completed_at ?? p.created_at)}</span>
                        <span>{usd(p.cost_usd)}</span>
                      </div>
                    </td>
                    <td>
                      <ModelName id={p.models.writer.model_id} name={p.models.writer.human_name} showId={false} />
                    </td>
                    <td className="actions">
                      <button type="button" className="btn btn-sm" onClick={() => navigate(`/compare/${jobId}/${p.job_id}`)}>
                        ⇄ Compare
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Dialog>
  );
}

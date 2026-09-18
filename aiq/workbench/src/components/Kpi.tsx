// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';

export function KpiTile({ value, label, hint, bar, progress, loading }: { value: ReactNode; label: string; hint?: ReactNode; bar?: number; progress?: number; loading?: boolean }) {
  return (
    <div className="glass card kpi" aria-busy={loading}>
      {loading ? <span className="skeleton" style={{ width: '50%', height: 26 }} /> : <b>{value}</b>}
      <span>{label}</span>
      {bar !== undefined ? <div className="gradient-bar" style={{ width: `${Math.max(8, Math.min(100, bar))}%` }} /> : null}
      {progress !== undefined ? (
        <div className="progress" aria-hidden="true">
          <i style={{ width: `${Math.min(100, progress)}%` }} />
        </div>
      ) : null}
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

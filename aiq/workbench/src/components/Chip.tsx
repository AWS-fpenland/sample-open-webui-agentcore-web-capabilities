// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';
import type { JobStatus } from '../types';

export type Tone = 'neutral' | 'ok' | 'warn' | 'err' | 'info';

export function Chip({ tone = 'neutral', title, children, onClick, pressed, className = '' }: { tone?: Tone; title?: string; children: ReactNode; onClick?: () => void; pressed?: boolean; className?: string }) {
  const cls = `chip ${tone === 'neutral' ? '' : `chip-${tone}`} ${onClick ? 'chip-btn' : ''} ${className}`.trim();
  if (onClick) {
    return (
      <button type="button" className={cls} title={title} onClick={onClick} aria-pressed={pressed}>
        {children}
      </button>
    );
  }
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

const STATUS: Record<JobStatus, { tone: Tone; icon: string; label: string }> = {
  queued: { tone: 'info', icon: '◔', label: 'queued' },
  clarifying: { tone: 'info', icon: '?', label: 'clarifying' },
  running: { tone: 'warn', icon: '●', label: 'running' },
  cancelling: { tone: 'warn', icon: '◌', label: 'cancelling' },
  completed: { tone: 'ok', icon: '✓', label: 'completed' },
  failed: { tone: 'err', icon: '✕', label: 'failed' },
  cancelled: { tone: 'neutral', icon: '–', label: 'cancelled' },
};

export function StatusChip({ status, progress, title }: { status: JobStatus; progress?: { phase: string; pct: number } | null; title?: string }) {
  const s = STATUS[status] ?? { tone: 'neutral' as Tone, icon: '·', label: status };
  const live = status === 'running' || status === 'queued';
  return (
    <span className={`chip chip-${s.tone}`} title={title ?? (progress ? `${progress.phase} · ${progress.pct}%` : s.label)}>
      {live ? <i className="dot" aria-hidden="true" /> : <span aria-hidden="true">{s.icon}</span>}
      {s.label}
      {live && progress ? (
        <>
          <span>{Math.round(progress.pct)}%</span>
          <span className="progress" aria-hidden="true">
            <i style={{ width: `${Math.min(100, progress.pct)}%` }} />
          </span>
        </>
      ) : null}
    </span>
  );
}

// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Loading skeletons, empty states, error banners — every list and view uses these.
import type { ReactNode } from 'react';
import type { ApiError } from '../api/client';
import { useSession } from '../session';

export function Skeleton({ w = '100%', h = 14, style }: { w?: number | string; h?: number; style?: React.CSSProperties }) {
  return <span className="skeleton" style={{ width: w, height: h, ...style }} aria-hidden="true" />;
}

export function TableSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div className="sk-row" key={i}>
          <Skeleton h={16} />
          <Skeleton h={16} w="70%" />
          <Skeleton h={16} w="60%" />
          <Skeleton h={16} w="50%" />
          <Skeleton h={16} w="40%" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="illo" aria-hidden="true" />
      <h3>{title}</h3>
      {children ? <div className="small">{children}</div> : null}
      {action ? <div style={{ marginTop: 14 }}>{action}</div> : null}
    </div>
  );
}

export function ErrorBanner({ error, onRetry, title = 'Something went wrong' }: { error: ApiError | Error | string; onRetry?: () => void; title?: string }) {
  const session = useSession();
  const message = typeof error === 'string' ? error : error.message;
  const code = typeof error === 'string' ? undefined : (error as ApiError).code;
  const status = typeof error === 'string' ? undefined : (error as ApiError).status;
  const unauth = code === 'unauthenticated';
  return (
    <div className="banner" role="alert">
      <span aria-hidden="true">⚠</span>
      <div className="grow">
        <b>{unauth ? 'Your session is not valid for the runtime' : title}</b>
        <div>{message}</div>
        {code || status ? (
          <div className="code">
            {code ?? 'error'}
            {status ? ` · HTTP ${status}` : ''}
          </div>
        ) : null}
      </div>
      <div className="actions">
        {unauth ? (
          <button type="button" className="btn btn-sm" onClick={() => session.signIn()}>
            Sign in again
          </button>
        ) : null}
        {onRetry ? (
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            Retry
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function InfoBanner({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' | 'ok' }) {
  return (
    <div className={`banner ${tone}`} role="status">
      <span aria-hidden="true">{tone === 'ok' ? '✓' : tone === 'warn' ? '!' : 'i'}</span>
      <div className="grow">{children}</div>
    </div>
  );
}

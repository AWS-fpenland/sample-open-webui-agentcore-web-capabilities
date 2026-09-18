// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export function usd(v: number | null | undefined, digits = 2): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—';
  if (v !== 0 && Math.abs(v) < 0.01 && digits <= 2) return `$${v.toFixed(4)}`;
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: Math.max(digits, 2) });
}

export function num(v: number | null | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—';
  return v.toLocaleString('en-US');
}

export function tokens(v: number | null | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—';
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 10_000) return `${(v / 1_000).toFixed(1)}K`;
  return v.toLocaleString('en-US');
}

export function bytes(n: number | null | undefined): string {
  if (n === undefined || n === null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function ms(v: number | null | undefined): string {
  if (v === undefined || v === null) return '—';
  return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === undefined || seconds === null || Number.isNaN(seconds)) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function pct(part: number, total: number): string {
  if (!total) return '—';
  return `${Math.round((part / total) * 100)}%`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  if (d === 1) return 'yesterday';
  if (d < 14) return `${d} days ago`;
  return fmtDate(iso);
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }), hour: '2-digit', minute: '2-digit' });
}

export function fmtDateTimeZ(iso: string | null | undefined): string {
  if (!iso) return '—';
  return iso.replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

/** job_<32hex> → pkg_<8hex>… (the vocabulary is "package" everywhere the user reads it). */
export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  const hex = id.replace(/^(job|pkg)_/, '');
  return `pkg_${hex.slice(0, 8)}…`;
}

export function hostOf(url: string | null | undefined): string {
  if (!url) return '';
  try {
    return new URL(url).host.replace(/^www\./, '');
  } catch {
    return url;
  }
}

export function shortSha(sha: string | null | undefined, n = 8): string {
  return sha ? `${sha.slice(0, n)}…` : '—';
}

export function withinDays(iso: string, days: number): boolean {
  const t = Date.parse(iso);
  return !Number.isNaN(t) && Date.now() - t <= days * 86400_000;
}

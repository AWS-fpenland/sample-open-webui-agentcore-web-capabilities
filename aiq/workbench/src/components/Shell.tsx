// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// App shell: glass sidebar (Library, Model Lab + contextual entries), user chip, theme/density toggles, mock chip.
import type { ReactNode } from 'react';
import { NavLink, useLocation, useParams } from 'react-router-dom';
import { useSession } from '../session';
import { useDensity, useTheme } from '../lib/theme';
import { shortId } from '../lib/format';

function initials(email: string | null): string {
  if (!email) return '?';
  const [name] = email.split('@');
  return name.slice(0, 2).toUpperCase();
}

export function Shell({ children }: { children: ReactNode }) {
  const session = useSession();
  const [theme, toggleTheme] = useTheme();
  const [density, toggleDensity] = useDensity();
  const loc = useLocation();
  const params = useParams();
  const jobId = loc.pathname.startsWith('/p/') || loc.pathname.startsWith('/exports/') ? params.jobId ?? loc.pathname.split('/')[2] : undefined;
  const compare = loc.pathname.startsWith('/compare/');

  return (
    <div className="app">
      <a className="skip-link" href="#content">
        Skip to content
      </a>
      <nav className="nav glass" aria-label="Primary">
        <NavLink to="/" className="brand" aria-label="AI-Q Workbench home">
          <span className="brand-mark" aria-hidden="true" />
          <b className="gradient-text">AI-Q Workbench</b>
        </NavLink>
        <NavLink to="/" end aria-current={loc.pathname === '/' ? 'page' : undefined}>
          <span className="ico" aria-hidden="true">▦</span>
          <span>Library</span>
        </NavLink>
        <NavLink to="/lab" aria-current={loc.pathname.startsWith('/lab') ? 'page' : undefined}>
          <span className="ico" aria-hidden="true">◈</span>
          <span>Model Lab</span>
        </NavLink>
        {jobId ? (
          <>
            <div className="nav-section">{shortId(jobId)}</div>
            <NavLink to={`/p/${jobId}`} aria-current={loc.pathname === `/p/${jobId}` ? 'page' : undefined}>
              <span className="ico" aria-hidden="true">▤</span>
              <span>Package</span>
            </NavLink>
            <NavLink to={`/exports/${jobId}`} aria-current={loc.pathname.startsWith('/exports/') ? 'page' : undefined}>
              <span className="ico" aria-hidden="true">⤓</span>
              <span>Exports</span>
            </NavLink>
          </>
        ) : null}
        {compare ? (
          <>
            <div className="nav-section">Comparison</div>
            <a href={loc.pathname} aria-current="page">
              <span className="ico" aria-hidden="true">⇄</span>
              <span>Compare</span>
            </a>
          </>
        ) : null}
        <div className="nav-bottom">
          <div className="nav-actions">
            <button type="button" className="btn btn-sm" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`} title="Theme">
              ◐ <span>{theme === 'dark' ? 'Dark' : 'Light'}</span>
            </button>
            <button type="button" className="btn btn-sm" onClick={toggleDensity} aria-pressed={density === 'compact'} aria-label="Toggle compact density" title="Density">
              ☰ <span>{density === 'compact' ? 'Compact' : 'Cozy'}</span>
            </button>
          </div>
          <div className="userchip" title={session.email ?? 'Signed in'}>
            <span className="avatar" aria-hidden="true">
              {initials(session.email)}
            </span>
            <span className="ellipsis" style={{ minWidth: 0 }}>
              <span className="ellipsis" style={{ display: 'block', color: 'var(--fg-muted)', fontWeight: 600 }}>
                {session.email ?? 'researcher'}
              </span>
              <span style={{ display: 'block' }}>{session.mock ? 'mock identity' : 'Same Cognito session as Open WebUI'}</span>
            </span>
          </div>
          <div className="nav-actions">
            {session.mock ? (
              <span className="chip chip-warn mock-chip" title="Fixtures instead of the runtime; no sign-in required">
                ◌ mock mode
              </span>
            ) : null}
            <NavLink to="/signout" className="btn btn-sm btn-ghost">
              Sign out
            </NavLink>
          </div>
        </div>
      </nav>
      <main className="main" id="content" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}

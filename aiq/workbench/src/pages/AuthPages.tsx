// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { MOCK_FLAG_KEY } from '../config';
import { POST_LOGIN_KEY, useSession } from '../session';

function Centered({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="center">
      <div className="glass card">
        <div className="brand" style={{ margin: '0 0 14px' }}>
          <span className="brand-mark" aria-hidden="true" />
          <b className="gradient-text">AI-Q Workbench</b>
        </div>
        <h1>{title}</h1>
        {children}
      </div>
    </div>
  );
}

export function SignInPage() {
  const session = useSession();
  const loc = useLocation();
  return (
    <Centered title="Research Workbench">
      <p className="muted small">Every research job becomes a durable package: report, sources, artifacts, run metadata, lineage. Sign in with the same account you use in Open WebUI — one Cognito session covers both.</p>
      {session.error ? (
        <div className="banner" role="alert" style={{ marginBottom: 12 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <b>Sign-in failed</b>
            {session.error}
          </div>
        </div>
      ) : null}
      <button type="button" className="btn btn-primary" style={{ width: '100%', justifyContent: 'center' }} onClick={() => session.signIn(loc.pathname + loc.search)}>
        Sign in
      </button>
      <div className="faint" style={{ marginTop: 14 }}>
        PKCE code flow · tokens stay in this tab's session storage · <Link to="/?mock=1">preview with fixtures</Link>
      </div>
    </Centered>
  );
}

export function LoadingPage({ text }: { text: string }) {
  return (
    <Centered title="Research Workbench">
      <div className="row" role="status">
        <span className="spin" aria-hidden="true" style={{ width: 14, height: 14, border: '2px solid var(--fg-muted)', borderRightColor: 'transparent', borderRadius: '50%', display: 'inline-block' }} />
        <span className="muted">{text}</span>
      </div>
    </Centered>
  );
}

export function AuthCallbackPage() {
  const session = useSession();
  const navigate = useNavigate();
  useEffect(() => {
    if (session.mock || session.authenticated) {
      const target = sessionStorage.getItem(POST_LOGIN_KEY) || '/';
      sessionStorage.removeItem(POST_LOGIN_KEY);
      navigate(target.startsWith('/auth') ? '/' : target, { replace: true });
    }
  }, [session.mock, session.authenticated, navigate]);
  if (session.error) {
    return (
      <Centered title="Sign-in did not complete">
        <p className="muted small">{session.error}</p>
        <button type="button" className="btn btn-primary" onClick={() => session.signIn('/')}>
          Try again
        </button>
      </Centered>
    );
  }
  return <LoadingPage text="Completing sign-in…" />;
}

export function SignOutPage() {
  const session = useSession();
  const navigate = useNavigate();
  useEffect(() => {
    if (session.mock) {
      sessionStorage.removeItem(MOCK_FLAG_KEY);
      navigate('/', { replace: true });
      return;
    }
    session.signOut();
  }, [session, navigate]);
  return <LoadingPage text="Signing out…" />;
}

export function NotFoundPage() {
  return (
    <div className="empty">
      <div className="illo" aria-hidden="true" />
      <h3>Nothing here</h3>
      <div className="small">
        <Link to="/">Back to the library</Link>
      </div>
    </div>
  );
}

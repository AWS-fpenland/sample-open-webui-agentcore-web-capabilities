// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
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

/** No landing page: an unauthenticated visitor is sent to Cognito Managed Login immediately, and comes back to the route they asked for. */
export function SignInRedirectPage() {
  const session = useSession();
  const loc = useLocation();
  const failed = Boolean(session.error);
  useEffect(() => {
    if (!failed) session.signIn(loc.pathname + loc.search);
  }, [failed, session, loc.pathname, loc.search]);
  if (failed) {
    return (
      <Centered title="Sign-in required">
        <div className="banner" role="alert" style={{ marginBottom: 12 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <b>Sign-in did not complete</b>
            {session.error}
          </div>
        </div>
        <button type="button" className="btn btn-primary" style={{ width: '100%', justifyContent: 'center' }} onClick={() => session.signIn(loc.pathname + loc.search)}>
          Sign in
        </button>
      </Centered>
    );
  }
  return <LoadingPage text="Redirecting to sign-in…" />;
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
    if (session.authenticated) {
      const target = sessionStorage.getItem(POST_LOGIN_KEY) || '/';
      sessionStorage.removeItem(POST_LOGIN_KEY);
      navigate(target.startsWith('/auth') ? '/' : target, { replace: true });
    }
  }, [session.authenticated, navigate]);
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
  useEffect(() => {
    session.signOut();
  }, [session]);
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

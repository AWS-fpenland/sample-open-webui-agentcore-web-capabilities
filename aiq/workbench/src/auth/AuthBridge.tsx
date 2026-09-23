// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Bridges react-oidc-context (Cognito PKCE) to the Session + Api contexts the pages consume.
import { useMemo, useRef, type ReactNode } from 'react';
import { useAuth } from 'react-oidc-context';
import { ApiContext } from '../api';
import { createRealApi } from '../api/client';
import { cognitoLogoutUrl } from '../config';
import { POST_LOGIN_KEY, SessionContext, type Session } from '../session';
import type { WorkbenchConfig } from '../types';

export function AuthBridge({ cfg, children }: { cfg: WorkbenchConfig; children: ReactNode }) {
  const auth = useAuth();
  const tokenRef = useRef<string | undefined>(undefined);
  tokenRef.current = auth.user && !auth.user.expired ? auth.user.access_token : undefined;
  const api = useMemo(() => createRealApi(cfg, () => tokenRef.current), [cfg]);
  const session = useMemo<Session>(
    () => ({
      authenticated: auth.isAuthenticated,
      loading: auth.isLoading,
      error: auth.error?.message ?? null,
      email: (auth.user?.profile?.email as string | undefined) ?? null,
      signIn(returnTo) {
        sessionStorage.setItem(POST_LOGIN_KEY, returnTo ?? window.location.pathname + window.location.search);
        void auth.signinRedirect();
      },
      signOut() {
        void auth.removeUser().then(() => window.location.assign(cognitoLogoutUrl(cfg)));
      },
    }),
    [auth, cfg],
  );
  return (
    <ApiContext.Provider value={api}>
      <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
    </ApiContext.Provider>
  );
}

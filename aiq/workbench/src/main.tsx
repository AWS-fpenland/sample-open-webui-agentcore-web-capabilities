// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Boot: load /config.json → Cognito PKCE (react-oidc-context) + real runtime API. No unauthenticated mode exists.
import './styles/tokens.css';
import './styles/app.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { WebStorageStateStore } from 'oidc-client-ts';
import { AuthProvider } from 'react-oidc-context';
import App from './App';
import { AuthBridge } from './auth/AuthBridge';
import { ConfigContext, loadConfig } from './config';

const root = createRoot(document.getElementById('root')!);

loadConfig()
  .then((cfg) => {
    for (const k of ['userPoolId', 'clientId', 'cognitoDomain', 'runtimeArn'] as const) {
      if (!cfg[k] || String(cfg[k]).includes('PLACEHOLDER')) throw new Error(`config.json "${k}" is not set.`);
    }
    // Cognito user pools are OIDC-discoverable at the issuer URL; Managed Login renders the sign-in UX.
    // PKCE code flow, public client (no secret); tokens in sessionStorage (cleared with the tab); silent renew via refresh token.
    const oidcConfig = {
      authority: `https://cognito-idp.${cfg.region}.amazonaws.com/${cfg.userPoolId}`,
      client_id: cfg.clientId,
      redirect_uri: `${window.location.origin}/auth/callback`,
      response_type: 'code',
      scope: 'openid email profile',
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: true,
      onSigninCallback: () => {
        // strip ?code=&state=; AuthCallbackPage navigates to the saved route once the user is present
        window.history.replaceState({}, document.title, '/auth/callback');
      },
    };
    root.render(
      <StrictMode>
        <ConfigContext.Provider value={cfg}>
          <AuthProvider {...oidcConfig}>
            <AuthBridge cfg={cfg}>
              <BrowserRouter>
                <App />
              </BrowserRouter>
            </AuthBridge>
          </AuthProvider>
        </ConfigContext.Provider>
      </StrictMode>,
    );
  })
  .catch((err: Error) => {
    root.render(
      <div className="center">
        <div className="glass card">
          <h1>Workbench could not start</h1>
          <p className="muted">Deployment configuration could not be loaded: {err.message}</p>
        </div>
      </div>,
    );
  });

// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Deploy-time config from /config.json (written by the deploy step next to the bundle) — no ids baked into the build.
// There is no unauthenticated mode: every route requires a Cognito session (security mandate, 2026-09-19).
import { createContext, useContext } from 'react';
import type { WorkbenchConfig } from './types';

export const ConfigContext = createContext<WorkbenchConfig | null>(null);

export function useConfig(): WorkbenchConfig {
  const cfg = useContext(ConfigContext);
  if (!cfg) throw new Error('config not loaded');
  return cfg;
}

export async function loadConfig(): Promise<WorkbenchConfig> {
  const res = await fetch('/config.json', { cache: 'no-store' });
  if (!res.ok) throw new Error(`config.json: HTTP ${res.status}`);
  const cfg = (await res.json()) as WorkbenchConfig;
  for (const k of ['region', 'owuiUrl'] as const) if (!cfg[k]) throw new Error(`config.json is missing "${k}"`);
  return cfg;
}

/** Cognito Managed Login sign-out (clears the hosted session, then returns to the workbench). */
export function cognitoLogoutUrl(cfg: WorkbenchConfig): string {
  return (
    `https://${cfg.cognitoDomain}/logout?client_id=${encodeURIComponent(cfg.clientId)}` +
    `&logout_uri=${encodeURIComponent(`${window.location.origin}/`)}`
  );
}

// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Deploy-time config from /config.json (written by the deploy step next to the bundle) — no ids baked into the build.
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

export const MOCK_FLAG_KEY = 'aiq-wb-mock';

/** Mock mode: config.mock, `?mock=1` (remembered for the tab), never when `?mock=0`. */
export function resolveMockMode(cfg: WorkbenchConfig, search: string): boolean {
  const q = new URLSearchParams(search).get('mock');
  if (q === '0') {
    sessionStorage.removeItem(MOCK_FLAG_KEY);
    return false;
  }
  if (q === '1') sessionStorage.setItem(MOCK_FLAG_KEY, '1');
  return cfg.mock === true || sessionStorage.getItem(MOCK_FLAG_KEY) === '1';
}

/** Cognito Managed Login sign-out (clears the hosted session, then returns to the workbench). */
export function cognitoLogoutUrl(cfg: WorkbenchConfig): string {
  return (
    `https://${cfg.cognitoDomain}/logout?client_id=${encodeURIComponent(cfg.clientId)}` +
    `&logout_uri=${encodeURIComponent(`${window.location.origin}/`)}`
  );
}

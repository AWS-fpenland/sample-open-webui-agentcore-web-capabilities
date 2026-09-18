// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Theme (dark default, light via data-theme) and density (data-density=compact) persisted in localStorage.
import { useCallback, useEffect, useState } from 'react';

export type Theme = 'dark' | 'light';
export type Density = 'comfortable' | 'compact';
const THEME_KEY = 'aiq-wb-theme';
const DENSITY_KEY = 'aiq-wb-density';

function read<T extends string>(key: string, allowed: T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return allowed.includes(v as T) ? (v as T) : fallback;
  } catch {
    return fallback;
  }
}

export function applyTheme(t: Theme): void {
  document.documentElement.setAttribute('data-theme', t);
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {
    /* private mode */
  }
}

export function applyDensity(d: Density): void {
  if (d === 'compact') document.documentElement.setAttribute('data-density', 'compact');
  else document.documentElement.removeAttribute('data-density');
  try {
    localStorage.setItem(DENSITY_KEY, d);
  } catch {
    /* private mode */
  }
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => read(THEME_KEY, ['dark', 'light'], 'dark'));
  useEffect(() => applyTheme(theme), [theme]);
  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), []);
  return [theme, toggle];
}

export function useDensity(): [Density, () => void] {
  const [density, setDensity] = useState<Density>(() => read(DENSITY_KEY, ['comfortable', 'compact'], 'comfortable'));
  useEffect(() => applyDensity(density), [density]);
  const toggle = useCallback(() => setDensity((d) => (d === 'compact' ? 'comfortable' : 'compact')), []);
  return [density, toggle];
}

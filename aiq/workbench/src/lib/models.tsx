// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Lazily-loaded, shared Capability Matrix (the `models` op) so every screen can show human names next to ids.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useApi } from '../api';
import { ApiError, toApiError } from '../api/client';
import type { MatrixEntry, ModelsResponse } from '../types';

interface ModelsState {
  data: ModelsResponse | null;
  loading: boolean;
  error: ApiError | null;
  ensure: () => void;
  reload: () => void;
  setData: (d: ModelsResponse) => void;
  nameFor: (id: string | null | undefined, fallback?: string | null) => string;
  entryFor: (id: string | null | undefined, lane?: string | null) => MatrixEntry | undefined;
}

const Ctx = createContext<ModelsState | null>(null);

export function ModelsProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const [data, setData] = useState<ModelsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const requested = useRef(false);
  const [tick, setTick] = useState(0);

  const ensure = useCallback(() => {
    if (!requested.current) {
      requested.current = true;
      setTick((t) => t + 1);
    }
  }, []);
  const reload = useCallback(() => {
    requested.current = true;
    setTick((t) => t + 1);
  }, []);

  useEffect(() => {
    if (!requested.current || tick === 0) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .models()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(toApiError(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, tick]);

  const byId = useMemo(() => {
    const m = new Map<string, MatrixEntry[]>();
    for (const e of data?.entries ?? []) {
      const list = m.get(e.id) ?? [];
      list.push(e);
      m.set(e.id, list);
      if (!m.has(e.base_model_id)) m.set(e.base_model_id, [e]);
    }
    return m;
  }, [data]);

  const value = useMemo<ModelsState>(
    () => ({
      data,
      loading,
      error,
      ensure,
      reload,
      setData,
      nameFor: (id, fallback) => {
        if (!id) return fallback ?? '—';
        return byId.get(id)?.[0]?.name ?? fallback ?? id;
      },
      entryFor: (id, lane) => {
        if (!id) return undefined;
        const list = byId.get(id);
        if (!list) return undefined;
        return (lane && list.find((e) => e.lane === lane)) || list.find((e) => e.offered) || list[0];
      },
    }),
    [data, loading, error, ensure, reload, byId],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useModels(autoload = true): ModelsState {
  const s = useContext(Ctx);
  if (!s) throw new Error('ModelsProvider missing');
  const { ensure } = s;
  useEffect(() => {
    if (autoload) ensure();
  }, [autoload, ensure]);
  return s;
}

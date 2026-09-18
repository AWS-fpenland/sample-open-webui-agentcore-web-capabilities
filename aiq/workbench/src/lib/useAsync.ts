// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';
import { ApiError, toApiError } from '../api/client';

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: ApiError | null;
  reload: () => void;
  setData: (updater: T | ((prev: T | null) => T | null)) => void;
}

/** Load-on-mount + reload; ignores stale results; keeps previous data while reloading. */
export function useAsync<T>(fn: () => Promise<T>, deps: DependencyList, enabled = true): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [error, setError] = useState<ApiError | null>(null);
  const [tick, setTick] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fnRef
      .current()
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, enabled]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const set = useCallback((u: T | ((prev: T | null) => T | null)) => {
    setData((prev) => (typeof u === 'function' ? (u as (p: T | null) => T | null)(prev) : u));
  }, []);
  return { data, loading, error, reload, setData: set };
}

/** Fire-and-track for button actions (export, save...). */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<R | null>(null);
  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setBusy(true);
      setError(null);
      try {
        const r = await fn(...args);
        setResult(r);
        return r;
      } catch (e) {
        setError(toApiError(e));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [fn],
  );
  const reset = useCallback(() => {
    setError(null);
    setResult(null);
  }, []);
  return { run, busy, error, result, reset };
}

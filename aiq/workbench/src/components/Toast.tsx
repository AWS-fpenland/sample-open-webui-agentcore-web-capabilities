// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

interface Toast {
  id: number;
  tone: 'ok' | 'err' | 'warn' | 'info';
  text: ReactNode;
}
interface ToastApi {
  push: (tone: Toast['tone'], text: ReactNode, ttlMs?: number) => void;
}
const Ctx = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast['tone'], text: ReactNode, ttlMs = 5000) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, tone, text }]);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), ttlMs);
  }, []);
  const api = useMemo(() => ({ push }), [push]);
  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="toasts" aria-live="polite" aria-atomic="false">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            <span aria-hidden="true">{t.tone === 'ok' ? '✓' : t.tone === 'err' ? '✕' : t.tone === 'warn' ? '!' : 'i'}</span>
            <div className="grow">{t.text}</div>
            <button type="button" aria-label="Dismiss" onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>
              ×
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const t = useContext(Ctx);
  if (!t) throw new Error('ToastProvider missing');
  return t;
}

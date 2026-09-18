// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { KeyboardEvent } from 'react';

export interface TabDef<T extends string> {
  id: T;
  label: string;
  count?: number | string | null;
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: TabDef<T>[]; value: T; onChange: (v: T) => void; label: string }) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = tabs.findIndex((t) => t.id === value);
    if (e.key === 'ArrowRight') onChange(tabs[(i + 1) % tabs.length].id);
    else if (e.key === 'ArrowLeft') onChange(tabs[(i - 1 + tabs.length) % tabs.length].id);
    else if (e.key === 'Home') onChange(tabs[0].id);
    else if (e.key === 'End') onChange(tabs[tabs.length - 1].id);
    else return;
    e.preventDefault();
    const list = e.currentTarget;
    requestAnimationFrame(() => list.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus());
  };
  return (
    <div className="tabs" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" id={`tab-${t.id}`} aria-selected={t.id === value} aria-controls={`panel-${t.id}`} tabIndex={t.id === value ? 0 : -1} onClick={() => onChange(t.id)}>
          {t.label}
          {t.count !== undefined && t.count !== null ? <span className="count">({t.count})</span> : null}
        </button>
      ))}
    </div>
  );
}

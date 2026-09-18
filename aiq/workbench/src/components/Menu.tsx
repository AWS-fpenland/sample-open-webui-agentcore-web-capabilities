// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Dropdown menu with keyboard support (Arrow keys, Home/End, Escape) and outside-click dismissal.
import { useEffect, useRef, useState, type ReactNode } from 'react';

export interface MenuItem {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
  onSelect: () => void;
  separatorBefore?: boolean;
}

export function Menu({ label, items, primary, busy }: { label: ReactNode; items: MenuItem[]; primary?: boolean; busy?: boolean }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      const btns = [...(list.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
      const i = btns.indexOf(document.activeElement as HTMLButtonElement);
      if (e.key === 'Escape') setOpen(false);
      else if (e.key === 'ArrowDown') {
        e.preventDefault();
        btns[(i + 1) % btns.length]?.focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        btns[(i - 1 + btns.length) % btns.length]?.focus();
      } else if (e.key === 'Home') btns[0]?.focus();
      else if (e.key === 'End') btns[btns.length - 1]?.focus();
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    list.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="menu-wrap" ref={wrap}>
      <button type="button" className={`btn ${primary ? 'btn-primary' : ''}`} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)} disabled={busy}>
        {busy ? <span className="spin" aria-hidden="true" /> : null}
        {label} <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="menu" role="menu" ref={list}>
          {items.map((it) => (
            <div key={it.id}>
              {it.separatorBefore ? <div className="sep" role="separator" /> : null}
              <button
                type="button"
                role="menuitem"
                disabled={it.disabled}
                onClick={() => {
                  setOpen(false);
                  it.onSelect();
                }}
              >
                <span>{it.label}</span>
                {it.hint ? <span className="hint">{it.hint}</span> : null}
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

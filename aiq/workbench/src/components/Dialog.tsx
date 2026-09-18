// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Accessible modal: role=dialog, focus moves in, Escape / backdrop closes, focus returns to the opener.
import { useEffect, useRef, type ReactNode } from 'react';

export function Dialog({ open, title, subtitle, onClose, children, footer, wide }: { open: boolean; title: string; subtitle?: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>('select, input, button:not([data-close]), textarea, [tabindex]:not([tabindex="-1"])');
    (first ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
      if (e.key === 'Tab' && el) {
        const focusables = [...el.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea, [tabindex]:not([tabindex="-1"])')];
        if (!focusables.length) return;
        const firstEl = focusables[0];
        const lastEl = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === firstEl) {
          e.preventDefault();
          lastEl.focus();
        } else if (!e.shiftKey && document.activeElement === lastEl) {
          e.preventDefault();
          firstEl.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="glass dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title" ref={ref} tabIndex={-1} style={wide ? { width: 'min(960px, 100%)' } : undefined}>
        <header>
          <div>
            <h2 id="dlg-title">{title}</h2>
            {subtitle ? <div className="muted small">{subtitle}</div> : null}
          </div>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close dialog" data-close>
            ✕
          </button>
        </header>
        {children}
        {footer ? <footer>{footer}</footer> : null}
      </div>
    </div>
  );
}

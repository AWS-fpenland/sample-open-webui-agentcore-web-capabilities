// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { Chip } from './Chip';

const TAG_RE = /^[a-z0-9][a-z0-9._-]{0,47}$/;

export function TagEditor({ tags, onChange, onTagClick, busy }: { tags: string[]; onChange: (tags: string[]) => void; onTagClick?: (t: string) => void; busy?: boolean }) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const commit = () => {
    const t = draft.trim().toLowerCase();
    if (!t) {
      setAdding(false);
      return;
    }
    if (!TAG_RE.test(t)) {
      setErr('lowercase letters, digits, . _ - (max 48)');
      return;
    }
    if (!tags.includes(t)) onChange([...tags, t]);
    setDraft('');
    setErr(null);
    setAdding(false);
  };

  return (
    <div className="tags" aria-busy={busy}>
      {tags.map((t) => (
        <span key={t} className="chip">
          <button type="button" className="tag-x" style={{ padding: 0 }} onClick={() => onTagClick?.(t)} title={onTagClick ? `Filter by #${t}` : undefined}>
            #{t}
          </button>
          <button type="button" className="tag-x" aria-label={`Remove tag ${t}`} onClick={() => onChange(tags.filter((x) => x !== t))} disabled={busy}>
            ×
          </button>
        </span>
      ))}
      {adding ? (
        <span className="row-nowrap">
          <input
            className="input tag-input"
            autoFocus
            aria-label="New tag"
            aria-invalid={!!err}
            value={draft}
            placeholder="new-tag"
            onChange={(e) => {
              setDraft(e.target.value);
              setErr(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit();
              if (e.key === 'Escape') {
                setAdding(false);
                setDraft('');
              }
            }}
            onBlur={commit}
          />
          {err ? <span className="faint" style={{ color: 'var(--err)' }}>{err}</span> : null}
        </span>
      ) : (
        <Chip onClick={() => setAdding(true)} title="Add a tag">
          ＋ tag
        </Chip>
      )}
    </div>
  );
}

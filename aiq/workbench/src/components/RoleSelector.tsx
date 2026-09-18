// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Per-role model picker limited to entries the Capability Matrix marks selectable for that role. Shows price per 1M
// tokens and the role score; excluded models are listed disabled with their reason so nothing is hidden or substituted.
import type { MatrixEntry, ModelsResponse, ModelsSelection, ModelsValidated, Role } from '../types';
import { ROLES, ROLE_LABEL } from '../types';
import { usd } from '../lib/format';
import { laneLabel } from './ModelName';

export function selectionFromDefaults(models: ModelsResponse, base?: Partial<Record<Role, { model_id: string; lane: string }>> | null): ModelsSelection {
  const out: ModelsSelection = {};
  for (const role of ROLES) {
    const fromBase = base?.[role];
    const fromPrefs = models.prefs?.[role];
    const id = fromBase?.model_id ?? fromPrefs?.model_id ?? models.defaults[role];
    if (!id) continue;
    const e = models.entries.find((x) => x.id === id && (x.lane === (fromBase?.lane ?? fromPrefs?.lane) || x.offered)) ?? models.entries.find((x) => x.id === id);
    out[role] = { model_id: id, lane: e?.lane ?? fromBase?.lane ?? fromPrefs?.lane ?? 'converse' };
  }
  return out;
}

export function scoreClass(score: number | null | undefined, verdict?: string): string {
  if (score === null || score === undefined || verdict === 'not_evaluated') return 'score score-na';
  if (score >= 80) return 'score score-hi';
  if (score >= 50) return 'score score-mid';
  return 'score score-lo';
}

function optionLabel(e: MatrixEntry, role: Role): string {
  const r = e.roles[role];
  const score = r && r.verdict !== 'not_evaluated' ? ` · ${r.score}` : ' · n/e';
  const price = e.price.input_per_1m !== null ? ` · $${e.price.input_per_1m}/${e.price.output_per_1m}${e.price.source !== 'aws-published' ? ' est.' : ''}` : ' · unpriced';
  return `${e.name}${score}${price} · ${laneLabel(e.lane)}`;
}

export function RoleSelector({ models, value, onChange, roles = ROLES, validation, disabled }: { models: ModelsResponse; value: ModelsSelection; onChange: (v: ModelsSelection) => void; roles?: Role[]; validation?: ModelsValidated | null; disabled?: boolean }) {
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="role-pick">
        {roles.map((role) => {
          const selectable = models.entries.filter((e) => e.roles_selectable[role]).sort((a, b) => (b.roles?.[role]?.score ?? -1) - (a.roles?.[role]?.score ?? -1) || a.name.localeCompare(b.name));
          const excluded = models.entries.filter((e) => !e.roles_selectable[role]).slice(0, 12);
          const cur = value[role];
          const key = cur ? `${cur.lane}::${cur.model_id}` : '';
          const problem = validation?.problems.find((p) => p.role === role);
          return (
            <FragmentRow key={role} role={role} problem={problem?.reason}>
              <select
                id={`role-${role}`}
                className="select"
                aria-label={`${ROLE_LABEL[role]} model`}
                aria-invalid={!!problem}
                value={key}
                disabled={disabled}
                onChange={(e) => {
                  const [lane, ...rest] = e.target.value.split('::');
                  const id = rest.join('::');
                  onChange({ ...value, [role]: id ? { model_id: id, lane } : undefined });
                }}
              >
                {!cur ? <option value="">— choose —</option> : null}
                {cur && !selectable.some((e) => `${e.lane}::${e.id}` === key) ? <option value={key}>{cur.model_id} (not selectable for this role)</option> : null}
                <optgroup label={`Offered for ${ROLE_LABEL[role].toLowerCase()} (${selectable.length})`}>
                  {selectable.map((e) => (
                    <option key={`${e.lane}::${e.id}`} value={`${e.lane}::${e.id}`}>
                      {optionLabel(e, role)}
                    </option>
                  ))}
                </optgroup>
                {excluded.length ? (
                  <optgroup label="Excluded (see matrix for reasons)">
                    {excluded.map((e) => (
                      <option key={`x-${e.lane}::${e.id}`} value={`${e.lane}::${e.id}`} disabled>
                        {e.name} — {e.offered ? 'capability missing for this role' : e.exclusion_reasons[0] ?? 'excluded'}
                      </option>
                    ))}
                  </optgroup>
                ) : null}
              </select>
            </FragmentRow>
          );
        })}
      </div>
    </div>
  );
}

function FragmentRow({ role, problem, children }: { role: Role; problem?: string; children: React.ReactNode }) {
  return (
    <>
      <label htmlFor={`role-${role}`}>{ROLE_LABEL[role]}</label>
      <div>
        {children}
        {problem ? <div className="faint" style={{ color: 'var(--err)', marginTop: 4 }}>{problem}</div> : null}
      </div>
    </>
  );
}

export function EstimateLine({ validation, loading }: { validation: ModelsValidated | null | undefined; loading?: boolean }) {
  if (loading) return <div className="faint">Estimating…</div>;
  if (!validation) return null;
  return (
    <div className="xs muted">
      {validation.ok ? (
        <>
          Estimated deep run: <b style={{ color: 'var(--fg)' }}>{usd(validation.estimate_usd.low)} – {usd(validation.estimate_usd.high)}</b>{' '}
          <span className="faint">from Price List rates × typical token volumes</span>
        </>
      ) : (
        <span style={{ color: 'var(--err)' }}>
          {validation.problems.length} problem{validation.problems.length === 1 ? '' : 's'} — the runtime will reject this selection rather than substitute silently.
        </span>
      )}
    </div>
  );
}

// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// "Re-run with…": per-role picker (offered models only), live estimate via models.validate, submit → new package.
import { useEffect, useMemo, useState } from 'react';
import { useApi } from '../../api';
import { toApiError, type ApiError } from '../../api/client';
import { Dialog } from '../../components/Dialog';
import { EstimateLine, RoleSelector, selectionFromDefaults } from '../../components/RoleSelector';
import { ErrorBanner, Skeleton } from '../../components/States';
import { useModels } from '../../lib/models';
import type { ModelsSelection, ModelsValidated, Role, SummaryModel } from '../../types';
import { ROLES } from '../../types';

export function RerunDialog({ open, jobId, title, current, onClose, onAccepted }: { open: boolean; jobId: string; title: string; current: Partial<Record<Role, SummaryModel>> | Record<Role, { model_id: string; lane: string }>; onClose: () => void; onAccepted: (jobId: string) => void }) {
  const api = useApi();
  const models = useModels();
  const [sel, setSel] = useState<ModelsSelection | null>(null);
  const [validation, setValidation] = useState<ModelsValidated | null>(null);
  const [validating, setValidating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const base = useMemo(() => {
    const out: Partial<Record<Role, { model_id: string; lane: string }>> = {};
    for (const r of ROLES) {
      const m = (current as Partial<Record<Role, { model_id: string; lane: string }>>)[r];
      if (m?.model_id) out[r] = { model_id: m.model_id, lane: m.lane === 'bedrock_runtime' ? 'converse' : m.lane === 'bedrock_mantle' ? 'mantle_chat' : m.lane };
    }
    return out;
  }, [current]);

  useEffect(() => {
    if (models.data && !sel) setSel(selectionFromDefaults(models.data, base));
  }, [models.data, base, sel]);

  useEffect(() => {
    if (!sel) return;
    let cancelled = false;
    setValidating(true);
    const t = window.setTimeout(() => {
      api
        .modelsValidate(sel)
        .then((v) => !cancelled && setValidation(v))
        .catch((e: unknown) => !cancelled && setError(toApiError(e)))
        .finally(() => !cancelled && setValidating(false));
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [api, sel]);

  const changed = sel ? ROLES.filter((r) => sel[r] && sel[r]!.model_id !== base[r]?.model_id) : [];

  const submit = async () => {
    if (!sel) return;
    setBusy(true);
    setError(null);
    try {
      const only: ModelsSelection = {};
      for (const r of changed) only[r] = sel[r];
      const res = await api.packagesRerun(jobId, Object.keys(only).length ? only : undefined);
      onAccepted(res.job_id);
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      title="Re-run with…"
      subtitle={<span className="ellipsis" style={{ display: 'block', maxWidth: 520 }}>{title}</span>}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={submit} disabled={busy || !sel || (validation ? !validation.ok : false)}>
            {busy ? <span className="spin" aria-hidden="true" /> : '↻'} {changed.length ? `Re-run with ${changed.length} change${changed.length === 1 ? '' : 's'}` : 'Re-run with the same models'}
          </button>
        </>
      }
    >
      {models.error ? (
        <ErrorBanner error={models.error} title="Could not load the capability matrix" onRetry={models.reload} />
      ) : !models.data || !sel ? (
        <div className="stack">
          <Skeleton h={34} />
          <Skeleton h={34} />
          <Skeleton h={34} />
        </div>
      ) : (
        <div className="stack">
          <p className="muted small" style={{ margin: 0 }}>
            Only models that passed live probes on a wired lane are offered; role score and price per 1M tokens are shown next to each. The new package is linked to this one as a <b>re-run</b>.
          </p>
          <RoleSelector models={models.data} value={sel} onChange={setSel} validation={validation} disabled={busy} />
          <EstimateLine validation={validation} loading={validating} />
          {changed.length ? <div className="faint">Changing: {changed.join(', ')}</div> : <div className="faint">No role changed yet — the re-run will reuse the original models (useful after a failure).</div>}
          {error ? <ErrorBanner error={error} title="Re-run was not accepted" /> : null}
        </div>
      )}
    </Dialog>
  );
}

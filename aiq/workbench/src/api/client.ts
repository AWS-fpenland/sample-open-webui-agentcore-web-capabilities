// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Real adapter: every operation is one POST to the AgentCore runtime data plane with the user's Cognito
// access token as Bearer (11-architecture.md §7). Responses are streamed NDJSON events; errors arrive as
// {type:"error", data:{error:{code,message}}} and are surfaced as ApiError — never turned into fake success.
import { NdjsonDecoder, decodeAll } from './decode';
import { getRuntimeSessionId } from './session';
import type {
  ArtifactUrl,
  Comparison,
  EvalStarted,
  EvalsResponse,
  ExportFormat,
  ExportResult,
  ExportTheme,
  Health,
  JobAccepted,
  ListParams,
  ModelsResponse,
  ModelsSelection,
  ModelsValidated,
  PackagePatch,
  PackageResponse,
  PackagesPage,
  RuntimeEvent,
  WorkbenchConfig,
} from '../types';

export class ApiError extends Error {
  constructor(
    message: string,
    public code: string = 'error',
    public status?: number,
    public details?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export function toApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e;
  if (e instanceof Error) return new ApiError(e.message, e.name === 'AbortError' ? 'aborted' : 'error');
  return new ApiError(String(e));
}

export interface WorkbenchApi {
  readonly mock: boolean;
  packagesList(params: ListParams): Promise<PackagesPage>;
  packagesGet(jobId: string): Promise<PackageResponse>;
  packagesUpdate(jobId: string, patch: PackagePatch): Promise<PackageResponse>;
  packagesDelete(jobId: string): Promise<{ job_id: string }>;
  packagesCompare(jobId: string, otherJobId: string): Promise<Comparison>;
  packagesRerun(jobId: string, models?: ModelsSelection, dataSources?: string[]): Promise<JobAccepted>;
  exportPackage(jobId: string, format: ExportFormat, theme?: ExportTheme): Promise<ExportResult>;
  artifactUrl(jobId: string, artifactId: string): Promise<ArtifactUrl>;
  models(role?: string): Promise<ModelsResponse>;
  modelsPrefs(models: ModelsSelection): Promise<{ prefs: ModelsSelection }>;
  modelsValidate(models: ModelsSelection): Promise<ModelsValidated>;
  evalStart(models: ModelsSelection, questionIds?: string[], judge?: string): Promise<EvalStarted>;
  evalList(): Promise<EvalsResponse>;
  /** One poll of the job journal (`tail:false`): events with seq > after. */
  events(jobId: string, after: number): Promise<RuntimeEvent[]>;
  health(): Promise<Health>;
}

export function runtimeEndpoint(cfg: WorkbenchConfig): string {
  return `https://bedrock-agentcore.${cfg.region}.amazonaws.com/runtimes/${encodeURIComponent(cfg.runtimeArn)}/invocations?qualifier=DEFAULT`;
}

/** Drop undefined/null fields: the runtime's contracts use extra="forbid" and reject nulls for optional fields. */
function compact<T extends Record<string, unknown>>(o: T): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== '') out[k] = v;
  return out;
}

export function createRealApi(cfg: WorkbenchConfig, getToken: () => string | undefined): WorkbenchApi {
  const endpoint = runtimeEndpoint(cfg);

  async function invoke(body: Record<string, unknown>): Promise<RuntimeEvent[]> {
    const token = getToken();
    if (!token) throw new ApiError('Your session has no access token. Sign in again.', 'unauthenticated', 401);
    let res: Response;
    try {
      res = await fetch(endpoint, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
          Accept: 'text/event-stream, application/json',
          'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': getRuntimeSessionId(),
        },
        body: JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiError(`Could not reach the runtime (${(e as Error).message}). Check network/CORS and that the runtime is deployed.`, 'network');
    }
    if (!res.ok) {
      const text = (await res.text().catch(() => '')).slice(0, 500);
      const code = res.status === 401 || res.status === 403 ? 'unauthenticated' : 'http_error';
      throw new ApiError(`Runtime HTTP ${res.status}${text ? `: ${text}` : ''}`, code, res.status);
    }
    const events: RuntimeEvent[] = [];
    const take = (objs: Record<string, unknown>[]) => {
      for (const o of objs) if (typeof o.type === 'string') events.push(o as unknown as RuntimeEvent);
    };
    if (res.body) {
      const reader = res.body.getReader();
      const dec = new NdjsonDecoder();
      const td = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        take(dec.push(td.decode(value, { stream: true })));
      }
      take(dec.push(td.decode()));
      take(dec.flush());
    } else {
      take(decodeAll(await res.text()));
    }
    return events;
  }

  function raiseIfError(events: RuntimeEvent[]): void {
    const err = events.find((e) => e.type === 'error');
    if (!err) return;
    const d = (err.data ?? {}) as { error?: { code?: string; message?: string }; code?: string; message?: string };
    const inner = d.error ?? d;
    throw new ApiError(inner.message ?? 'The runtime reported an error.', inner.code ?? 'error', undefined, err.data);
  }

  function expect<T>(events: RuntimeEvent[], type: string): T {
    raiseIfError(events);
    const hit = events.find((e) => e.type === type);
    if (!hit) {
      const got = events.map((e) => e.type).join(', ') || 'no events';
      throw new ApiError(`The runtime returned no "${type}" event (got: ${got}).`, 'unexpected_response');
    }
    return hit.data as T;
  }

  return {
    mock: false,
    async packagesList(params) {
      return expect<PackagesPage>(await invoke({ op: 'packages.list', ...compact(params as Record<string, unknown>) }), 'packages');
    },
    async packagesGet(job_id) {
      return expect<PackageResponse>(await invoke({ op: 'packages.get', job_id }), 'package');
    },
    async packagesUpdate(job_id, patch) {
      return expect<PackageResponse>(await invoke({ op: 'packages.update', job_id, ...compact(patch as Record<string, unknown>) }), 'package');
    },
    async packagesDelete(job_id) {
      return expect<{ job_id: string }>(await invoke({ op: 'packages.delete', job_id }), 'package.deleted');
    },
    async packagesCompare(job_id, other_job_id) {
      return expect<Comparison>(await invoke({ op: 'packages.compare', job_id, other_job_id }), 'comparison');
    },
    async packagesRerun(job_id, models, data_sources) {
      const events = await invoke({ op: 'packages.rerun', job_id, ...compact({ models, data_sources }) });
      raiseIfError(events);
      const ev = events.find((e) => e.type === 'job.accepted');
      if (!ev) throw new ApiError('The runtime did not accept the re-run (no job.accepted event).', 'unexpected_response');
      const d = (ev.data ?? {}) as { job_id?: string; parent_job_id?: string | null; relation?: string };
      const newId = ev.job_id ?? d.job_id;
      if (!newId) throw new ApiError('job.accepted carried no job_id.', 'unexpected_response');
      return { job_id: newId, parent_job_id: d.parent_job_id ?? job_id, relation: d.relation ?? 'rerun' };
    },
    async exportPackage(job_id, format, theme) {
      return expect<ExportResult>(await invoke({ op: 'export', job_id, format, ...compact({ theme }) }), 'export');
    },
    async artifactUrl(job_id, artifact_id) {
      return expect<ArtifactUrl>(await invoke({ op: 'artifact.url', job_id, artifact_id }), 'artifact.url');
    },
    async models(role) {
      return expect<ModelsResponse>(await invoke({ op: 'models', ...compact({ role }) }), 'models');
    },
    async modelsPrefs(models) {
      return expect<{ prefs: ModelsSelection }>(await invoke({ op: 'models.prefs', models }), 'models.prefs');
    },
    async modelsValidate(models) {
      return expect<ModelsValidated>(await invoke({ op: 'models.validate', models }), 'models.validated');
    },
    async evalStart(models, question_ids, judge) {
      return expect<EvalStarted>(await invoke({ op: 'eval', models, ...compact({ question_ids, judge }) }), 'eval.started');
    },
    async evalList() {
      return expect<EvalsResponse>(await invoke({ op: 'eval.list' }), 'evals');
    },
    async events(job_id, after) {
      const events = await invoke({ op: 'events', job_id, after, tail: false });
      // A lone error event means the poll itself failed; error events *inside* a journal are terminal job events.
      if (events.length === 1 && events[0].type === 'error' && events[0].seq === undefined) raiseIfError(events);
      return events;
    },
    async health() {
      return expect<Health>(await invoke({ op: 'health' }), 'health');
    },
  };
}

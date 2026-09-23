// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Wire types for the runtime operations contract (11-architecture.md §10) and the
// Research Package manifest (research-tracks/phase3-architecture/package-schema.json).

export type Role = 'router' | 'clarifier' | 'shallow' | 'planner' | 'researcher' | 'writer';
export const ROLES: Role[] = ['router', 'clarifier', 'shallow', 'planner', 'researcher', 'writer'];
export type LabRole = Role | 'chart';
export const LAB_ROLES: LabRole[] = [...ROLES, 'chart'];
export const ROLE_LABEL: Record<LabRole, string> = {
  router: 'Router',
  clarifier: 'Clarifier',
  shallow: 'Quick answer',
  planner: 'Planner',
  researcher: 'Researchers',
  writer: 'Writer',
  chart: 'Chart',
};

export type JobStatus = 'queued' | 'clarifying' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
export const TERMINAL_STATUSES: JobStatus[] = ['completed', 'failed', 'cancelled'];
export const isTerminal = (s: string | undefined | null): boolean => !!s && (TERMINAL_STATUSES as string[]).includes(s);

export type Mode = 'auto' | 'shallow' | 'deep' | 'deep_clarify';
export const MODE_LABEL: Record<Mode, string> = { auto: 'Auto', shallow: 'Quick', deep: 'Deep', deep_clarify: 'Deep + clarify' };

export type ExportFormat = 'md' | 'html' | 'pdf' | 'docx' | 'pptx' | 'json' | 'csv' | 'bibtex' | 'ris' | 'csl' | 'zip';
export type ExportTheme = 'dark' | 'light' | 'print';

/** Deploy-time /config.json. */
export interface WorkbenchConfig {
  region: string;
  userPoolId: string;
  clientId: string;
  cognitoDomain: string;
  runtimeArn: string;
  owuiUrl: string;
  workbenchUrl: string;
  connectedApps?: boolean;
}

/** One line of the runtime's NDJSON response. */
export interface RuntimeEvent {
  type: string;
  seq?: number;
  job_id?: string;
  data: Record<string, unknown> | undefined;
}

// ---- packages.list -------------------------------------------------------
export interface SummaryModel {
  model_id: string;
  human_name: string | null;
  lane: string;
}
export interface PackageSummary {
  job_id: string;
  title: string | null;
  question: string;
  mode: Mode;
  depth: 'shallow' | 'deep' | 'meta' | null;
  status: JobStatus;
  created_at: string;
  completed_at: string | null;
  models: Record<Role, SummaryModel>;
  cost_usd: number;
  counts: { sources: number; citations_verified: number; citations_unverified: number; artifacts: number; exports: number };
  tags: string[];
  pinned: boolean;
  lineage: { parent_package_id: string | null; root_package_id: string; relation: LineageRelation };
  progress?: { phase: string; pct: number };
  error?: string | null;
}
export interface PackagesPage {
  items: PackageSummary[];
  next_cursor: string | null;
}
export interface ListParams {
  limit?: number;
  cursor?: string;
  status?: JobStatus;
  mode?: Mode;
  tag?: string;
  q?: string;
  pinned?: boolean;
}

// ---- manifest (aiq-agentcore/package/v1) ----------------------------------
export type LineageRelation = 'root' | 'rerun' | 'followup' | 'eval' | 'clarified' | 'edit' | 'ask';
export interface ModelRole {
  model_id: string;
  human_name?: string | null;
  provider?: string | null;
  lane: string;
  region?: string | null;
  max_tokens?: number | null;
  reasoning_effort?: string | null;
  nat_llm_name?: string | null;
}
export interface CostLine {
  component: string;
  unit: string;
  quantity: number;
  unit_price_usd: number;
  usd: number;
  model_id?: string | null;
}
export interface Source {
  source_id: string;
  url?: string | null;
  title?: string | null;
  kind: 'web_search' | 'web_page' | 'document' | 'news' | 'prediction_market' | 'package';
  retrieved_at: string;
  tool: string;
  snippet?: string | null;
  document_key?: string | null;
  content_sha256?: string | null;
  cited_by?: string[];
}
export interface Citation {
  marker: string;
  source_id?: string | null;
  url?: string | null;
  verified: boolean;
  match_level: 'exact' | 'normalized' | 'host_path' | 'host' | 'unmatched';
  reason?: string | null;
}
export interface Artifact {
  artifact_id: string;
  job_id: string;
  kind: 'image' | 'table' | 'dataset' | 'notebook' | 'document' | 'text' | 'archive' | 'other';
  mime_type: string;
  filename: string;
  sandbox_path: string;
  storage_key: string;
  storage_uri?: string | null;
  sha256: string;
  size_bytes: number;
  title?: string | null;
  caption?: string | null;
  inline: boolean;
  workflow?: string | null;
  source_tool_call_id?: string | null;
  provenance?: Record<string, unknown>;
  created_at: string;
  capture_phase: 'checkpoint' | 'final';
  status: 'pending' | 'available' | 'rejected' | 'deleted';
  referenced_in_report?: boolean;
}
export interface ExportRecord {
  format: string;
  key: string;
  sha256: string;
  size_bytes: number;
  created_at: string;
  derived: boolean;
  generator?: string | null;
  theme?: string | null;
}
export interface EvalResult {
  eval_id: string;
  run_at: string;
  question_set?: string | null;
  judge: ModelRole | null;
  metrics: Record<string, number | string | boolean | null>;
  rationale_key?: string | null;
  cost_usd?: number | null;
}
export interface Manifest {
  schema_version: 'aiq-agentcore/package/v1';
  package_id: string;
  job_id: string;
  tenant_key: string;
  title: string | null;
  question: string;
  clarification?: { q: string; a: string }[];
  mode: Mode;
  depth: 'shallow' | 'deep' | 'meta' | null;
  data_sources?: string[];
  status: JobStatus;
  error?: string | null;
  timing: {
    created_at: string;
    started_at?: string | null;
    completed_at?: string | null;
    updated_at: string;
    duration_seconds?: number | null;
    clarification_turns?: number;
  };
  runtime: {
    adapter_version: string;
    source_commit?: string | null;
    upstream_ref?: string | null;
    runtime_version?: string | null;
    runtime_session_id?: string | null;
    conversation_id?: string | null;
    region?: string | null;
  };
  models: {
    selection: 'deploy_default' | 'session_override' | 'rerun_override' | 'backfill_inferred';
    roles: Record<Role, ModelRole> & { orchestrator?: ModelRole; source_router?: ModelRole };
  };
  usage: {
    input_tokens: number;
    output_tokens: number;
    llm_calls?: number;
    truncated_outputs?: number;
    searches: number;
    pages: number;
    retrievals: number;
    sandbox_seconds?: number;
    browser_sessions?: number;
    guardrail_text_units?: number;
    per_model?: { model_id: string; input_tokens: number; output_tokens: number; calls: number }[];
  };
  cost: {
    currency: 'USD';
    total_usd: number;
    lines: CostLine[];
    price_source: { kind: 'aws_price_list_api' | 'pricing_page' | 'estimate'; retrieved_at: string; offer_codes?: string[]; region?: string };
    confidence?: 'billed' | 'computed' | 'estimated';
  };
  report: {
    present: boolean;
    key?: string | null;
    sha256?: string | null;
    size_bytes?: number | null;
    word_count?: number | null;
    format?: 'text/markdown';
    headings?: { level: number; text: string }[];
    summary?: string | null;
    guardrail_action?: string | null;
  };
  citations: { ledger_key?: string | null; verified: number; unverified: number; items: Citation[] };
  sources: Source[];
  artifacts: Artifact[];
  lineage: {
    parent_package_id?: string | null;
    relation: LineageRelation;
    root_package_id: string;
    result_kind?: string | null;
    changes?: { models_changed?: string[]; data_sources_changed?: boolean; question_changed?: boolean };
  };
  organization: { tags: string[]; pinned: boolean; project?: string | null; notes?: string | null };
  exports: ExportRecord[];
  evals?: EvalResult[];
  knowledge?: Record<string, unknown>;
  retention?: Record<string, unknown>;
}
export interface PackageResponse {
  manifest: Manifest;
  report_md: string;
  events_tail?: RuntimeEvent[];
}
export interface PackagePatch {
  tags?: string[];
  pinned?: boolean;
  title?: string;
  notes?: string;
}

// ---- compare / rerun / export / artifacts ---------------------------------
export interface Comparison {
  a: PackageSummary;
  b: PackageSummary;
  sections: { title: string; in_a: boolean; in_b: boolean }[];
  sources: { shared: Source[]; only_a: Source[]; only_b: Source[] };
  claims: { a: string | null; b: string | null; markers_a: string[]; markers_b: string[] }[];
  run: { cost_a: number; cost_b: number; seconds_a: number; seconds_b: number; citations_a: number; citations_b: number };
}
export interface JobAccepted {
  job_id: string;
  parent_job_id?: string | null;
  relation?: string;
}
export interface ExportResult {
  format: ExportFormat;
  filename: string;
  size: number;
  sha256: string;
  url: string;
  expires_in: number;
  cached: boolean;
  backend?: string;
}
export interface ArtifactUrl {
  url: string;
  expires_in: number;
}

// ---- models (Model Lab) ----------------------------------------------------
export type Capability = 'plain' | 'system' | 'stream' | 'tools' | 'json' | 'long' | 'reasoning';
export const CAPABILITIES: Capability[] = ['plain', 'system', 'stream', 'tools', 'json', 'long', 'reasoning'];
export interface CapabilityResult {
  ok: boolean;
  error_code: string | null;
  ms: number | null;
}
export interface MatrixEntry {
  id: string;
  lane: 'converse' | 'mantle_chat' | 'mantle_responses' | 'mantle_messages' | string;
  base_model_id: string;
  name: string;
  provider: string;
  family?: string | null;
  region?: string | null;
  routing?: string | null;
  offered: boolean;
  exclusion_reasons: string[];
  raw_error?: string | null;
  price: { input_per_1m: number | null; output_per_1m: number | null; source: string };
  capabilities: Record<Capability, CapabilityResult>;
  latency: { plain_ms: number | null; ttft_ms: number | null; long_ms: number | null };
  roles: Record<LabRole, { score: number; verdict: string }>;
  roles_selectable: Record<LabRole, boolean>;
  attempts: { at: string | null; plain_ok: boolean; error_code: string | null }[];
}
export interface RoleModelRef {
  model_id: string;
  lane: string;
}
export type ModelsSelection = Partial<Record<Role, RoleModelRef>>;
export interface ModelsResponse {
  matrix: {
    generated_at: string;
    probed_as: string;
    digest: string;
    summary: Record<string, unknown> & { entries?: number; offered?: number; excluded?: number; sweep_cost_usd?: number; probed_at?: string };
  };
  entries: MatrixEntry[];
  defaults: Partial<Record<Role, string>>;
  prefs: ModelsSelection | null;
}
export interface ModelsValidated {
  ok: boolean;
  problems: { role: string; model_id: string; reason: string }[];
  estimate_usd: { low: number; high: number };
}

// ---- evals -----------------------------------------------------------------
export interface EvalRow {
  question_id: string;
  job_id: string;
  status: JobStatus;
  cost_usd: number | null;
  seconds: number | null;
  citations_verified: number | null;
  citations_unverified: number | null;
  judge_score?: number | null;
}
export interface EvalRun {
  eval_id: string;
  created_at: string;
  models: ModelsSelection;
  status: JobStatus;
  rows: EvalRow[];
  judge?: RoleModelRef | null;
}
export interface EvalsResponse {
  items: EvalRun[];
}
export interface EvalStarted {
  eval_id: string;
  job_ids: string[];
}

export interface Health {
  version: string;
  source_commit?: string;
  [k: string]: unknown;
}

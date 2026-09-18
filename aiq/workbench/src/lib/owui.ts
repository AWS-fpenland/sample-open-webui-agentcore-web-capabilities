// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Deep links back into Open WebUI (the conversation is where intent is formed; 09 §6).
import type { ModelsSelection, WorkbenchConfig } from '../types';

const base = (cfg: WorkbenchConfig) => cfg.owuiUrl.replace(/\/+$/, '');

export function owuiChatUrl(cfg: WorkbenchConfig, conversationId: string): string {
  return `${base(cfg)}/c/${encodeURIComponent(conversationId)}`;
}

export function owuiNewResearchUrl(cfg: WorkbenchConfig, question: string, model = 'aiq_agentcore.deep', submit = false): string {
  return `${base(cfg)}/?models=${encodeURIComponent(model)}&q=${encodeURIComponent(question)}&submit=${submit ? 'true' : 'false'}`;
}

export function owuiHomeUrl(cfg: WorkbenchConfig, model = 'aiq_agentcore.deep'): string {
  return `${base(cfg)}/?models=${encodeURIComponent(model)}`;
}

/** The chat command that applies a per-session model selection: `/model writer=<id> planner=<id> …`. */
export function modelCommand(models: ModelsSelection): string {
  const parts = Object.entries(models)
    .filter(([, v]) => v?.model_id)
    .map(([role, v]) => `${role}=${v!.model_id}`);
  return parts.length ? `/model ${parts.join(' ')}` : '/models';
}

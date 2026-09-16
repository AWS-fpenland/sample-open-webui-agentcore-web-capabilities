#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Standalone, opt-in CDK app for "AI-Q on AgentCore". It is deliberately not
// wired into bin/app.ts so the base Open WebUI stacks are never touched.
//
//   npx cdk -a 'npx ts-node --prefer-ts-exts bin/aiq.ts' deploy \
//     -c aiq=on -c runId=<run-id> -c account=<12 digits> -c region=us-east-1 \
//     -c userPoolId=<pool> -c allowedClients=<client-id>[,<client-id>] \
//     [-c imageTag=<tag> | -c imageDigest=sha256:...]
import * as cdk from 'aws-cdk-lib';
import { AiqStack } from '../lib/aiq-stack';

const app = new cdk.App();
if (app.node.tryGetContext('aiq') !== 'on') {
  throw new Error('AI-Q on AgentCore is opt-in: pass -c aiq=on');
}
function required(key: string): string {
  const value = app.node.tryGetContext(key);
  if (typeof value !== 'string' || !value) {
    throw new Error(`Missing required context: -c ${key}=...`);
  }
  return value;
}
const runId = required('runId');
const account = required('account');
const region = required('region');
const userPoolId = required('userPoolId');
const allowedClientIds = required('allowedClients').split(',').map((s) => s.trim()).filter(Boolean);
const imageTag = app.node.tryGetContext('imageTag') as string | undefined;
const imageDigest = app.node.tryGetContext('imageDigest') as string | undefined;
const model = (key: string, fallback: string): string => (app.node.tryGetContext(key) as string | undefined) ?? fallback;

new AiqStack(app, `aiq-${runId}`, {
  env: { account, region },
  runId,
  userPoolId,
  allowedClientIds,
  imageTag,
  imageDigest,
  retentionDays: Number(app.node.tryGetContext('retentionDays') ?? 30),
  guardrail: app.node.tryGetContext('guardrail') !== 'off',
  enforceCitations: app.node.tryGetContext('enforceCitations') !== 'false',
  fetchMaxPages: Number(app.node.tryGetContext('fetchMaxPages') ?? 12),
  maxTokensDeep: Number(app.node.tryGetContext('maxTokensDeep') ?? 16384),
  maxTokensWriter: Number(app.node.tryGetContext('maxTokensWriter') ?? 16384),
  models: {
    // Verified available in the target account on 2026-09-15 (Converse OK).
    router: model('modelRouter', 'global.anthropic.claude-haiku-4-5-20251001-v1:0'),
    shallow: model('modelShallow', 'global.anthropic.claude-sonnet-5'),
    planner: model('modelPlanner', 'global.anthropic.claude-sonnet-5'),
    researcher: model('modelResearcher', 'global.anthropic.claude-sonnet-5'),
    writer: model('modelWriter', 'global.anthropic.claude-sonnet-5'),
    embedding: model('modelEmbedding', 'amazon.titan-embed-text-v2:0'),
  },
  runtimeEnvironment: {
    AIQ_ENGINE: (app.node.tryGetContext('engine') as string | undefined) ?? 'aiq',
    AIQ_LOG_LEVEL: (app.node.tryGetContext('logLevel') as string | undefined) ?? 'INFO',
    // Sandboxed skills on AgentCore Code Interpreter (default on); -c sandbox=off removes skills/sandbox from the workflow.
    AIQ_SANDBOX: (app.node.tryGetContext('sandbox') as string | undefined) ?? 'on',
    AIQ_AGENTCORE_CI_IDENTIFIER: (app.node.tryGetContext('codeInterpreter') as string | undefined) ?? 'aws.codeinterpreter.v1',
    AIQ_AGENTCORE_CI_NETWORK_MODE: (app.node.tryGetContext('codeInterpreterNetwork') as string | undefined) ?? 'SANDBOX',
    // Optional per-role Bedrock reasoning control (e.g. Nemotron reasoning_effort none|low|medium|high); unset = provider default.
    ...Object.fromEntries(
      (['router', 'clarifier', 'shallow', 'planner', 'researcher', 'writer'] as const)
        .map((role) => [`AIQ_REASONING_EFFORT_${role.toUpperCase()}`, app.node.tryGetContext(`reasoning${role[0].toUpperCase()}${role.slice(1)}`) as string | undefined])
        .filter(([, v]) => typeof v === 'string' && v.length > 0),
    ),
  },
});
app.synth();

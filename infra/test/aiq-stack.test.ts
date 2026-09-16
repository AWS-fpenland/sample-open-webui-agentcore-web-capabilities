// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AiqStack } from '../lib/aiq-stack';

const base = {
  env: { account: '111111111111', region: 'us-east-1' },
  runId: 'test-run',
  userPoolId: 'us-east-1_TESTPOOL',
  allowedClientIds: ['client-a'],
  models: {
    router: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
    shallow: 'global.anthropic.claude-sonnet-5',
    planner: 'global.anthropic.claude-sonnet-5',
    researcher: 'global.anthropic.claude-sonnet-5',
    writer: 'global.anthropic.claude-sonnet-5',
    embedding: 'amazon.titan-embed-text-v2:0',
  },
};

function synth(extra: Partial<ConstructorParameters<typeof AiqStack>[2]> = {}): Template {
  const app = new cdk.App();
  return Template.fromStack(new AiqStack(app, 'aiq-test-run', { ...base, ...extra }));
}

describe('AiqStack', () => {
  test('phase 1 has no runtime and all storage is private', () => {
    const t = synth();
    t.resourceCountIs('AWS::BedrockAgentCore::Runtime', 0);
    t.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
      BucketEncryption: Match.anyValue(),
    });
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'aiq-test-run-jobs', TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true } });
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'aiq-test-run-events', TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true } });
    t.hasResourceProperties('AWS::BedrockAgentCore::Gateway', { AuthorizerType: 'AWS_IAM', ProtocolType: 'MCP' });
    t.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
      TargetConfiguration: { Mcp: { Connector: { Source: { ConnectorId: 'web-search' }, Enabled: ['WebSearch'] } } },
      CredentialProviderConfigurations: [{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }],
    });
    t.hasResourceProperties('AWS::Bedrock::KnowledgeBase', { StorageConfiguration: { Type: 'S3_VECTORS' } });
    t.hasResourceProperties('AWS::S3Vectors::Index', { Dimension: 1024, DistanceMetric: 'cosine',
      MetadataConfiguration: { NonFilterableMetadataKeys: ['AMAZON_BEDROCK_TEXT'] } });
    t.hasResourceProperties('AWS::Bedrock::DataSource', { DataDeletionPolicy: 'DELETE',
      DataSourceConfiguration: { Type: 'S3', S3Configuration: { InclusionPrefixes: ['documents/'] } } });
    t.hasResourceProperties('AWS::CodeBuild::Project', { Environment: Match.objectLike({ PrivilegedMode: true, Type: 'ARM_CONTAINER' }) });
  });

  test('guardrail, reaper and sandbox permissions are present and scoped', () => {
    const t = synth({ imageTag: 'abc123', guardrailMode: 'enforce', guardrailFilters: { PROMPT_ATTACK: 'HIGH' } });
    t.hasResourceProperties('AWS::Bedrock::Guardrail', { Name: 'aiq-test-run-guardrail',
      ContentPolicyConfig: { FiltersConfig: Match.arrayWith([Match.objectLike({ Type: 'PROMPT_ATTACK', InputStrength: 'HIGH', OutputStrength: 'NONE' })]) } });
    t.resourceCountIs('AWS::Bedrock::GuardrailVersion', 1);
    t.hasResourceProperties('AWS::Lambda::Function', { FunctionName: 'aiq-test-run-reaper', Runtime: 'python3.12',
      Environment: { Variables: Match.objectLike({ STALE_AFTER_SECONDS: '600' }) } });
    t.hasResourceProperties('AWS::Events::Rule', { ScheduleExpression: 'rate(5 minutes)' });
    t.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
      EnvironmentVariables: Match.objectLike({ AIQ_ENFORCE_CITATIONS: 'true', AIQ_FETCH_MAX_PAGES: '12', AIQ_GUARDRAIL_ID: Match.anyValue() }),
    });
    const policies = t.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement as any[]);
    const sandbox = statements.find((s) => s.Sid === 'AgentCoreBuiltinSandboxTools');
    expect(sandbox).toBeDefined();
    expect(sandbox.Resource).toEqual(expect.arrayContaining([expect.stringContaining(':aws:browser/*')]));
    expect(sandbox.Resource).not.toContain('*');
  });

  test('guardrail is off by default: no resource, nothing in the request path, mode exported to the runtime', () => {
    const t = synth({ imageTag: 'abc123' });
    t.resourceCountIs('AWS::Bedrock::Guardrail', 0);
    t.resourceCountIs('AWS::Bedrock::GuardrailVersion', 0);
    t.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
      EnvironmentVariables: Match.objectLike({ AIQ_GUARDRAIL_MODE: 'off' }),
    });
    const env = Object.values(t.findResources('AWS::BedrockAgentCore::Runtime'))[0].Properties.EnvironmentVariables;
    expect(env.AIQ_GUARDRAIL_ID).toBeUndefined();
    expect(JSON.stringify(t.findResources('AWS::IAM::Policy'))).not.toContain('bedrock:ApplyGuardrail');
  });

  test('audit mode creates the guardrail with tunable defaults (no prompt-attack filter) and never blocks', () => {
    const t = synth({ imageTag: 'abc123', guardrailMode: 'audit' });
    t.resourceCountIs('AWS::Bedrock::Guardrail', 1);
    const filters = Object.values(t.findResources('AWS::Bedrock::Guardrail'))[0].Properties.ContentPolicyConfig.FiltersConfig;
    expect(filters.map((f: { Type: string }) => f.Type).sort()).toEqual(['HATE', 'INSULTS', 'MISCONDUCT', 'SEXUAL', 'VIOLENCE']);
    expect(filters.find((f: { Type: string }) => f.Type === 'HATE')).toEqual({ Type: 'HATE', InputStrength: 'MEDIUM', OutputStrength: 'MEDIUM' });
    t.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
      EnvironmentVariables: Match.objectLike({ AIQ_GUARDRAIL_MODE: 'audit', AIQ_GUARDRAIL_ID: Match.anyValue(), AIQ_GUARDRAIL_VERSION: Match.anyValue() }),
    });
    t.hasOutput('GuardrailMode', { Value: 'audit' });
  });

  test('filter strengths are tunable and validated', () => {
    const t = synth({ imageTag: 'abc123', guardrailMode: 'enforce', guardrailFilters: { PROMPT_ATTACK: 'LOW', HATE: 'NONE', VIOLENCE: 'HIGH' } });
    const filters = Object.values(t.findResources('AWS::Bedrock::Guardrail'))[0].Properties.ContentPolicyConfig.FiltersConfig;
    expect(filters).toEqual(expect.arrayContaining([
      { Type: 'PROMPT_ATTACK', InputStrength: 'LOW', OutputStrength: 'NONE' },
      { Type: 'VIOLENCE', InputStrength: 'HIGH', OutputStrength: 'HIGH' },
    ]));
    expect(filters.find((f: { Type: string }) => f.Type === 'HATE')).toBeUndefined();
    expect(() => synth({ imageTag: 'abc123', guardrailMode: 'enforce', guardrailFilters: { HATE: 'EXTREME' as never } })).toThrow(/HATE strength/);
    expect(() => synth({ imageTag: 'abc123', guardrailMode: 'audit', guardrailFilters: {
      PROMPT_ATTACK: 'NONE', HATE: 'NONE', INSULTS: 'NONE', SEXUAL: 'NONE', VIOLENCE: 'NONE', MISCONDUCT: 'NONE' } })).toThrow(/at least one filter/);
    expect(() => synth({ imageTag: 'abc123', guardrailMode: 'loud' as never })).toThrow(/guardrailMode must be one of/);
    // legacy boolean still works
    synth({ imageTag: 'abc123', guardrail: true }).resourceCountIs('AWS::Bedrock::Guardrail', 1);
  });

  test('phase 2 adds a JWT-authorized HTTP runtime that forwards Authorization and pins the digest', () => {
    const t = synth({ imageTag: 'abc123', imageDigest: 'sha256:' + 'f'.repeat(64) });
    t.resourceCountIs('AWS::BedrockAgentCore::Runtime', 1);
    t.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
      AgentRuntimeName: 'aiq_test_run',
      ProtocolConfiguration: 'HTTP',
      NetworkConfiguration: { NetworkMode: 'PUBLIC' },
      RequestHeaderConfiguration: { RequestHeaderAllowlist: ['Authorization'] },
      LifecycleConfiguration: { IdleRuntimeSessionTimeout: 1800, MaxLifetime: 28800 },
      AuthorizerConfiguration: { CustomJWTAuthorizer: {
        DiscoveryUrl: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL/.well-known/openid-configuration',
        AllowedClients: ['client-a'],
      } },
      AgentRuntimeArtifact: { ContainerConfiguration: { ContainerUri: Match.objectLike({ 'Fn::Join': Match.anyValue() }) } },
      EnvironmentVariables: Match.objectLike({ AIQ_RUN_ID: 'test-run', AIQ_JWT_ALLOWED_CLIENTS: 'client-a', AIQ_ENGINE: Match.absent() }),
    });
  });

  test('runtime role is scoped: no wildcard bedrock-agentcore or dynamodb resources', () => {
    const t = synth({ imageTag: 'abc123' });
    const policies = t.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement as any[]);
    for (const s of statements) {
      const actions = ([] as string[]).concat(s.Action);
      if (actions.some((a) => a.startsWith('dynamodb:') || a === 'bedrock-agentcore:InvokeGateway' || a === 'bedrock:Retrieve')) {
        expect(s.Resource).not.toEqual('*');
      }
    }
  });

  test('rejects an invalid run id and an empty client list', () => {
    expect(() => synth({ runId: 'Bad_Run' })).toThrow(/runId/);
    expect(() => synth({ allowedClientIds: [] })).toThrow(/client/);
  });
});

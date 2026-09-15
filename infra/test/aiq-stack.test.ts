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

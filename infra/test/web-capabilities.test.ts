import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { execFileSync, spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { WebCapabilitiesStack, WEB_SEARCH_REGIONS } from '../lib/web-capabilities-stack';

const infraDir = path.join(__dirname, '..');
const sourceDir = path.join(infraDir, '..', 'gateway', 'web-search-provisioner');
let assetPath: string;

beforeAll(() => {
  assetPath = fs.mkdtempSync(path.join(os.tmpdir(), 'web-search-contract-asset-'));
  for (const filename of ['index.py', 'requirements.txt']) {
    fs.copyFileSync(path.join(sourceDir, filename), path.join(assetPath, filename));
  }
  for (const packageName of ['boto3', 'botocore', 's3transfer', 'jmespath', 'dateutil', 'urllib3']) {
    fs.mkdirSync(path.join(assetPath, packageName));
    fs.writeFileSync(path.join(assetPath, packageName, '__init__.py'), '__version__ = "1.43.94"\n');
  }
  for (const packageName of ['boto3', 'botocore']) {
    fs.mkdirSync(path.join(assetPath, `${packageName}-1.43.94.dist-info`));
    fs.writeFileSync(path.join(assetPath, `${packageName}-1.43.94.dist-info`, 'METADATA'), '\nVersion: 1.43.94\n');
  }
  fs.mkdirSync(path.join(assetPath, 'botocore', 'data', 'bedrock-agentcore-control'), { recursive: true });
  fs.writeFileSync(path.join(assetPath, 'six.py'), '');
});

afterAll(() => fs.rmSync(assetPath, { recursive: true, force: true }));

function stack(env: cdk.Environment = { account: '123456789012', region: 'us-east-1' }, bundle = assetPath): WebCapabilitiesStack {
  return new WebCapabilitiesStack(new cdk.App(), 'OpenWebUI-WebCapabilities', {
    env, providerAssetPath: bundle,
  });
}

test('search-only MCP IAM gateway, native target, dependent version pin, and public references', () => {
  const searchStack = stack();
  const template = Template.fromStack(searchStack);
  template.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
  template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
    Name: 'open-webui-web-search', AuthorizerType: 'AWS_IAM', ProtocolType: 'MCP',
    ProtocolConfiguration: { Mcp: { SupportedVersions: ['2025-03-26'] } },
    ExceptionLevel: Match.absent(), AuthorizerConfiguration: Match.absent(),
    InterceptorConfigurations: Match.absent(),
  });
  template.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
    Name: 'web-search-tool',
    GatewayIdentifier: { 'Fn::GetAtt': ['SearchGateway', 'GatewayIdentifier'] },
    TargetConfiguration: { Mcp: { Connector: {
      Source: { ConnectorId: 'web-search' }, Enabled: ['WebSearch'],
      Configurations: [{ Name: 'WebSearch', ParameterValues: {} }],
    } } },
    CredentialProviderConfigurations: [{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }],
  });
  template.resourceCountIs('AWS::BedrockAgentCore::GatewayTarget', 1);
  template.hasResourceProperties('Custom::WebSearchVersionPin', {
    TargetName: 'web-search-tool', ConnectorId: 'web-search', ConnectorVersion: '1.2.0',
    TargetId: { 'Fn::GetAtt': ['SearchTarget', 'TargetId'] },
  });
  template.hasOutput('ToolName', { Value: 'web-search-tool___WebSearch' });
  template.hasOutput('ConnectorVersion', { Value: '1.2.0' });
  template.hasOutput('GatewayId', { Value: searchStack.resolve(searchStack.gatewayId) });
  template.hasOutput('GatewayUrl', { Value: searchStack.resolve(searchStack.gatewayUrl) });
  expect(searchStack.resolve(searchStack.gatewayArn)).toEqual({ 'Fn::GetAtt': ['SearchGateway', 'GatewayArn'] });
  const resources = Object.values(template.toJSON().Resources) as any[];
  expect(resources.every(resource => !/Browser|ECS|EC2|Cognito|DynamoDB|CloudFront/.test(resource.Type))).toBe(true);
  expect(JSON.stringify(template.toJSON())).not.toMatch(/Fn::ImportValue|bedrock-mantle|CUSTOM_JWT/);
});

test('gateway role grants only service-owned search and its own gateway without a cycle', () => {
  const template = Template.fromStack(stack()).toJSON();
  const resources = template.Resources as Record<string, any>;
  const gatewayId = Object.keys(resources).find(key => resources[key].Type === 'AWS::BedrockAgentCore::Gateway')!;
  const roleId = resources[gatewayId].Properties.RoleArn['Fn::GetAtt'][0];
  const role = resources[roleId];
  expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  expect(role.Properties.Policies).toHaveLength(1);
  expect(role.Properties.Policies[0].PolicyDocument.Statement).toEqual([{
    Effect: 'Allow', Action: 'bedrock-agentcore:InvokeWebSearch',
    Resource: 'arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1',
  }]);
  expect(role.Properties.AssumeRolePolicyDocument.Statement[0].Condition).toEqual({
    StringEquals: { 'aws:SourceAccount': '123456789012' },
    ArnLike: { 'aws:SourceArn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/*' },
  });
  const invokeId = Object.keys(resources).find(key => resources[key].Type === 'AWS::IAM::Policy'
    && resources[key].Properties.PolicyDocument.Statement.some((statement: any) => statement.Action === 'bedrock-agentcore:InvokeGateway'))!;
  expect(resources[invokeId].Properties.Roles).toEqual([{ Ref: roleId }]);
  expect(resources[invokeId].Properties.PolicyDocument.Statement).toEqual([{
    Effect: 'Allow', Action: 'bedrock-agentcore:InvokeGateway', Resource: { 'Fn::GetAtt': [gatewayId, 'GatewayArn'] },
  }]);
  expect(JSON.stringify(role)).not.toContain(gatewayId);
  expect(resources[gatewayId].DependsOn ?? []).not.toContain(invokeId);
  const target = Object.values(resources).find(resource => resource.Type === 'AWS::BedrockAgentCore::GatewayTarget');
  expect(target.DependsOn).toContain(invokeId);
});

test('native CFN owns failed-pin rollback cleanup even when custom Create receives no Delete', () => {
  const resources = Template.fromStack(stack()).toJSON().Resources as Record<string, any>;
  const target = resources.SearchTarget;
  expect(target.Type).toBe('AWS::BedrockAgentCore::GatewayTarget');
  expect(target.DeletionPolicy).toBe('Delete');
  expect(target.UpdateReplacePolicy).toBe('Delete');
  const pin = resources.SearchVersionPin;
  expect(pin.Type).toBe('Custom::WebSearchVersionPin');
  expect(pin.DependsOn).toContain('SearchTarget');
  expect(pin.Properties.TargetId).toEqual({ 'Fn::GetAtt': ['SearchTarget', 'TargetId'] });
  expect(target.DependsOn).not.toContain('SearchVersionPin');
  expect(JSON.stringify(target)).not.toContain('ServiceToken');
  expect(JSON.stringify(target.Properties.TargetConfiguration)).not.toContain('Version');
});

test('provider target permissions are gateway-scoped and framework payload logging is suppressed', () => {
  const template = Template.fromStack(stack());
  template.hasResourceProperties('AWS::Lambda::Function', {
    Handler: 'index.handler', Runtime: 'python3.12', Timeout: 360,
    Code: { S3Bucket: Match.anyValue(), S3Key: Match.anyValue() },
  });
  template.hasResourceProperties('AWS::Lambda::Function', {
    LoggingConfig: Match.objectLike({ ApplicationLogLevel: 'FATAL', LogFormat: 'JSON' }),
  });
  const resources = template.toJSON().Resources as Record<string, any>;
  const statements = Object.values(resources).filter(resource => resource.Type === 'AWS::IAM::Policy')
    .flatMap(resource => resource.Properties.PolicyDocument.Statement);
  const targetStatements = statements.filter(statement => JSON.stringify(statement.Action).includes('GatewayTarget'));
  expect(targetStatements).toHaveLength(1);
  expect(targetStatements[0].Action).toEqual([
    'bedrock-agentcore:GetGatewayTarget', 'bedrock-agentcore:UpdateGatewayTarget',
    'bedrock-agentcore:SynchronizeGatewayTargets',
  ]);
  for (const statement of targetStatements) {
    expect(JSON.stringify(statement.Resource)).toContain('GatewayArn');
    expect(statement.Resource).not.toEqual('*');
    expect(statement.Resource).not.toContain('*');
  }
  expect(JSON.stringify(statements)).not.toMatch(/iam:PassRole|bedrock-agentcore:\*|DeleteGateway|CreateGatewayTarget|ListGatewayTargets/);
});

test.each(WEB_SEARCH_REGIONS)('accepts approved search region %s', region => {
  Template.fromStack(stack({ account: '123456789012', region }));
});

test.each([
  {}, { account: '123456789012' }, { region: 'us-east-1' },
  { account: 'invalid', region: 'us-east-1' },
  { account: '123456789012', region: 'us-west-2' },
  { account: '123456789012', region: 'cn-north-1' },
  { account: cdk.Aws.ACCOUNT_ID, region: 'us-east-1' },
  { account: '123456789012', region: cdk.Aws.REGION },
])('rejects missing, unresolved, or unapproved environment %j', env => {
  expect(() => stack(env)).toThrow(/explicit/);
});

test('requires an external prebuilt asset instead of silently downloading during synth', () => {
  expect(() => stack(undefined, '')).toThrow(/Prebuild/);
  expect(() => stack(undefined, sourceDir)).toThrow(/outside the checkout/);
  const incomplete = fs.mkdtempSync(path.join(os.tmpdir(), 'search-incomplete-'));
  try {
    expect(() => stack(undefined, incomplete)).toThrow(/missing or stale/);
    for (const filename of ['index.py', 'requirements.txt']) {
      fs.copyFileSync(path.join(sourceDir, filename), path.join(incomplete, filename));
    }
    expect(() => stack(undefined, incomplete)).toThrow(/boto3==1.43.94/);
  } finally {
    fs.rmSync(incomplete, { recursive: true, force: true });
  }
});

test('real standalone entrypoint rejects implicit opt-in despite ambient account and region', () => {
  const result = spawnSync(process.execPath, ['-r', 'ts-node/register/transpile-only', 'bin/web-capabilities.ts'], {
    cwd: infraDir, encoding: 'utf8', timeout: 60_000,
    env: { ...process.env, CDK_CONTEXT_JSON: '{}', CDK_DEFAULT_ACCOUNT: '123456789012', CDK_DEFAULT_REGION: 'us-east-1' },
  });
  expect(result.status).not.toBe(0);
  expect(result.stderr).toContain('webCapabilities=on');
}, 90_000);

test('real entrypoint synthesizes exactly one independent stack with no lookups', () => {
  const output = fs.mkdtempSync(path.join(os.tmpdir(), 'web-capabilities-synth-'));
  try {
    execFileSync(process.execPath, ['-r', 'ts-node/register/transpile-only', 'bin/web-capabilities.ts'], {
      cwd: infraDir, timeout: 60_000,
      env: { ...process.env, CDK_OUTDIR: output, CDK_CONTEXT_JSON: JSON.stringify({
        webCapabilities: 'on', account: '123456789012', region: 'us-east-1', providerAssetPath: assetPath,
      }) },
    });
    const manifest = JSON.parse(fs.readFileSync(path.join(output, 'manifest.json'), 'utf8'));
    const stacks = Object.entries(manifest.artifacts).filter(([, artifact]: [string, any]) => artifact.type === 'aws:cloudformation:stack');
    expect(stacks.map(([name]) => name)).toEqual(['OpenWebUI-WebCapabilities']);
    expect(manifest.missing ?? []).toEqual([]);
    expect((stacks[0][1] as any).dependencies.every((dependency: string) => dependency.endsWith('.assets'))).toBe(true);
    expect(fs.existsSync(path.join(output, 'OpenWebUI-WebCapabilities.template.json'))).toBe(true);
  } finally {
    fs.rmSync(output, { recursive: true, force: true });
  }
}, 90_000);

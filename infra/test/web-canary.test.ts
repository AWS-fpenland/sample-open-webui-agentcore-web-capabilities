import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { WebCanaryStack, WebCanaryStackProps } from '../lib/web-canary-stack';

const root = path.resolve(__dirname, '..', '..');
let bundle: string;

beforeAll(() => {
  bundle = fs.mkdtempSync(path.join(os.tmpdir(), 'web-canary-template-'));
  fs.mkdirSync(path.join(bundle, 'web_capabilities'));
  for (const module of ['__init__', 'browser', 'brokered_browser', 'documents', 'gateway', 'http_fetch', 'lambda_handler', 'quota', 'search', 'url_policy']) {
    fs.copyFileSync(path.join(root, 'web_capabilities', `${module}.py`), path.join(bundle, 'web_capabilities', `${module}.py`));
  }
  fs.copyFileSync(path.join(root, 'web_capabilities', 'requirements-lambda.txt'), path.join(bundle, 'requirements-lambda.txt'));
});

afterAll(() => fs.rmSync(bundle, { recursive: true, force: true }));

function make(overrides: Partial<WebCanaryStackProps> = {}) {
  return new WebCanaryStack(new cdk.App(), 'CanaryTest', {
    env: { account: '123456789012', region: 'us-east-1' }, adapterAssetPath: bundle,
    gatewayArn: 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/owned-search-123',
    gatewayUrl: 'https://owned-search-123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp',
    taskRoleArn: 'arn:aws:iam::123456789012:role/VerifiedOpenWebUITaskRole',
    subjects: ['synthetic-user'], availabilityZoneId: 'use1-az1', ...overrides,
  });
}

test('private bounded ARM64 Lambda starts with every feature disabled', () => {
  const template = Template.fromStack(make());
  template.resourceCountIs('AWS::Lambda::Function', 1);
  template.resourceCountIs('AWS::Lambda::Url', 0);
  template.resourceCountIs('AWS::ApiGateway::RestApi', 0);
  const adapter = Object.values(template.findResources('AWS::Lambda::Function'))[0];
  expect(adapter.Properties).toMatchObject({
    Architectures: ['arm64'], Runtime: 'python3.12', Handler: 'web_capabilities.lambda_handler.handler',
    MemorySize: 1024, Timeout: 60, ReservedConcurrentExecutions: 2,
    Environment: { Variables: { AGENTCORE_WEB_SEARCH_ENABLED: 'false', AGENTCORE_WEB_FETCH_ENABLED: 'false',
      AGENTCORE_WEB_BROWSER_ENABLED: 'false', AGENTCORE_WEB_SUBJECTS: '["synthetic-user"]' } },
  });
  expect(adapter.Properties.VpcConfig).toBeUndefined();
  template.hasResourceProperties('AWS::Lambda::Alias', { Name: 'live' });
});

test('HTTP and Browser flags are independent and ordinary extraction can stay enabled', () => {
  const template = Template.fromStack(make({ searchEnabled: true, fetchEnabled: true, browserEnabled: false }));
  const adapter = Object.values(template.findResources('AWS::Lambda::Function'))[0];
  expect(adapter.Properties.Environment.Variables).toMatchObject({
    AGENTCORE_WEB_SEARCH_ENABLED: 'true', AGENTCORE_WEB_FETCH_ENABLED: 'true', AGENTCORE_WEB_BROWSER_ENABLED: 'false',
  });
});

test('only a qualified invocation policy modifies the existing task role', () => {
  const template = Template.fromStack(make());
  const policies = Object.values(template.findResources('AWS::IAM::Policy'));
  const existing = policies.filter(policy => policy.Properties.Roles.includes('VerifiedOpenWebUITaskRole'));
  expect(existing).toHaveLength(1);
  const statements = existing[0].Properties.PolicyDocument.Statement;
  expect(statements).toHaveLength(1);
  expect(statements[0].Action).toBe('lambda:InvokeFunction');
  const aliasId = Object.keys(template.findResources('AWS::Lambda::Alias'))[0];
  expect(statements[0].Resource).toEqual({ Ref: aliasId });
  const resources = Object.values(template.toJSON().Resources) as any[];
  expect(resources.some(resource => /ECS|Cognito|RDS|CloudFront|GatewayTarget/.test(resource.Type))).toBe(false);
});

test('quota update permissions and browser/gateway calls are resource scoped', () => {
  const template = Template.fromStack(make());
  const policies = Object.values(template.findResources('AWS::IAM::Policy'));
  const statements = policies.flatMap(policy => policy.Properties.PolicyDocument.Statement);
  expect(statements.every(statement => statement.Resource !== '*')).toBe(true);
  const quota = statements.find(statement => statement.Action === 'dynamodb:UpdateItem');
  expect(quota).toBeDefined();
  expect(JSON.stringify(statements)).not.toMatch(/InvokeWebSearch|bedrock-mantle|CreateBrowser|CreateGateway|s3:|secretsmanager:/);
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    BillingMode: 'PAY_PER_REQUEST', TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
  });
  const quotaResource = Object.values(template.findResources('AWS::DynamoDB::Table'))[0];
  expect(quotaResource.DeletionPolicy).toBe('Retain');
  template.resourceCountIs('AWS::CloudWatch::Alarm', 2);
});

test.each([
  { subjects: [] }, { subjects: [''] }, { subjects: ['subject\n'] },
  { taskRoleArn: 'arn:aws:iam::999999999999:role/Other' },
  { gatewayArn: 'arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/owned-search-123' },
  { gatewayUrl: 'https://unrelated.example/mcp' },
  { gatewayUrl: 'https://owned-search-123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference' },
])('rejects unverified identity/resource configurations %p', options => {
  expect(() => make(options)).toThrow();
});

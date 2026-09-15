import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { execFileSync, spawnSync } from 'child_process';
import { createHash } from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { NativeWebStack, NativeWebStackProps } from '../lib/native-web-stack';

const root = path.resolve(__dirname, '..', '..');
const account = '123456789012';
const browserId = 'verified_canary-abcdefghij';
const quotaTableName = 'VerifiedCanaryQuota';
const modules = ['__init__', 'browser', 'brokered_browser', 'documents', 'gateway', 'http_fetch',
  'lambda_handler', 'quota', 'search', 'url_policy', 'native_handler'];
let bundle: string;

beforeAll(() => {
  bundle = fs.mkdtempSync(path.join(os.tmpdir(), 'native-web-template-'));
  fs.mkdirSync(path.join(bundle, 'web_capabilities'));
  for (const module of modules) {
    fs.copyFileSync(path.join(root, 'web_capabilities', `${module}.py`), path.join(bundle, 'web_capabilities', `${module}.py`));
  }
  fs.copyFileSync(path.join(root, 'web_capabilities', 'requirements-lambda.txt'), path.join(bundle, 'requirements-lambda.txt'));
  fs.writeFileSync(path.join(bundle, 'source-commit.txt'), execFileSync('git', ['-C', root, 'rev-parse', 'HEAD']));
  const manifest = [...modules.map(module => `web_capabilities/${module}.py`), 'requirements-lambda.txt', 'source-commit.txt']
    .sort().map(source => `${createHash('sha256').update(fs.readFileSync(path.join(bundle, source))).digest('hex')}  ${source}\n`).join('');
  fs.writeFileSync(path.join(bundle, 'source-sha256.txt'), manifest);
});

afterAll(() => fs.rmSync(bundle, { recursive: true, force: true }));

function verifiedResources(region: string) {
  return {
    env: { account, region },
    gatewayArn: `arn:aws:bedrock-agentcore:${region}:${account}:gateway/verified-search-123`,
    gatewayUrl: `https://verified-search-123.gateway.bedrock-agentcore.${region}.amazonaws.com/mcp`,
    browserArn: `arn:aws:bedrock-agentcore:${region}:${account}:browser-custom/${browserId}`,
    browserId,
    quotaTableArn: `arn:aws:dynamodb:${region}:${account}:table/${quotaTableName}`,
    quotaTableName,
  };
}

function make(overrides: Partial<NativeWebStackProps> = {}) {
  return new NativeWebStack(new cdk.App(), 'NativeTest', {
    ...verifiedResources('us-east-1'), adapterAssetPath: bundle, ...overrides,
  });
}

test('bounded ARM64 native adapter starts disabled with curated Browser routing', () => {
  const template = Template.fromStack(make());
  template.resourceCountIs('AWS::Lambda::Function', 1);
  const adapter = Object.values(template.findResources('AWS::Lambda::Function'))[0];
  expect(adapter.Properties).toMatchObject({
    Architectures: ['arm64'], Runtime: 'python3.12', Handler: 'web_capabilities.native_handler.handler',
    MemorySize: 1024, Timeout: 60, ReservedConcurrentExecutions: 2,
    Environment: { Variables: {
      AGENTCORE_WEB_REGION: 'us-east-1', AGENTCORE_WEB_QUOTA_TABLE: quotaTableName,
      AGENTCORE_WEB_GATEWAY_URL: verifiedResources('us-east-1').gatewayUrl,
      AGENTCORE_WEB_BROWSER_ID: browserId,
      AGENTCORE_WEB_SEARCH_ENABLED: 'false', AGENTCORE_WEB_FETCH_ENABLED: 'false',
      AGENTCORE_WEB_BROWSER_ENABLED: 'false',
      AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY: 'false',
      AGENTCORE_WEB_BROWSER_ROUTES: '["https://quotes.toscrape.com/js/"]',
      AGENTCORE_WEB_URL_POLICY: JSON.stringify(JSON.parse(fs.readFileSync(path.join(root, 'config', 'web-canary-url-policy.json'), 'utf8'))),
    } },
  });
  expect(adapter.Properties.VpcConfig).toBeUndefined();
  const secretId = Object.keys(template.findResources('AWS::SecretsManager::Secret'))[0];
  expect(adapter.Properties.Environment.Variables.AGENTCORE_WEB_SERVICE_SECRET_ARN).toEqual({ Ref: secretId });
  template.resourceCountIs('AWS::CloudWatch::Alarm', 0);
});

test.each([
  [false, false, false], [false, false, true], [false, true, false], [false, true, true],
  [true, false, false], [true, false, true], [true, true, false], [true, true, true],
])('feature flags remain independent: search=%s fetch=%s browser=%s', (searchEnabled, fetchEnabled, browserEnabled) => {
  const template = Template.fromStack(make({ searchEnabled, fetchEnabled, browserEnabled, browserNetworkPolicyReady: browserEnabled }));
  template.hasResourceProperties('AWS::Lambda::Function', {
    Environment: { Variables: {
      AGENTCORE_WEB_SEARCH_ENABLED: String(searchEnabled), AGENTCORE_WEB_FETCH_ENABLED: String(fetchEnabled),
      AGENTCORE_WEB_BROWSER_ENABLED: String(browserEnabled),
      AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY: String(browserEnabled),
    } },
  });
});

test('public HTTPS URL targets live alias and never grants unrestricted direct invocation', () => {
  const template = Template.fromStack(make());
  template.resourceCountIs('AWS::Lambda::Version', 1);
  template.resourceCountIs('AWS::Lambda::Alias', 1);
  template.resourceCountIs('AWS::Lambda::Url', 1);
  template.hasResourceProperties('AWS::Lambda::Alias', { Name: 'live' });
  const aliasId = Object.keys(template.findResources('AWS::Lambda::Alias'))[0];
  const functionId = Object.keys(template.findResources('AWS::Lambda::Function'))[0];
  const endpoint = Object.values(template.findResources('AWS::Lambda::Url'))[0];
  expect(endpoint.Properties).toEqual({
    AuthType: 'NONE', Qualifier: 'live', TargetFunctionArn: { 'Fn::GetAtt': [functionId, 'Arn'] },
  });
  expect(endpoint.DependsOn).toContain(aliasId);
  const permissions = Object.values(template.findResources('AWS::Lambda::Permission')).map(resource => resource.Properties);
  expect(permissions).toHaveLength(2);
  expect(permissions).toEqual(expect.arrayContaining([
    { Action: 'lambda:InvokeFunctionUrl', FunctionName: { Ref: aliasId }, Principal: '*', FunctionUrlAuthType: 'NONE' },
    { Action: 'lambda:InvokeFunction', FunctionName: { Ref: aliasId }, Principal: '*', InvokedViaFunctionUrl: true },
  ]));
});

test('only new runtime role receives exact-resource service and logging permissions', () => {
  const template = Template.fromStack(make());
  template.resourceCountIs('AWS::IAM::Role', 1);
  template.resourceCountIs('AWS::IAM::Policy', 1);
  const roleId = Object.keys(template.findResources('AWS::IAM::Role'))[0];
  const role = Object.values(template.findResources('AWS::IAM::Role'))[0];
  expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  expect(role.Properties.AssumeRolePolicyDocument.Statement).toEqual([
    { Action: 'sts:AssumeRole', Effect: 'Allow', Principal: { Service: 'lambda.amazonaws.com' } },
  ]);
  const policy = Object.values(template.findResources('AWS::IAM::Policy'))[0];
  expect(policy.Properties.Roles).toEqual([{ Ref: roleId }]);
  const statements = policy.Properties.PolicyDocument.Statement;
  const secretId = Object.keys(template.findResources('AWS::SecretsManager::Secret'))[0];
  expect(statements).toEqual(expect.arrayContaining([
    { Effect: 'Allow', Action: 'secretsmanager:GetSecretValue', Resource: { Ref: secretId } },
    { Effect: 'Allow', Action: 'bedrock-agentcore:InvokeGateway', Resource: verifiedResources('us-east-1').gatewayArn },
    { Effect: 'Allow', Action: 'dynamodb:UpdateItem', Resource: verifiedResources('us-east-1').quotaTableArn },
    { Effect: 'Allow', Action: ['bedrock-agentcore:StartBrowserSession', 'bedrock-agentcore:GetBrowserSession',
      'bedrock-agentcore:StopBrowserSession', 'bedrock-agentcore:ConnectBrowserAutomationStream'],
    Resource: verifiedResources('us-east-1').browserArn },
  ]));
  expect(statements).toHaveLength(5);
  const logging = statements.find((statement: any) => Array.isArray(statement.Action) && statement.Action.includes('logs:PutLogEvents'));
  const logId = Object.keys(template.findResources('AWS::Logs::LogGroup'))[0];
  expect(JSON.stringify(logging.Resource)).toContain(logId);
  expect(statements.every((statement: any) => statement.Resource !== '*' && !statement.NotResource)).toBe(true);
  expect(JSON.stringify(statements)).not.toMatch(/DescribeSecret|ListSecrets|CreateBrowser|CreateGateway|Scan|GetItem|iam:PassRole/);
});

test('only adapter resources are created; secret and logs are retained without rotation or token outputs', () => {
  const template = Template.fromStack(make({ alarmsEnabled: true }));
  const resources = Object.values(template.toJSON().Resources) as any[];
  const allowed = ['AWS::Lambda::Function', 'AWS::Lambda::Version', 'AWS::Lambda::Alias', 'AWS::Lambda::Url',
    'AWS::Lambda::Permission', 'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::Logs::LogGroup',
    'AWS::Logs::MetricFilter', 'AWS::CloudWatch::Alarm', 'AWS::SecretsManager::Secret'];
  expect(resources.every(resource => allowed.includes(resource.Type))).toBe(true);
  const secret = Object.values(template.findResources('AWS::SecretsManager::Secret'))[0];
  expect(secret).toMatchObject({ DeletionPolicy: 'Retain', UpdateReplacePolicy: 'Retain', Properties: {
    GenerateSecretString: { PasswordLength: 64, ExcludePunctuation: true, IncludeSpace: false },
  } });
  expect(secret.Properties.SecretString).toBeUndefined();
  const logGroup = Object.values(template.findResources('AWS::Logs::LogGroup'))[0];
  expect(logGroup).toMatchObject({ DeletionPolicy: 'Retain', UpdateReplacePolicy: 'Retain', Properties: { RetentionInDays: 7 } });
  template.resourceCountIs('AWS::CloudWatch::Alarm', 2);
  template.resourceCountIs('AWS::Logs::MetricFilter', 1);
  template.hasResourceProperties('AWS::Logs::MetricFilter', {
    FilterPattern: '{ $.event = "native_web_capability" && $.success = false }',
  });
  const outputs = template.toJSON().Outputs;
  expect(Object.keys(outputs).sort()).toEqual(['AdapterArn', 'AdapterLogsName', 'EndpointUrl', 'ServiceSecretArn']);
  const aliasId = Object.keys(template.findResources('AWS::Lambda::Alias'))[0];
  expect(outputs.AdapterArn.Value).toEqual({ Ref: aliasId });
  expect(JSON.stringify(template.toJSON())).not.toContain('resolve:secretsmanager');
});

test.each(['us-east-1', 'eu-west-1', 'ap-northeast-1'])('accepts explicit verified resources in %s', region => {
  expect(() => Template.fromStack(make(verifiedResources(region)))).not.toThrow();
});

test.each([
  { env: undefined }, { env: { region: 'us-east-1' } }, { env: { account, region: 'us-west-2' } },
  { env: { account: account + '\n', region: 'us-east-1' } },
  { gatewayArn: 'arn:aws:bedrock-agentcore:us-east-1:999999999999:gateway/verified-search-123' },
  { gatewayArn: 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/*' },
  { gatewayArn: 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/verified-search-123\n' },
  { gatewayUrl: 'https://verified-search-123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp?redirect=evil' },
  { gatewayUrl: 'https://verified-search-123.gateway.bedrock-agentcore.us-east-1.amazonaws.com.evil/mcp' },
  { browserArn: `arn:aws:bedrock-agentcore:us-east-1:999999999999:browser-custom/${browserId}` },
  { browserArn: `arn:aws:bedrock-agentcore:eu-west-1:${account}:browser-custom/${browserId}` },
  { browserId: 'different' }, { browserId: '*' }, { browserId: browserId + '\n' },
  { quotaTableArn: `arn:aws:dynamodb:us-east-1:999999999999:table/${quotaTableName}` },
  { quotaTableArn: `arn:aws:dynamodb:eu-west-1:${account}:table/${quotaTableName}` },
  { quotaTableArn: `arn:aws:dynamodb:us-east-1:${account}:table/${quotaTableName}/index/anything` },
  { quotaTableName: 'different' }, { quotaTableName: '*' }, { quotaTableName: quotaTableName + '\n' },
  { adapterAssetPath: 'relative' }, { adapterAssetPath: root },
  { searchEnabled: 'true' as unknown as boolean }, { fetchEnabled: 1 as unknown as boolean },
  { browserEnabled: null as unknown as boolean }, { alarmsEnabled: 'false' as unknown as boolean },
  { browserNetworkPolicyReady: 'true' as unknown as boolean },
  { browserEnabled: true }, { browserEnabled: true, browserNetworkPolicyReady: false },
])('rejects unverified or ambiguous configuration %#', overrides => {
  expect(() => make(overrides)).toThrow();
});

test.each([
  ...modules.map(module => [`web_capabilities/${module}.py`, 'Stale native adapter bundle']),
  ['requirements-lambda.txt', 'Stale native adapter dependency lock'],
  ['source-commit.txt', 'Stale native adapter source commit marker'],
  ['source-sha256.txt', 'Invalid native adapter source SHA256 manifest'],
])('rejects stale source or provenance: %s', (source, message) => {
  const filename = path.join(bundle, source);
  const original = fs.readFileSync(filename);
  try {
    fs.appendFileSync(filename, '\nstale');
    expect(() => make()).toThrow(message);
  } finally {
    fs.writeFileSync(filename, original);
  }
});

test('canonical asset paths cannot bypass the external-directory guard', () => {
  const link = path.join(bundle, 'checkout-link');
  fs.symlinkSync(root, link, 'dir');
  try {
    expect(() => make({ adapterAssetPath: link })).toThrow('external immutable asset directory');
  } finally {
    fs.unlinkSync(link);
  }
});

test.each([
  [{}, 'nativeWeb=on'],
  [{ nativeWeb: 'off' }, 'nativeWeb=on'],
  [{ nativeWeb: 'on' }, 'absolute private nativeWebConfigPath'],
  [{ nativeWeb: 'on', nativeWebConfigPath: 'relative.json' }, 'absolute private nativeWebConfigPath'],
])('standalone entry point requires opt-in and absolute private configuration %#', (context, message) => {
  const result = spawnSync(process.execPath, ['-r', 'ts-node/register/transpile-only', 'bin/native-web.ts'], {
    cwd: path.join(root, 'infra'), encoding: 'utf8',
    env: { ...process.env, CDK_CONTEXT_JSON: JSON.stringify(context) },
  });
  expect(result.status).not.toBe(0);
  expect(result.stderr).toContain(message);
}, 30000);

test('build script refuses relative, existing, and normalized in-checkout paths before installing', () => {
  for (const destination of ['relative', bundle, path.join(root, '..', path.basename(root), 'rejected-bundle')]) {
    const result = spawnSync('sh', [path.join(root, 'scripts', 'build-native-web.sh'), destination], { encoding: 'utf8' });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/absolute|outside the checkout|not overwritten/);
  }
});

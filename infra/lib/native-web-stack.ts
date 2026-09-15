import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { execFileSync } from 'child_process';
import { createHash } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

export interface NativeWebStackProps extends cdk.StackProps {
  readonly adapterAssetPath: string;
  readonly gatewayArn: string;
  readonly gatewayUrl: string;
  readonly browserArn: string;
  readonly browserId: string;
  readonly quotaTableArn: string;
  readonly quotaTableName: string;
  readonly searchEnabled?: boolean;
  readonly fetchEnabled?: boolean;
  readonly browserEnabled?: boolean;
  readonly browserNetworkPolicyReady?: boolean;
  readonly alarmsEnabled?: boolean;
}

export class NativeWebStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: NativeWebStackProps) {
    super(scope, id, props);
    const account = props.env?.account;
    const region = props.env?.region;
    if (!account || account.length !== 12 || !/^\d{12}$/.test(account) || !region
      || !['us-east-1', 'eu-west-1', 'ap-northeast-1'].includes(region)) {
      throw new Error('Configure an explicit authorized account and supported region');
    }
    const gatewayMatch = new RegExp(`^arn:aws:bedrock-agentcore:${region}:${account}:gateway/([a-z0-9-]+)$`).exec(props.gatewayArn);
    if (!gatewayMatch || gatewayMatch[0] !== props.gatewayArn
      || props.gatewayUrl !== `https://${gatewayMatch[1]}.gateway.bedrock-agentcore.${region}.amazonaws.com/mcp`) {
      throw new Error('Gateway ARN and MCP URL must identify the same verified account/region resource');
    }
    if (typeof props.browserId !== 'string' || props.browserId.trim() !== props.browserId
      || !/^[A-Za-z0-9_-]{1,128}$/.test(props.browserId)
      || props.browserArn !== `arn:aws:bedrock-agentcore:${region}:${account}:browser-custom/${props.browserId}`) {
      throw new Error('Browser ARN and ID must identify the same verified same-account/region custom Browser');
    }
    if (typeof props.quotaTableName !== 'string' || props.quotaTableName.trim() !== props.quotaTableName
      || !/^[A-Za-z0-9_.-]{3,255}$/.test(props.quotaTableName)
      || props.quotaTableArn !== `arn:aws:dynamodb:${region}:${account}:table/${props.quotaTableName}`) {
      throw new Error('Quota table ARN and name must identify the same verified account/region table');
    }
    for (const flag of [props.searchEnabled, props.fetchEnabled, props.browserEnabled,
      props.browserNetworkPolicyReady, props.alarmsEnabled]) {
      if (flag !== undefined && typeof flag !== 'boolean') {
        throw new Error('Native web feature and alarm flags must be JSON booleans');
      }
    }
    if (props.browserEnabled === true && props.browserNetworkPolicyReady !== true) {
      throw new Error('Browser enablement requires explicit browserNetworkPolicyReady attestation of the verified canary isolation');
    }
    const root = fs.realpathSync(path.resolve(__dirname, '..', '..'));
    if (typeof props.adapterAssetPath !== 'string' || !path.isAbsolute(props.adapterAssetPath)) {
      throw new Error('Build the adapter into an external immutable asset directory');
    }
    const bundle = fs.realpathSync(props.adapterAssetPath);
    if (bundle === root || bundle.startsWith(root + path.sep)) {
      throw new Error('Build the adapter into an external immutable asset directory');
    }
    const modules = ['__init__', 'browser', 'brokered_browser', 'documents', 'gateway', 'http_fetch',
      'lambda_handler', 'quota', 'search', 'url_policy', 'native_handler'];
    const sources = modules.map(module => `web_capabilities/${module}.py`);
    for (const source of sources) {
      if (!fs.readFileSync(path.join(bundle, source)).equals(fs.readFileSync(path.join(root, source)))) {
        throw new Error(`Stale native adapter bundle: ${source}`);
      }
    }
    if (!fs.readFileSync(path.join(bundle, 'requirements-lambda.txt'))
      .equals(fs.readFileSync(path.join(root, 'web_capabilities', 'requirements-lambda.txt')))) {
      throw new Error('Stale native adapter dependency lock');
    }
    const commit = execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' });
    if (fs.readFileSync(path.join(bundle, 'source-commit.txt'), 'utf8') !== commit) {
      throw new Error('Stale native adapter source commit marker');
    }
    const manifest = [...sources, 'requirements-lambda.txt', 'source-commit.txt'].sort()
      .map(source => `${createHash('sha256').update(fs.readFileSync(path.join(bundle, source))).digest('hex')}  ${source}\n`).join('');
    if (fs.readFileSync(path.join(bundle, 'source-sha256.txt'), 'utf8') !== manifest) {
      throw new Error('Invalid native adapter source SHA256 manifest');
    }
    const serviceSecret = new secretsmanager.Secret(this, 'ServiceSecret', {
      generateSecretString: { passwordLength: 64, excludePunctuation: true, includeSpace: false },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const logGroup = new logs.LogGroup(this, 'AdapterLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const role = new iam.Role(this, 'AdapterRole', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    logGroup.grantWrite(role);
    const adapter = new lambda.Function(this, 'Adapter', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'web_capabilities.native_handler.handler',
      code: lambda.Code.fromAsset(bundle, { exclude: ['**/__pycache__', '**/*.pyc'] }),
      timeout: cdk.Duration.seconds(60),
      memorySize: 1024,
      reservedConcurrentExecutions: 2,
      role,
      logGroup,
      environment: {
        AGENTCORE_WEB_REGION: region,
        AGENTCORE_WEB_QUOTA_TABLE: props.quotaTableName,
        AGENTCORE_WEB_GATEWAY_URL: props.gatewayUrl,
        AGENTCORE_WEB_BROWSER_ID: props.browserId,
        AGENTCORE_WEB_SEARCH_ENABLED: props.searchEnabled === true ? 'true' : 'false',
        AGENTCORE_WEB_FETCH_ENABLED: props.fetchEnabled === true ? 'true' : 'false',
        AGENTCORE_WEB_BROWSER_ENABLED: props.browserEnabled === true ? 'true' : 'false',
        AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY: props.browserNetworkPolicyReady === true ? 'true' : 'false',
        AGENTCORE_WEB_URL_POLICY: JSON.stringify(JSON.parse(fs.readFileSync(path.join(root, 'config', 'web-canary-url-policy.json'), 'utf8'))),
        AGENTCORE_WEB_BROWSER_ROUTES: JSON.stringify(['https://quotes.toscrape.com/js/']),
        AGENTCORE_WEB_SERVICE_SECRET_ARN: serviceSecret.secretArn,
        PYTHONDONTWRITEBYTECODE: '1',
      },
    });
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'], resources: [serviceSecret.secretArn],
    }));
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeGateway'], resources: [props.gatewayArn],
    }));
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:StartBrowserSession', 'bedrock-agentcore:GetBrowserSession',
        'bedrock-agentcore:StopBrowserSession', 'bedrock-agentcore:ConnectBrowserAutomationStream'],
      resources: [props.browserArn],
    }));
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem'], resources: [props.quotaTableArn],
    }));
    const alias = new lambda.Alias(this, 'Live', { aliasName: 'live', version: adapter.currentVersion });
    const endpoint = alias.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });
    if (props.alarmsEnabled === true) {
      const rejected = new logs.MetricFilter(this, 'RejectedMetric', {
        logGroup, filterPattern: logs.FilterPattern.literal('{ $.event = "native_web_capability" && $.success = false }'),
        metricNamespace: 'OpenWebUI/NativeWeb', metricName: 'Rejected', metricValue: '1',
      });
      new cloudwatch.Alarm(this, 'RejectedRequests', {
        metric: rejected.metric({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
        threshold: 3, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      new cloudwatch.Alarm(this, 'AdapterRuntimeFailures', {
        metric: alias.metricErrors({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
        threshold: 1, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }
    new cdk.CfnOutput(this, 'EndpointUrl', { value: endpoint.url });
    new cdk.CfnOutput(this, 'ServiceSecretArn', { value: serviceSecret.secretArn });
    new cdk.CfnOutput(this, 'AdapterArn', { value: alias.functionArn });
    new cdk.CfnOutput(this, 'AdapterLogsName', { value: logGroup.logGroupName });
  }
}

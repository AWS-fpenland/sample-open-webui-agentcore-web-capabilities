import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { WebBrowserIsolation } from './web-browser-isolation';

export interface WebCanaryStackProps extends cdk.StackProps {
  readonly adapterAssetPath: string;
  readonly gatewayArn: string;
  readonly gatewayUrl: string;
  readonly taskRoleArn: string;
  readonly subjects: string[];
  readonly availabilityZoneId: string;
  readonly searchEnabled?: boolean;
  readonly fetchEnabled?: boolean;
  readonly browserEnabled?: boolean;
}

export class WebCanaryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: WebCanaryStackProps) {
    super(scope, id, props);
    const account = props.env?.account;
    const region = props.env?.region;
    if (!account || !/^\d{12}$/.test(account) || !region
      || !['us-east-1', 'eu-west-1', 'ap-northeast-1'].includes(region)) {
      throw new Error('Configure an explicit authorized account and supported region');
    }
    if (!new RegExp(`^arn:aws:iam::${account}:role/[A-Za-z0-9+=,.@_-]+$`).test(props.taskRoleArn)) {
      throw new Error('Task role must be an explicitly verified same-account role');
    }
    const gatewayMatch = new RegExp(`^arn:aws:bedrock-agentcore:${region}:${account}:gateway/([a-z0-9-]+)$`).exec(props.gatewayArn);
    if (!gatewayMatch || props.gatewayUrl !== `https://${gatewayMatch[1]}.gateway.bedrock-agentcore.${region}.amazonaws.com/mcp`) {
      throw new Error('Gateway ARN and MCP URL must identify the same verified account/region resource');
    }
    if (!Array.isArray(props.subjects) || props.subjects.length < 1 || props.subjects.length > 10
      || props.subjects.some(subject => typeof subject !== 'string' || !/^[A-Za-z0-9_-]{1,256}$/.test(subject))) {
      throw new Error('Configure exact authorized Open WebUI canary subjects');
    }
    const root = path.resolve(__dirname, '..', '..');
    const bundle = path.resolve(props.adapterAssetPath);
    if (!path.isAbsolute(props.adapterAssetPath) || bundle.startsWith(root + path.sep)) {
      throw new Error('Build the adapter into an external immutable asset directory');
    }
    for (const module of ['__init__', 'browser', 'brokered_browser', 'documents', 'gateway', 'http_fetch', 'lambda_handler', 'quota', 'search', 'url_policy']) {
      if (!fs.readFileSync(path.join(bundle, 'web_capabilities', `${module}.py`))
        .equals(fs.readFileSync(path.join(root, 'web_capabilities', `${module}.py`)))) {
        throw new Error(`Stale adapter bundle: ${module}`);
      }
    }
    if (!fs.readFileSync(path.join(bundle, 'requirements-lambda.txt'))
      .equals(fs.readFileSync(path.join(root, 'web_capabilities', 'requirements-lambda.txt')))) {
      throw new Error('Stale adapter dependency lock');
    }
    const isolation = new WebBrowserIsolation(this, 'BrowserIsolation', {
      availabilityZoneId: props.availabilityZoneId,
    });
    const quota = new dynamodb.Table(this, 'Quota', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const logGroup = new logs.LogGroup(this, 'AdapterLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const adapter = new lambda.Function(this, 'Adapter', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'web_capabilities.lambda_handler.handler',
      code: lambda.Code.fromAsset(bundle, { exclude: ['**/__pycache__', '**/*.pyc'] }),
      timeout: cdk.Duration.seconds(60),
      memorySize: 1024,
      reservedConcurrentExecutions: 2,
      logGroup,
      environment: {
        AGENTCORE_WEB_REGION: region,
        AGENTCORE_WEB_SUBJECTS: JSON.stringify(props.subjects),
        AGENTCORE_WEB_QUOTA_TABLE: quota.tableName,
        AGENTCORE_WEB_GATEWAY_URL: props.gatewayUrl,
        AGENTCORE_WEB_BROWSER_ID: isolation.browserId,
        AGENTCORE_WEB_SEARCH_ENABLED: props.searchEnabled === true ? 'true' : 'false',
        AGENTCORE_WEB_FETCH_ENABLED: props.fetchEnabled === true ? 'true' : 'false',
        AGENTCORE_WEB_BROWSER_ENABLED: props.browserEnabled === true ? 'true' : 'false',
        AGENTCORE_WEB_URL_POLICY: JSON.stringify(JSON.parse(fs.readFileSync(path.join(root, 'config', 'web-canary-url-policy.json'), 'utf8'))),
        PYTHONDONTWRITEBYTECODE: '1',
      },
    });
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeGateway'], resources: [props.gatewayArn],
    }));
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:StartBrowserSession', 'bedrock-agentcore:GetBrowserSession',
        'bedrock-agentcore:StopBrowserSession', 'bedrock-agentcore:ConnectBrowserAutomationStream'],
      resources: [isolation.browserArn],
    }));
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem'], resources: [quota.tableArn],
    }));
    const alias = new lambda.Alias(this, 'Live', { aliasName: 'live', version: adapter.currentVersion });
    const role = iam.Role.fromRoleArn(this, 'VerifiedTaskRole', props.taskRoleArn, { mutable: true });
    const invocation = new iam.Policy(this, 'TaskCanaryInvocation', {
      roles: [role], statements: [new iam.PolicyStatement({
        actions: ['lambda:InvokeFunction'], resources: [alias.functionArn],
      })],
    });
    invocation.node.addDependency(alias);
    const rejected = new logs.MetricFilter(this, 'RejectedMetric', {
      logGroup, filterPattern: logs.FilterPattern.literal('{ $.event = "web_capability" && $.success = false }'),
      metricNamespace: 'OpenWebUI/WebCanary', metricName: 'Rejected', metricValue: '1',
    });
    new cloudwatch.Alarm(this, 'CanaryFailures', {
      metric: rejected.metric({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
      threshold: 3, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, 'AdapterRuntimeFailures', {
      metric: adapter.metricErrors({ period: cdk.Duration.minutes(5) }), threshold: 1, evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cdk.CfnOutput(this, 'AdapterArn', { value: alias.functionArn });
    new cdk.CfnOutput(this, 'AdapterLogsName', { value: logGroup.logGroupName });
    new cdk.CfnOutput(this, 'BrowserId', { value: isolation.browserId });
    new cdk.CfnOutput(this, 'BrowserVpcId', { value: isolation.vpcId });
    new cdk.CfnOutput(this, 'BrowserSubnetId', { value: isolation.subnetIds[0] });
    new cdk.CfnOutput(this, 'BrowserSecurityGroupId', { value: isolation.securityGroupId });
    new cdk.CfnOutput(this, 'QuotaTableName', { value: quota.tableName });
  }
}

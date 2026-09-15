import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as path from 'path';

export interface WebCapabilitiesStackProps extends cdk.StackProps {
  readonly providerAssetPath: string;
}

export const WEB_SEARCH_REGIONS = ['us-east-1', 'eu-west-1', 'ap-northeast-1'];
export const WEB_SEARCH_VERSION = '1.2.0';
export const WEB_SEARCH_TOOL = 'web-search-tool___WebSearch';

function providerAsset(assetPath: string): lambda.Code {
  const buildHelp = 'Prebuild with pip --target; see gateway/web-search-provisioner/index.py. '
    + 'Pass the absolute external bundle directory as -c providerAssetPath=...';
  if (typeof assetPath !== 'string' || !path.isAbsolute(assetPath) || !fs.existsSync(assetPath)) {
    throw new Error(buildHelp);
  }
  const resolved = fs.realpathSync(assetPath);
  const repoRoot = fs.realpathSync(path.join(__dirname, '..', '..'));
  const relative = path.relative(repoRoot, resolved);
  if (!relative.startsWith(`..${path.sep}`) && relative !== '..') {
    throw new Error(`Provider bundle must be outside the checkout. ${buildHelp}`);
  }
  const source = path.join(repoRoot, 'gateway', 'web-search-provisioner');
  for (const filename of ['index.py', 'requirements.txt']) {
    if (!fs.existsSync(path.join(resolved, filename))
      || !fs.readFileSync(path.join(resolved, filename)).equals(fs.readFileSync(path.join(source, filename)))) {
      throw new Error(`Provider bundle is missing or stale: ${filename}. ${buildHelp}`);
    }
  }
  for (const packageName of ['boto3', 'botocore']) {
    const metadata = path.join(resolved, `${packageName}-1.43.94.dist-info`, 'METADATA');
    const modulePath = path.join(resolved, packageName, '__init__.py');
    if (!fs.existsSync(metadata) || !fs.readFileSync(metadata, 'utf8').includes('\nVersion: 1.43.94\n')
      || !fs.existsSync(modulePath)
      || !/__version__\s*=\s*['"]1\.43\.94['"]/.test(fs.readFileSync(modulePath, 'utf8'))) {
      throw new Error(`Provider must bundle ${packageName}==1.43.94. ${buildHelp}`);
    }
  }
  for (const moduleName of ['s3transfer', 'jmespath', 'dateutil', 'urllib3']) {
    if (!fs.existsSync(path.join(resolved, moduleName, '__init__.py'))) {
      throw new Error(`Provider dependency missing: ${moduleName}. ${buildHelp}`);
    }
  }
  if (!fs.existsSync(path.join(resolved, 'six.py'))
    || !fs.existsSync(path.join(resolved, 'botocore', 'data', 'bedrock-agentcore-control'))) {
    throw new Error(`Provider dependencies or AgentCore service model missing. ${buildHelp}`);
  }
  return lambda.Code.fromAsset(resolved, { exclude: ['**/__pycache__', '**/*.pyc'] });
}

export class WebCapabilitiesStack extends cdk.Stack {
  public readonly gatewayArn: string;
  public readonly gatewayId: string;
  public readonly gatewayUrl: string;

  constructor(scope: Construct, id: string, props: WebCapabilitiesStackProps) {
    super(scope, id, props);
    const account = props.env?.account;
    const region = props.env?.region;
    if (!account || cdk.Token.isUnresolved(account) || !/^\d{12}$/.test(account)) {
      throw new Error('Search requires an explicit 12-digit account: -c account=...');
    }
    if (!region || cdk.Token.isUnresolved(region) || !WEB_SEARCH_REGIONS.includes(region)) {
      throw new Error(`Search requires an explicit approved region: ${WEB_SEARCH_REGIONS.join(', ')}`);
    }
    const code = providerAsset(props.providerAssetPath);
    const gatewayRole = new iam.Role(this, 'SearchGatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${region}:${account}:gateway/*` },
        },
      }),
      inlinePolicies: {
        WebSearch: new iam.PolicyDocument({ statements: [new iam.PolicyStatement({
          actions: ['bedrock-agentcore:InvokeWebSearch'],
          resources: [`arn:aws:bedrock-agentcore:${region}:aws:tool/web-search.v1`],
        })] }),
      },
    });
    const gateway = new cdk.CfnResource(this, 'SearchGateway', {
      type: 'AWS::BedrockAgentCore::Gateway',
      properties: {
        Name: 'open-webui-web-search',
        RoleArn: gatewayRole.roleArn,
        ProtocolType: 'MCP',
        ProtocolConfiguration: { Mcp: { SupportedVersions: ['2025-03-26'] } },
        AuthorizerType: 'AWS_IAM',
      },
    });
    this.gatewayId = gateway.getAtt('GatewayIdentifier').toString();
    this.gatewayArn = gateway.getAtt('GatewayArn').toString();
    this.gatewayUrl = gateway.getAtt('GatewayUrl').toString();
    const invokePolicy = new iam.Policy(this, 'InvokeOwnSearchGateway', {
      roles: [gatewayRole],
      statements: [new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [this.gatewayArn],
      })],
    });
    const target = new agentcore.CfnGatewayTarget(this, 'SearchTarget', {
      gatewayIdentifier: this.gatewayId,
      name: 'web-search-tool',
      targetConfiguration: {
        mcp: { connector: {
          source: { connectorId: 'web-search' },
          enabled: ['WebSearch'],
          configurations: [{ name: 'WebSearch', parameterValues: {} }],
        } },
      },
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
    });
    target.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    target.node.addDependency(invokePolicy);
    const provisioner = new lambda.Function(this, 'SearchTargetProvisioner', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code,
      timeout: cdk.Duration.minutes(6),
      memorySize: 256,
      logGroup: new logs.LogGroup(this, 'SearchProvisionerLogs', {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    provisioner.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:GetGatewayTarget', 'bedrock-agentcore:UpdateGatewayTarget',
        'bedrock-agentcore:SynchronizeGatewayTargets'],
      resources: [this.gatewayArn],
    }));
    const provider = new cr.Provider(this, 'SearchTargetProvider', {
      onEventHandler: provisioner,
      frameworkLambdaLoggingLevel: lambda.ApplicationLogLevel.FATAL,
    });
    const versionPin = new cdk.CustomResource(this, 'SearchVersionPin', {
      resourceType: 'Custom::WebSearchVersionPin',
      serviceToken: provider.serviceToken,
      properties: {
        GatewayIdentifier: this.gatewayId,
        TargetId: target.attrTargetId,
        TargetName: 'web-search-tool',
        ConnectorId: 'web-search',
        ConnectorVersion: WEB_SEARCH_VERSION,
      },
    });
    versionPin.node.addDependency(target);
    versionPin.node.addDependency(provisioner.role!);
    new cdk.CfnOutput(this, 'GatewayUrl', { value: this.gatewayUrl });
    new cdk.CfnOutput(this, 'GatewayId', { value: this.gatewayId });
    new cdk.CfnOutput(this, 'ToolName', { value: WEB_SEARCH_TOOL });
    new cdk.CfnOutput(this, 'ConnectorVersion', { value: WEB_SEARCH_VERSION });
  }
}

// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import { AiqWorkbench } from './aiq-workbench';

/**
 * AI-Q on Amazon Bedrock AgentCore — one additive, run-scoped stack.
 *
 * Everything this stack creates is prefixed with `aiq-<runId>` so several
 * independent runs can coexist in one account and be torn down individually.
 * It never references or mutates the Open WebUI application stacks; the only
 * shared dependency is the *read-only* use of the deployment's Cognito user
 * pool as the JWT issuer trusted by the AgentCore Runtime.
 *
 * Resources
 *  - ECR repository + CodeBuild (ARM64, privileged) that builds the runtime
 *    container remotely from an S3 source zip (no local Docker required).
 *  - Private S3 bucket: source zips, build digests, per-tenant reports and
 *    citation ledgers, per-tenant documents (+ Knowledge Base metadata sidecars).
 *  - DynamoDB: jobs (per-tenant partition) and events (append-only journal).
 *  - AgentCore Gateway (AWS_IAM inbound) with the managed Web Search connector.
 *  - Amazon S3 Vectors bucket/index + Bedrock Knowledge Base + S3 data source.
 *  - AgentCore Runtime (created only when an image tag/digest is supplied —
 *    the image must exist first), CUSTOM_JWT authorizer trusting the Open WebUI
 *    Cognito app client, HTTP protocol, Authorization header forwarded.
 */
export const GUARDRAIL_MODES = ['off', 'audit', 'enforce'] as const;
export type GuardrailMode = (typeof GUARDRAIL_MODES)[number];
export const GUARDRAIL_STRENGTHS = ['NONE', 'LOW', 'MEDIUM', 'HIGH'] as const;
export type GuardrailStrength = (typeof GUARDRAIL_STRENGTHS)[number];
export type GuardrailFilterType = 'PROMPT_ATTACK' | 'HATE' | 'INSULTS' | 'SEXUAL' | 'VIOLENCE' | 'MISCONDUCT';
/** Defaults used when a guardrail is enabled. PROMPT_ATTACK stays NONE: legitimate research requests are imperative
 *  by nature ("deeply research X and build me a package"), which that filter mistakes for instruction overrides. */
export const DEFAULT_GUARDRAIL_FILTERS: Record<GuardrailFilterType, GuardrailStrength> = {
  PROMPT_ATTACK: 'NONE', HATE: 'MEDIUM', INSULTS: 'MEDIUM', SEXUAL: 'MEDIUM', VIOLENCE: 'LOW', MISCONDUCT: 'LOW',
};

export interface AiqModels {
  readonly router: string;
  readonly shallow: string;
  readonly planner: string;
  readonly researcher: string;
  readonly writer: string;
  readonly embedding: string;
}

export interface AiqStackProps extends cdk.StackProps {
  /** Unique run identifier, e.g. `fable51-79d40d45`. Lower-case letters, digits, hyphens. */
  readonly runId: string;
  /** Existing Cognito user pool id whose access tokens the runtime accepts. */
  readonly userPoolId: string;
  /** App client ids whose tokens are accepted (the Open WebUI app client, optionally a smoke client). */
  readonly allowedClientIds: string[];
  /** Container image tag in the stack's ECR repository. When absent the Runtime is not created. */
  readonly imageTag?: string;
  /** Optional sha256 digest to pin instead of the tag (preferred for provenance). */
  readonly imageDigest?: string;
  readonly models: AiqModels;
  /** Days to keep jobs/events/reports (DynamoDB TTL + S3 lifecycle). */
  readonly retentionDays?: number;
  /** Extra runtime environment variables (never secrets). */
  readonly runtimeEnvironment?: Record<string, string>;
  /** Content policy at the adapter boundary (Amazon Bedrock Guardrail). 'off' (default): no guardrail resource and
   *  nothing in the request path. 'audit': every input/output is assessed and the assessment is journaled and metered,
   *  but nothing is blocked or rewritten. 'enforce': blocked inputs fail the job, blocked outputs are withheld. */
  readonly guardrailMode?: GuardrailMode;
  /** Per-filter strengths (NONE|LOW|MEDIUM|HIGH) merged over DEFAULT_GUARDRAIL_FILTERS; applied to INPUT and OUTPUT
   *  (PROMPT_ATTACK is INPUT-only by service definition). Ignored when guardrailMode is 'off'. */
  readonly guardrailFilters?: Partial<Record<GuardrailFilterType, GuardrailStrength>>;
  /** @deprecated Use guardrailMode. `true` maps to 'enforce'; anything else to 'off'. */
  readonly guardrail?: boolean;
  /** Fail shallow answers whose citations cannot be verified (upstream enforce_citations; default true). */
  readonly enforceCitations?: boolean;
  /** Max pages per job the Browser page loader may fetch (default 12). */
  readonly fetchMaxPages?: number;
  /** Output token budgets for the deep roles (planner/researchers) and the writer. */
  readonly maxTokensDeep?: number;
  readonly maxTokensWriter?: number;
  /** Phase 3 — Research Workbench (CloudFront + OAC static SPA, own PKCE client on the same pool). Absent = not deployed. */
  readonly workbench?: {
    readonly distDir: string;
    readonly owuiUrl: string;
    readonly cognitoDomainPrefix: string;
    readonly domainName?: string;
    readonly certificateArn?: string;
    readonly hostedZoneId?: string;
    readonly hostedZoneName?: string;
    /** Known runtime ARN (from a previous deploy) written into the SPA's config.json. */
    readonly runtimeArnHint?: string;
  };
}

export const WEB_SEARCH_CONNECTOR_ID = 'web-search';
export const WEB_SEARCH_TARGET_NAME = 'web-search-tool';
export const WEB_SEARCH_TOOL_NAME = `${WEB_SEARCH_TARGET_NAME}___WebSearch`;
export const EMBEDDING_DIMENSIONS = 1024;

export class AiqStack extends cdk.Stack {
  public readonly repository: ecr.Repository;
  public readonly artifacts: s3.Bucket;
  public readonly jobsTable: dynamodb.Table;
  public readonly eventsTable: dynamodb.Table;
  public readonly gatewayUrl: string;
  public readonly gatewayArn: string;
  public readonly knowledgeBaseId: string;
  public readonly dataSourceId: string;
  public readonly runtimeArn?: string;
  public readonly runtimeId?: string;

  constructor(scope: Construct, id: string, props: AiqStackProps) {
    super(scope, id, props);

    const { runId } = props;
    if (!/^[a-z0-9][a-z0-9-]{2,30}$/.test(runId)) {
      throw new Error('runId must be 3-31 chars of lower-case letters, digits and hyphens');
    }
    const account = props.env?.account;
    const region = props.env?.region;
    if (!account || cdk.Token.isUnresolved(account) || !/^\d{12}$/.test(account)) {
      throw new Error('AiqStack requires an explicit 12-digit account (env.account)');
    }
    if (!region || cdk.Token.isUnresolved(region)) {
      throw new Error('AiqStack requires an explicit region (env.region)');
    }
    if (props.allowedClientIds.length === 0) {
      throw new Error('At least one allowed Cognito app client id is required');
    }
    const prefix = `aiq-${runId}`;
    const runtimeName = `aiq_${runId.replace(/-/g, '_')}`; // Runtime names allow [A-Za-z0-9_] only
    const retentionDays = props.retentionDays ?? 30;

    cdk.Tags.of(this).add('aiq:run-id', runId);
    cdk.Tags.of(this).add('aiq:project', 'aiq-on-agentcore');

    // ── Container image build (remote, ARM64) ─────────────────────────────
    this.repository = new ecr.Repository(this, 'Repository', {
      repositoryName: prefix,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    this.artifacts = new s3.Bucket(this, 'Artifacts', {
      bucketName: `${prefix}-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: false,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [
        // phase-2 job layout expires with the journal; phase-3 Research Packages live under packages/<tenant>/ with NO expiry (ADR-21)
        { prefix: 'tenants/', expiration: cdk.Duration.days(retentionDays) },
        { prefix: 'source/', expiration: cdk.Duration.days(retentionDays) },
        { prefix: 'builds/', expiration: cdk.Duration.days(retentionDays) },
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(2) },
      ],
    });

    const buildLogs = new logs.LogGroup(this, 'ImageBuildLogs', {
      logGroupName: `/aws/codebuild/${prefix}-image`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const imageBuild = new codebuild.Project(this, 'ImageBuild', {
      projectName: `${prefix}-image`,
      description: `Builds the AI-Q on AgentCore runtime image (ARM64) for run ${runId} from an S3 source zip`,
      source: codebuild.Source.s3({ bucket: this.artifacts, path: 'source/latest.zip' }),
      environment: {
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
        computeType: codebuild.ComputeType.LARGE,
        privileged: true,
      },
      environmentVariables: {
        REPO_URI: { value: this.repository.repositoryUri },
        REPO_NAME: { value: this.repository.repositoryName },
        ARTIFACTS_BUCKET: { value: this.artifacts.bucketName },
        IMAGE_TAG: { value: 'dev' },
        SOURCE_COMMIT: { value: 'unknown' },
        AIQ_UPSTREAM_REF: { value: 'unpinned' },
      },
      timeout: cdk.Duration.minutes(60),
      logging: { cloudWatch: { logGroup: buildLogs } },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          pre_build: {
            commands: [
              'echo "source commit: $SOURCE_COMMIT  upstream: $AIQ_UPSTREAM_REF  tag: $IMAGE_TAG"',
              'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "${REPO_URI%%/*}"',
              'docker buildx version || true',
            ],
          },
          build: {
            commands: [
              'cd aiq/runtime',
              'docker build --platform linux/arm64 --progress=plain'
                + ' --build-arg SOURCE_COMMIT="$SOURCE_COMMIT" --build-arg AIQ_UPSTREAM_REF="$AIQ_UPSTREAM_REF"'
                + ' -t "$REPO_URI:$IMAGE_TAG" .',
            ],
          },
          post_build: {
            commands: [
              'docker push "$REPO_URI:$IMAGE_TAG"',
              'DIGEST=$(aws ecr describe-images --repository-name "$REPO_NAME" --image-ids imageTag="$IMAGE_TAG" --query "imageDetails[0].imageDigest" --output text)',
              'SIZE=$(aws ecr describe-images --repository-name "$REPO_NAME" --image-ids imageTag="$IMAGE_TAG" --query "imageDetails[0].imageSizeInBytes" --output text)',
              'printf "{\\"tag\\":\\"%s\\",\\"digest\\":\\"%s\\",\\"size_bytes\\":%s,\\"source_commit\\":\\"%s\\",\\"aiq_upstream_ref\\":\\"%s\\",\\"built_at\\":\\"%s\\"}\\n" "$IMAGE_TAG" "$DIGEST" "$SIZE" "$SOURCE_COMMIT" "$AIQ_UPSTREAM_REF" "$(date -u +%FT%TZ)" > build.json',
              'cat build.json',
              'aws s3 cp build.json "s3://$ARTIFACTS_BUCKET/builds/$IMAGE_TAG/build.json"',
            ],
          },
        },
      }),
    });
    this.repository.grantPullPush(imageBuild);
    imageBuild.addToRolePolicy(new iam.PolicyStatement({
      sid: 'DescribeBuiltImage',
      actions: ['ecr:DescribeImages'],
      resources: [this.repository.repositoryArn],
    }));
    this.artifacts.grantReadWrite(imageBuild);

    // ── Durable state ─────────────────────────────────────────────────────
    this.jobsTable = new dynamodb.Table(this, 'Jobs', {
      tableName: `${prefix}-jobs`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.eventsTable = new dynamodb.Table(this, 'Events', {
      tableName: `${prefix}-events`,
      partitionKey: { name: 'job_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'seq', type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Web search gateway (AWS_IAM inbound; the runtime role is the only caller) ──
    const gatewayRole = new iam.Role(this, 'GatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${region}:${account}:gateway/*` },
        },
      }),
      description: `AI-Q ${runId}: gateway execution role for the managed Web Search connector`,
    });
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeManagedWebSearch',
      actions: ['bedrock-agentcore:InvokeWebSearch'],
      resources: [`arn:aws:bedrock-agentcore:${region}:aws:tool/web-search.v1`],
    }));
    const gateway = new cdk.CfnResource(this, 'Gateway', {
      type: 'AWS::BedrockAgentCore::Gateway',
      properties: {
        Name: `${prefix}-gw`,
        Description: `AI-Q ${runId}: managed web search for the AgentCore runtime (IAM inbound)`,
        RoleArn: gatewayRole.roleArn,
        ProtocolType: 'MCP',
        ProtocolConfiguration: { Mcp: { SupportedVersions: ['2025-03-26'] } },
        AuthorizerType: 'AWS_IAM',
      },
    });
    this.gatewayArn = gateway.getAtt('GatewayArn').toString();
    this.gatewayUrl = gateway.getAtt('GatewayUrl').toString();
    const gatewayId = gateway.getAtt('GatewayIdentifier').toString();
    const selfInvoke = new iam.Policy(this, 'GatewaySelfInvoke', {
      roles: [gatewayRole],
      statements: [new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [this.gatewayArn],
      })],
    });
    const searchTarget = new agentcore.CfnGatewayTarget(this, 'WebSearchTarget', {
      gatewayIdentifier: gatewayId,
      name: WEB_SEARCH_TARGET_NAME,
      description: 'Managed AgentCore Web Search connector (results carry url/title/text/publishedDate)',
      targetConfiguration: {
        mcp: {
          connector: {
            source: { connectorId: WEB_SEARCH_CONNECTOR_ID },
            enabled: ['WebSearch'],
            configurations: [{ name: 'WebSearch', parameterValues: {} }],
          },
        },
      },
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
    });
    searchTarget.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    searchTarget.node.addDependency(selfInvoke);

    // ── Knowledge: S3 Vectors + Bedrock Knowledge Base over the documents/ prefix ──
    const vectorBucket = new cdk.CfnResource(this, 'VectorBucket', {
      type: 'AWS::S3Vectors::VectorBucket',
      properties: { VectorBucketName: `${prefix}-vectors` },
    });
    vectorBucket.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    const vectorIndex = new cdk.CfnResource(this, 'VectorIndex', {
      type: 'AWS::S3Vectors::Index',
      properties: {
        VectorBucketName: `${prefix}-vectors`,
        IndexName: 'documents',
        DataType: 'float32',
        Dimension: EMBEDDING_DIMENSIONS,
        DistanceMetric: 'cosine',
        // Chunk text is large and never filtered on; tenant_key/collection stay filterable.
        MetadataConfiguration: { NonFilterableMetadataKeys: ['AMAZON_BEDROCK_TEXT'] },
      },
    });
    vectorIndex.addDependency(vectorBucket);
    vectorIndex.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    const vectorIndexArn = vectorIndex.getAtt('IndexArn').toString();
    const vectorBucketArn = vectorBucket.getAtt('VectorBucketArn').toString();
    const embeddingModelArn = `arn:aws:bedrock:${region}::foundation-model/${props.models.embedding}`;

    const kbRole = new iam.Role(this, 'KnowledgeBaseRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock:${region}:${account}:knowledge-base/*` },
        },
      }),
      description: `AI-Q ${runId}: Bedrock Knowledge Base service role`,
    });
    kbRole.addToPolicy(new iam.PolicyStatement({
      sid: 'Embed',
      actions: ['bedrock:InvokeModel'],
      resources: [embeddingModelArn],
    }));
    kbRole.addToPolicy(new iam.PolicyStatement({
      sid: 'VectorIndex',
      actions: [
        's3vectors:GetIndex', 's3vectors:QueryVectors', 's3vectors:PutVectors', 's3vectors:GetVectors',
        's3vectors:DeleteVectors', 's3vectors:ListVectors',
      ],
      resources: [vectorIndexArn, vectorBucketArn],
    }));
    kbRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadDocuments',
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [this.artifacts.bucketArn, `${this.artifacts.bucketArn}/documents/*`],
      conditions: { StringEquals: { 'aws:ResourceAccount': account } },
    }));
    const knowledgeBase = new cdk.CfnResource(this, 'KnowledgeBase', {
      type: 'AWS::Bedrock::KnowledgeBase',
      properties: {
        Name: `${prefix}-kb`,
        Description: `AI-Q ${runId}: per-tenant document collections (filter on tenant_key/collection)`,
        RoleArn: kbRole.roleArn,
        KnowledgeBaseConfiguration: {
          Type: 'VECTOR',
          VectorKnowledgeBaseConfiguration: {
            EmbeddingModelArn: embeddingModelArn,
            EmbeddingModelConfiguration: {
              BedrockEmbeddingModelConfiguration: { Dimensions: EMBEDDING_DIMENSIONS, EmbeddingDataType: 'FLOAT32' },
            },
          },
        },
        StorageConfiguration: {
          Type: 'S3_VECTORS',
          S3VectorsConfiguration: { IndexArn: vectorIndexArn },
        },
      },
    });
    knowledgeBase.addDependency(vectorIndex);
    knowledgeBase.node.addDependency(kbRole);
    knowledgeBase.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    this.knowledgeBaseId = knowledgeBase.getAtt('KnowledgeBaseId').toString();
    const knowledgeBaseArn = knowledgeBase.getAtt('KnowledgeBaseArn').toString();
    const dataSource = new cdk.CfnResource(this, 'DocumentsDataSource', {
      type: 'AWS::Bedrock::DataSource',
      properties: {
        KnowledgeBaseId: this.knowledgeBaseId,
        Name: 'documents',
        Description: 'Tenant documents under documents/<tenant_key>/<collection>/ with .metadata.json sidecars',
        DataDeletionPolicy: 'DELETE',
        DataSourceConfiguration: {
          Type: 'S3',
          S3Configuration: { BucketArn: this.artifacts.bucketArn, InclusionPrefixes: ['documents/'] },
        },
        VectorIngestionConfiguration: {
          ChunkingConfiguration: {
            ChunkingStrategy: 'FIXED_SIZE',
            FixedSizeChunkingConfiguration: { MaxTokens: 400, OverlapPercentage: 15 },
          },
        },
      },
    });
    dataSource.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    this.dataSourceId = dataSource.getAtt('DataSourceId').toString();

    // ── Runtime execution role ────────────────────────────────────────────
    const runtimeRole = new iam.Role(this, 'RuntimeRole', {
      roleName: `${prefix}-runtime-role`,
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${region}:${account}:*` },
        },
      }),
      description: `AI-Q ${runId}: AgentCore Runtime execution role`,
    });
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EcrPull',
      actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability'],
      resources: [this.repository.repositoryArn],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({ sid: 'EcrAuth', actions: ['ecr:GetAuthorizationToken'], resources: ['*'] }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'RuntimeLogs',
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
      resources: [`arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({ sid: 'DescribeLogGroups', actions: ['logs:DescribeLogGroups'], resources: [`arn:aws:logs:${region}:${account}:log-group:*`] }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'Telemetry',
      actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
      resources: ['*'],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'Metrics',
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': ['bedrock-agentcore', 'AIQ/AgentCore'] } },
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'WorkloadIdentity',
      actions: ['bedrock-agentcore:GetWorkloadAccessToken', 'bedrock-agentcore:GetWorkloadAccessTokenForJWT', 'bedrock-agentcore:GetWorkloadAccessTokenForUserId'],
      resources: [
        `arn:aws:bedrock-agentcore:${region}:${account}:workload-identity-directory/default`,
        `arn:aws:bedrock-agentcore:${region}:${account}:workload-identity-directory/default/workload-identity/${runtimeName}-*`,
      ],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockInference',
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream', 'bedrock:Converse', 'bedrock:ConverseStream'],
      resources: [
        'arn:aws:bedrock:*::foundation-model/*',
        `arn:aws:bedrock:${region}:${account}:inference-profile/*`,
      ],
    }));
    // First invocation of a third-party (Anthropic) model in an account triggers an automatic AWS Marketplace
    // subscription; Bedrock requires the *invoking* role to hold these two actions or it fails with
    // "Your AWS Marketplace subscription for this model cannot be completed" (observed live 2026-09-15). The actions
    // do not support resource-level scoping; restrict further with aws-marketplace:ProductId once the model product
    // ids are pinned (follow-up). Reference: repost.aws/knowledge-center/bedrock-resolve-marketplace-permission
    // Phase 3 — Mantle lanes (ADR-29): the runtime mints a short-term Bedrock API key from its own role
    // (bedrock:CallWithBearerToken is what the presigned token authorises) and calls bedrock-mantle with it.
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockMantleLanes',
      actions: ['bedrock:CallWithBearerToken', 'bedrock-mantle:CallWithBearerToken', 'bedrock-mantle:InvokeModel',
        'bedrock-mantle:InvokeModelWithResponseStream', 'bedrock-mantle:ListFoundationModels'],
      resources: ['*'],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockModelActivation',
      actions: ['aws-marketplace:ViewSubscriptions', 'aws-marketplace:Subscribe'],
      resources: ['*'],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'KnowledgeBase',
      actions: ['bedrock:Retrieve', 'bedrock:StartIngestionJob', 'bedrock:GetIngestionJob', 'bedrock:ListIngestionJobs', 'bedrock:GetKnowledgeBase'],
      resources: [knowledgeBaseArn],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'WebSearchGateway',
      actions: ['bedrock-agentcore:InvokeGateway'],
      resources: [this.gatewayArn],
    }));
    this.jobsTable.grantReadWriteData(runtimeRole);
    this.eventsTable.grantReadWriteData(runtimeRole);
    this.artifacts.grantReadWrite(runtimeRole, 'tenants/*');
    this.artifacts.grantReadWrite(runtimeRole, 'documents/*');
    this.artifacts.grantDelete(runtimeRole, 'documents/*');
    this.artifacts.grantRead(runtimeRole, 'builds/*');

    // AgentCore Browser (page loader) and Code Interpreter (sandboxed skills). The AWS-managed
    // defaults (aws.browser.v1 / aws.codeinterpreter.v1) live under the "aws" account namespace;
    // sessions are sub-resources of the browser/code-interpreter ARNs. Proven pattern from the
    // fleet's PersonalAssistant/ContentOps runtimes.
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AgentCoreBuiltinSandboxTools',
      actions: [
        'bedrock-agentcore:StartBrowserSession', 'bedrock-agentcore:StopBrowserSession',
        'bedrock-agentcore:GetBrowserSession', 'bedrock-agentcore:ListBrowserSessions',
        'bedrock-agentcore:ConnectBrowserAutomationStream', 'bedrock-agentcore:UpdateBrowserStream',
        'bedrock-agentcore:GetBrowser', 'bedrock-agentcore:ListBrowsers',
        'bedrock-agentcore:StartCodeInterpreterSession', 'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession', 'bedrock-agentcore:GetCodeInterpreterSession',
        'bedrock-agentcore:ListCodeInterpreterSessions', 'bedrock-agentcore:GetCodeInterpreter',
        'bedrock-agentcore:ListCodeInterpreters',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:${region}:aws:browser/*`,
        `arn:aws:bedrock-agentcore:${region}:aws:code-interpreter/*`,
        `arn:aws:bedrock-agentcore:${region}:${account}:browser/*`,
        `arn:aws:bedrock-agentcore:${region}:${account}:browser-custom/*`,
        `arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*`,
        `arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter-custom/*`,
      ],
    }));

    // ── Amazon Bedrock Guardrail (native replacement for AI-Q's optional NeMo Guardrails) ──
    // Off by default for this workload; operators opt in per deployment (-c guardrailMode=audit|enforce) and tune each
    // filter's strength (-c guardrailPromptAttack=… -c guardrailHate=… …). 'audit' assesses and journals without blocking.
    const guardrailMode: GuardrailMode = props.guardrailMode ?? (props.guardrail === true ? 'enforce' : 'off');
    if (!GUARDRAIL_MODES.includes(guardrailMode)) {
      throw new Error(`guardrailMode must be one of ${GUARDRAIL_MODES.join('|')} (got ${guardrailMode})`);
    }
    const guardrailEnv: Record<string, string> = { AIQ_GUARDRAIL_MODE: guardrailMode };
    if (guardrailMode !== 'off') {
      const strengths: Record<GuardrailFilterType, GuardrailStrength> = { ...DEFAULT_GUARDRAIL_FILTERS, ...(props.guardrailFilters ?? {}) };
      for (const [type, strength] of Object.entries(strengths)) {
        if (!GUARDRAIL_STRENGTHS.includes(strength)) {
          throw new Error(`guardrail filter ${type} strength must be one of ${GUARDRAIL_STRENGTHS.join('|')} (got ${strength})`);
        }
      }
      const filters = (Object.keys(strengths) as GuardrailFilterType[])
        .filter((type) => strengths[type] !== 'NONE')
        .map((type) => ({ Type: type, InputStrength: strengths[type], OutputStrength: type === 'PROMPT_ATTACK' ? 'NONE' : strengths[type] }));
      if (filters.length === 0) throw new Error(`guardrailMode=${guardrailMode} needs at least one filter strength above NONE`);
      const guardrail = new cdk.CfnResource(this, 'Guardrail', {
        type: 'AWS::Bedrock::Guardrail',
        properties: {
          Name: `${prefix}-guardrail`,
          Description: `AI-Q ${runId}: ${guardrailMode} mode; ${filters.map((f) => `${f.Type}=${f.InputStrength}`).join(',')}`,
          BlockedInputMessaging: 'This research request was blocked by the content policy. Rephrase it, or ask the operator to lower the filter strength or switch the guardrail to audit mode.',
          BlockedOutputsMessaging: 'Part of this report was withheld by the content policy.',
          ContentPolicyConfig: { FiltersConfig: filters },
          Tags: [{ Key: 'aiq:run-id', Value: runId }, { Key: 'aiq:guardrail-mode', Value: guardrailMode }],
        },
      });
      guardrail.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
      const guardrailVersion = new cdk.CfnResource(this, 'GuardrailVersion', {
        type: 'AWS::Bedrock::GuardrailVersion',
        properties: { GuardrailIdentifier: guardrail.getAtt('GuardrailId').toString(), Description: `${guardrailMode} for AI-Q on AgentCore` },
      });
      runtimeRole.addToPolicy(new iam.PolicyStatement({
        sid: 'ApplyGuardrail',
        actions: ['bedrock:ApplyGuardrail'],
        resources: [guardrail.getAtt('GuardrailArn').toString()],
      }));
      guardrailEnv.AIQ_GUARDRAIL_ID = guardrail.getAtt('GuardrailId').toString();
      guardrailEnv.AIQ_GUARDRAIL_VERSION = guardrailVersion.getAtt('Version').toString();
      new cdk.CfnOutput(this, 'GuardrailId', { value: guardrail.getAtt('GuardrailId').toString() });
    }
    new cdk.CfnOutput(this, 'GuardrailMode', { value: guardrailMode });

    // ── Stale-job reaper: marks jobs whose runtime session died as failed (heartbeat > 10 min old) ──
    const reaper = new lambda.Function(this, 'StaleJobReaper', {
      functionName: `${prefix}-reaper`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'aiq', 'reaper'), { exclude: ['tests', '__pycache__'] }),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      environment: {
        JOBS_TABLE: this.jobsTable.tableName,
        EVENTS_TABLE: this.eventsTable.tableName,
        STALE_AFTER_SECONDS: '600',
        TTL_SECONDS: String(retentionDays * 86400),
      },
      logRetention: logs.RetentionDays.TWO_WEEKS,
      description: `AI-Q ${runId}: fails jobs with a stale heartbeat (lost runtime session)`,
    });
    this.jobsTable.grantReadWriteData(reaper);
    this.eventsTable.grantWriteData(reaper);
    new events.Rule(this, 'ReaperSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(reaper)],
      description: `AI-Q ${runId}: stale-job reaper every 5 minutes`,
    });
    new cdk.CfnOutput(this, 'ReaperFunction', { value: reaper.functionName });

    // ── Runtime (phase 2: requires a built image) ─────────────────────────
    let workbench: AiqWorkbench | undefined;
    const allowedClients: string[] = [...props.allowedClientIds];
    if (props.workbench) {
      const pool = cognito.UserPool.fromUserPoolId(this, 'OwuiPool', props.userPoolId);
      workbench = new AiqWorkbench(this, 'Workbench', {
        runId, userPool: pool, region: region!, cognitoDomainPrefix: props.workbench.cognitoDomainPrefix, owuiUrl: props.workbench.owuiUrl,
        runtimeArn: props.workbench.runtimeArnHint ?? '',
        domainName: props.workbench.domainName, certificateArn: props.workbench.certificateArn,
        hostedZoneId: props.workbench.hostedZoneId, hostedZoneName: props.workbench.hostedZoneName, distDir: props.workbench.distDir,
      });
      allowedClients.push(workbench.client.userPoolClientId);
    }

    const imageRef = props.imageDigest
      ? `${this.repository.repositoryUri}@${props.imageDigest}`
      : props.imageTag ? `${this.repository.repositoryUri}:${props.imageTag}` : undefined;
    if (imageRef) {
      const discoveryUrl = `https://cognito-idp.${region}.amazonaws.com/${props.userPoolId}/.well-known/openid-configuration`;
      const issuer = `https://cognito-idp.${region}.amazonaws.com/${props.userPoolId}`;
      const runtime = new cdk.CfnResource(this, 'Runtime', {
        type: 'AWS::BedrockAgentCore::Runtime',
        properties: {
          AgentRuntimeName: runtimeName,
          Description: `AI-Q research agents (NVIDIA AI-Q Blueprint on Bedrock) for run ${runId}`,
          AgentRuntimeArtifact: { ContainerConfiguration: { ContainerUri: imageRef } },
          RoleArn: runtimeRole.roleArn,
          NetworkConfiguration: { NetworkMode: 'PUBLIC' },
          ProtocolConfiguration: 'HTTP',
          AuthorizerConfiguration: {
            CustomJWTAuthorizer: { DiscoveryUrl: discoveryUrl, AllowedClients: allowedClients },
          },
          RequestHeaderConfiguration: { RequestHeaderAllowlist: ['Authorization'] },
          // Sessions stay alive while a deep-research job is running (HealthyBusy);
          // idle sessions are reclaimed after 30 min; hard cap 8 hours.
          LifecycleConfiguration: { IdleRuntimeSessionTimeout: 1800, MaxLifetime: 28800 },
          EnvironmentVariables: {
            AIQ_RUN_ID: runId,
            AIQ_REGION: region,
            AIQ_JOBS_TABLE: this.jobsTable.tableName,
            AIQ_EVENTS_TABLE: this.eventsTable.tableName,
            AIQ_ARTIFACTS_BUCKET: this.artifacts.bucketName,
            AIQ_GATEWAY_URL: this.gatewayUrl,
            AIQ_WEB_SEARCH_TOOL: WEB_SEARCH_TOOL_NAME,
            AIQ_KB_ID: this.knowledgeBaseId,
            AIQ_KB_DATA_SOURCE_ID: this.dataSourceId,
            AIQ_JWT_ISSUER: issuer,
            AIQ_JWT_ALLOWED_CLIENTS: cdk.Fn.join(',', allowedClients),
            AIQ_WORKBENCH_URL: workbench?.url ?? '',
            AIQ_MATRIX_KEY: 'model-lab/matrix/latest.json',
            AIQ_MODEL_ROUTER: props.models.router,
            AIQ_MODEL_SHALLOW: props.models.shallow,
            AIQ_MODEL_PLANNER: props.models.planner,
            AIQ_MODEL_RESEARCHER: props.models.researcher,
            AIQ_MODEL_WRITER: props.models.writer,
            AIQ_RETENTION_DAYS: String(retentionDays),
            AIQ_ENFORCE_CITATIONS: props.enforceCitations === false ? 'false' : 'true',
            AIQ_FETCH_MAX_PAGES: String(props.fetchMaxPages ?? 12),
            AIQ_MAX_TOKENS_DEEP: String(props.maxTokensDeep ?? 16384),
            AIQ_MAX_TOKENS_WRITER: String(props.maxTokensWriter ?? 16384),
            ...guardrailEnv,
            ...(props.runtimeEnvironment ?? {}),
          },
        },
      });
      runtime.node.addDependency(runtimeRole);
      runtime.node.addDependency(searchTarget);
      runtime.node.addDependency(dataSource);
      this.runtimeArn = runtime.getAtt('AgentRuntimeArn').toString();
      this.runtimeId = runtime.getAtt('AgentRuntimeId').toString();
      new cdk.CfnOutput(this, 'RuntimeArn', { value: this.runtimeArn });
      new cdk.CfnOutput(this, 'RuntimeId', { value: this.runtimeId });
      new cdk.CfnOutput(this, 'RuntimeImage', { value: imageRef });
      new cdk.CfnOutput(this, 'RuntimeName', { value: runtimeName });
    }

    new cdk.CfnOutput(this, 'GatewayUrl', { value: this.gatewayUrl });
    new cdk.CfnOutput(this, 'GatewayArn', { value: this.gatewayArn });
    new cdk.CfnOutput(this, 'WebSearchTool', { value: WEB_SEARCH_TOOL_NAME });
    new cdk.CfnOutput(this, 'JobsTable', { value: this.jobsTable.tableName });
    new cdk.CfnOutput(this, 'EventsTable', { value: this.eventsTable.tableName });
    new cdk.CfnOutput(this, 'ArtifactsBucket', { value: this.artifacts.bucketName });
    new cdk.CfnOutput(this, 'RepositoryUri', { value: this.repository.repositoryUri });
    new cdk.CfnOutput(this, 'ImageBuildProject', { value: imageBuild.projectName });
    new cdk.CfnOutput(this, 'KnowledgeBaseId', { value: this.knowledgeBaseId });
    new cdk.CfnOutput(this, 'DataSourceId', { value: this.dataSourceId });
    new cdk.CfnOutput(this, 'RuntimeRoleArn', { value: runtimeRole.roleArn });
  }
}

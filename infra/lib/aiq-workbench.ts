// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

export interface AiqWorkbenchProps {
  readonly runId: string;
  /** Existing Cognito user pool (the Open WebUI pool) — the Workbench gets its own PUBLIC (PKCE) app client on it. */
  readonly userPool: cognito.IUserPool;
  /** Cognito Managed Login domain prefix of that pool (e.g. `open-webui-<account>`), for sign-out. */
  readonly cognitoDomainPrefix: string;
  readonly region: string;
  /** AgentCore Runtime ARN the SPA calls directly (CORS verified on the data plane). */
  readonly runtimeArn: string;
  readonly owuiUrl: string;
  /** Optional custom hostname (needs certificateArn in us-east-1 + a public hosted zone). */
  readonly domainName?: string;
  readonly certificateArn?: string;
  readonly hostedZoneId?: string;
  readonly hostedZoneName?: string;
  /** Built SPA directory (aiq/workbench/dist). */
  readonly distDir: string;
}

/**
 * Research Workbench — static SPA behind CloudFront with Origin Access Control (never a public bucket; ADR-19/25).
 * Same Cognito pool as Open WebUI → one identity; the SPA holds tokens in sessionStorage (PKCE, no secret) and
 * calls the AgentCore Runtime data plane directly with the user's access token.
 */
export class AiqWorkbench extends Construct {
  public readonly url: string;
  public readonly client: cognito.UserPoolClient;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: AiqWorkbenchProps) {
    super(scope, id);
    if (!fs.existsSync(path.join(props.distDir, 'index.html'))) {
      throw new Error(`Workbench build not found at ${props.distDir} — run (cd aiq/workbench && npm run build) first`);
    }
    const bucket = new s3.Bucket(this, 'Bucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const rewrite = new cloudfront.Function(this, 'SpaRewrite', {
      comment: 'AI-Q Workbench: route extensionless SPA paths to /index.html',
      code: cloudfront.FunctionCode.fromInline(`function handler(event) {
  var req = event.request; var uri = req.uri;
  if (uri.indexOf('.') === -1 || uri.endsWith('/')) { req.uri = '/index.html'; }
  return req;
}`),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });
    const headers = new cloudfront.ResponseHeadersPolicy(this, 'Headers', {
      comment: 'AI-Q Workbench security headers',
      securityHeadersBehavior: {
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, override: true },
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob: https://*.amazonaws.com",
            `connect-src 'self' https://bedrock-agentcore.${props.region}.amazonaws.com https://cognito-idp.${props.region}.amazonaws.com https://${props.cognitoDomainPrefix}.auth.${props.region}.amazoncognito.com https://*.s3.${props.region}.amazonaws.com https://*.s3.amazonaws.com`,
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self' https://*.amazoncognito.com",
          ].join('; '),
        },
      },
    });
    const useDomain = !!(props.domainName && props.certificateArn && props.hostedZoneId && props.hostedZoneName);
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `AI-Q Research Workbench (${props.runId})`,
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: headers,
        functionAssociations: [{ function: rewrite, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }],
      },
      errorResponses: [{ httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html', ttl: cdk.Duration.seconds(0) }],
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      ...(useDomain ? {
        domainNames: [props.domainName!],
        certificate: acm.Certificate.fromCertificateArn(this, 'Cert', props.certificateArn!),
      } : {}),
    });
    this.url = useDomain ? `https://${props.domainName}` : `https://${this.distribution.distributionDomainName}`;
    if (useDomain) {
      const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', { hostedZoneId: props.hostedZoneId!, zoneName: props.hostedZoneName! });
      new route53.ARecord(this, 'Alias', { zone, recordName: props.domainName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(this.distribution)) });
      new route53.AaaaRecord(this, 'AliasV6', { zone, recordName: props.domainName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(this.distribution)) });
    }
    // Public PKCE client on the shared pool: no secret, code flow only, callback = the Workbench (one identity, ADR-25).
    this.client = new cognito.UserPoolClient(this, 'Client', {
      userPool: props.userPool,
      userPoolClientName: `aiq-${props.runId}-workbench`,
      generateSecret: false,
      authFlows: { userSrp: true },
      preventUserExistenceErrors: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: [`${this.url}/auth/callback`, 'http://localhost:5173/auth/callback'],
        logoutUrls: [`${this.url}/`, 'http://localhost:5173/'],
      },
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });
    // The pool uses Cognito Managed Login (branding v2): every app client needs a branding style or the hosted sign-in
    // page answers "Login pages unavailable". Cognito-provided defaults are enough (verified live 2026-09-18).
    new cognito.CfnManagedLoginBranding(this, 'Branding', {
      userPoolId: props.userPool.userPoolId,
      clientId: this.client.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    const config = {
      region: props.region,
      userPoolId: props.userPool.userPoolId,
      clientId: this.client.userPoolClientId,
      cognitoDomain: `${props.cognitoDomainPrefix}.auth.${props.region}.amazoncognito.com`,
      runtimeArn: props.runtimeArn,
      owuiUrl: props.owuiUrl,
      workbenchUrl: this.url,
      runId: props.runId,
      mock: false,
    };
    new s3deploy.BucketDeployment(this, 'Deploy', {
      destinationBucket: bucket,
      distribution: this.distribution,
      distributionPaths: ['/*'],
      sources: [
        s3deploy.Source.asset(props.distDir),
        s3deploy.Source.jsonData('config.json', config),
      ],
      prune: true,
      memoryLimit: 512,
    });
    const stack = cdk.Stack.of(this);
    new cdk.CfnOutput(stack, 'WorkbenchUrl', { value: this.url });
    new cdk.CfnOutput(stack, 'WorkbenchClientId', { value: this.client.userPoolClientId, description: 'Cognito app client (PKCE, no secret) used by the Workbench' });
    new cdk.CfnOutput(stack, 'WorkbenchDistributionId', { value: this.distribution.distributionId });
  }
}

import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as resolver from 'aws-cdk-lib/aws-route53resolver';
import { Construct } from 'constructs';

export interface WebBrowserIsolationProps {
  readonly availabilityZoneId: string;
}

/**
 * Network-isolated, curated trusted synthetic admin canary, not a general hostile
 * public-user security boundary. Trusts the standard AWS Browser sandbox: VPC
 * security groups do not control microVM loopback or browser-process compromise.
 * Rendering must use brokered route.fulfill responses, never direct networking.
 * The separate broker must deny queries and arbitrary public pages, permitting
 * only exact vetted reading endpoints. This construct does not implement it.
 *
 * Supply a Browser-supported AZ ID in the stack region (e.g. use1-az1 in us-east-1).
 * Deployment needs AWSServiceRoleForBedrockAgentCoreNetwork or permission for
 * AgentCore to create it; no browser execution role or custom provider is added.
 *
 * AWS::Route53Resolver::FirewallConfig was NON_PROVISIONABLE with read/list-only
 * handlers in the us-east-1 registry on 2026-09-15. A fresh VPC uses the documented
 * FirewallFailOpen=DISABLED default. Verify GetFirewallConfig read-only before
 * starting a canary; this template cannot enforce that setting against drift.
 * https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-dns-firewall-vpc-configuration.html
 * https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-browsercustom.html
 */
export class WebBrowserIsolation extends Construct {
  public readonly browserId: string;
  public readonly browserArn: string;
  public readonly subnetIds: string[];
  public readonly securityGroupId: string;
  public readonly vpcId: string;

  constructor(scope: Construct, id: string, props: WebBrowserIsolationProps) {
    super(scope, id);

    const vpc = new ec2.CfnVPC(this, 'Vpc', {
      cidrBlock: '10.254.0.0/27',
      enableDnsSupport: true,
      enableDnsHostnames: true,
    });
    const subnet = new ec2.CfnSubnet(this, 'IsolatedSubnet', {
      vpcId: vpc.ref,
      cidrBlock: '10.254.0.0/28',
      availabilityZoneId: props.availabilityZoneId,
      mapPublicIpOnLaunch: false,
      assignIpv6AddressOnCreation: false,
    });
    const routeTable = new ec2.CfnRouteTable(this, 'IsolatedRouteTable', {
      vpcId: vpc.ref,
    });
    const routeAssociation = new ec2.CfnSubnetRouteTableAssociation(this, 'IsolatedRoutes', {
      subnetId: subnet.ref,
      routeTableId: routeTable.ref,
    });
    const vpcReference = ec2.Vpc.fromVpcAttributes(this, 'VpcReference', {
      vpcId: vpc.ref,
      availabilityZones: [subnet.attrAvailabilityZone],
    });
    const securityGroup = new ec2.SecurityGroup(this, 'BrowserSecurityGroup', {
      vpc: vpcReference,
      description: 'No ingress or usable egress for the broker-rendered admin canary',
      allowAllOutbound: false,
      allowAllIpv6Outbound: false,
    });
    const blockedDomains = new resolver.CfnFirewallDomainList(this, 'BlockedDomains', {
      domains: ['*'],
    });
    const firewall = new resolver.CfnFirewallRuleGroup(this, 'DnsFirewall', {
      firewallRules: [{
        action: 'BLOCK',
        blockResponse: 'NXDOMAIN',
        firewallDomainListId: blockedDomains.attrId,
        priority: 1,
      }],
    });
    const firewallAssociation = new resolver.CfnFirewallRuleGroupAssociation(this, 'DnsFirewallAssociation', {
      firewallRuleGroupId: firewall.attrId,
      vpcId: vpc.ref,
      priority: 101,
      mutationProtection: 'DISABLED',
    });
    const browser = new cdk.CfnResource(this, 'Browser', {
      type: 'AWS::BedrockAgentCore::BrowserCustom',
      properties: {
        Name: 'OpenWebUIWebCanary',
        NetworkConfiguration: {
          NetworkMode: 'VPC',
          VpcConfig: {
            Subnets: [subnet.ref],
            SecurityGroups: [securityGroup.securityGroupId],
          },
        },
        RecordingConfig: { Enabled: false },
      },
    });
    browser.addResourceDependency(firewallAssociation);
    browser.addResourceDependency(routeAssociation);

    this.browserId = browser.getAtt('BrowserId').toString();
    this.browserArn = browser.getAtt('BrowserArn').toString();
    this.subnetIds = [subnet.ref];
    this.securityGroupId = securityGroup.securityGroupId;
    this.vpcId = vpc.ref;
  }
}

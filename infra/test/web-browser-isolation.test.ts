import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { WebBrowserIsolation } from '../lib/web-browser-isolation';

function fixture() {
  const app = new cdk.App({ context: { '@aws-cdk/aws-ec2:restrictDefaultSecurityGroup': true } });
  const stack = new cdk.Stack(app, 'BrowserIsolationTest', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  const isolation = new WebBrowserIsolation(stack, 'Isolation', { availabilityZoneId: 'use1-az1' });
  const template = Template.fromStack(stack);
  return { stack, isolation, template };
}

test('creates only a tiny IPv4 VPC and one explicitly isolated supported-AZ subnet', () => {
  const { template } = fixture();
  template.resourceCountIs('AWS::EC2::VPC', 1);
  template.resourceCountIs('AWS::EC2::Subnet', 1);
  template.hasResourceProperties('AWS::EC2::VPC', {
    CidrBlock: '10.254.0.0/27', EnableDnsSupport: true, EnableDnsHostnames: true,
  });
  const vpcId = Object.keys(template.findResources('AWS::EC2::VPC'))[0];
  const subnetId = Object.keys(template.findResources('AWS::EC2::Subnet'))[0];
  template.hasResourceProperties('AWS::EC2::Subnet', {
    VpcId: { Ref: vpcId }, CidrBlock: '10.254.0.0/28', AvailabilityZoneId: 'use1-az1',
    MapPublicIpOnLaunch: false, AssignIpv6AddressOnCreation: false,
  });
  template.resourceCountIs('AWS::EC2::RouteTable', 1);
  template.hasResourceProperties('AWS::EC2::RouteTable', { VpcId: { Ref: vpcId } });
  const routeTableId = Object.keys(template.findResources('AWS::EC2::RouteTable'))[0];
  template.hasResourceProperties('AWS::EC2::SubnetRouteTableAssociation', {
    SubnetId: { Ref: subnetId }, RouteTableId: { Ref: routeTableId },
  });
  const serialized = JSON.stringify(template.toJSON());
  expect(serialized).not.toMatch(/Fn::GetAZs|Fn::ImportValue|Ipv6CidrBlock|0\.0\.0\.0\/0|::\/0/);
});

test('SG has no ingress and CDK dummy egress rather than EC2 implicit allow-all', () => {
  const { template } = fixture();
  template.resourceCountIs('AWS::EC2::SecurityGroup', 1);
  const securityGroup = Object.values(template.findResources('AWS::EC2::SecurityGroup'))[0];
  expect(securityGroup.Properties.SecurityGroupIngress ?? []).toEqual([]);
  expect(securityGroup.Properties.SecurityGroupEgress).toEqual([{
    CidrIp: '255.255.255.255/32', Description: 'Disallow all traffic',
    IpProtocol: 'icmp', FromPort: 252, ToPort: 86,
  }]);
  const vpcId = Object.keys(template.findResources('AWS::EC2::VPC'))[0];
  expect(securityGroup.Properties.VpcId).toEqual({ Ref: vpcId });
  template.resourceCountIs('AWS::EC2::SecurityGroupIngress', 0);
  template.resourceCountIs('AWS::EC2::SecurityGroupEgress', 0);
});

test('blocks all DNS with no allow exceptions and associates the policy before Browser creation', () => {
  const { template } = fixture();
  template.resourceCountIs('AWS::Route53Resolver::FirewallDomainList', 1);
  template.hasResourceProperties('AWS::Route53Resolver::FirewallDomainList', { Domains: ['*'] });
  const domainId = Object.keys(template.findResources('AWS::Route53Resolver::FirewallDomainList'))[0];
  template.resourceCountIs('AWS::Route53Resolver::FirewallRuleGroup', 1);
  template.hasResourceProperties('AWS::Route53Resolver::FirewallRuleGroup', {
    FirewallRules: [{
      Action: 'BLOCK', BlockResponse: 'NXDOMAIN', Priority: 1,
      FirewallDomainListId: { 'Fn::GetAtt': [domainId, 'Id'] },
    }],
  });
  const firewallId = Object.keys(template.findResources('AWS::Route53Resolver::FirewallRuleGroup'))[0];
  const vpcId = Object.keys(template.findResources('AWS::EC2::VPC'))[0];
  template.resourceCountIs('AWS::Route53Resolver::FirewallRuleGroupAssociation', 1);
  template.hasResourceProperties('AWS::Route53Resolver::FirewallRuleGroupAssociation', {
    FirewallRuleGroupId: { 'Fn::GetAtt': [firewallId, 'Id'] },
    VpcId: { Ref: vpcId }, Priority: 101, MutationProtection: 'DISABLED',
  });
  const associationId = Object.keys(template.findResources('AWS::Route53Resolver::FirewallRuleGroupAssociation'))[0];
  const routeAssociationId = Object.keys(template.findResources('AWS::EC2::SubnetRouteTableAssociation'))[0];
  const browser = Object.values(template.findResources('AWS::BedrockAgentCore::BrowserCustom'))[0];
  expect(browser.DependsOn).toEqual(expect.arrayContaining([associationId, routeAssociationId]));
  template.resourceCountIs('AWS::Route53Resolver::FirewallConfig', 0);
});

test('uses the verified BrowserCustom schema without role, signing, storage or recording', () => {
  const { stack, isolation, template } = fixture();
  template.resourceCountIs('AWS::BedrockAgentCore::BrowserCustom', 1);
  const [browserId, browser] = Object.entries(template.findResources('AWS::BedrockAgentCore::BrowserCustom'))[0];
  expect(browser.Properties).toEqual({
    Name: 'OpenWebUIWebCanary',
    NetworkConfiguration: {
      NetworkMode: 'VPC',
      VpcConfig: {
        Subnets: stack.resolve(isolation.subnetIds),
        SecurityGroups: [stack.resolve(isolation.securityGroupId)],
      },
    },
    RecordingConfig: { Enabled: false },
  });
  expect(stack.resolve(isolation.browserId)).toEqual({ 'Fn::GetAtt': [browserId, 'BrowserId'] });
  expect(stack.resolve(isolation.browserArn)).toEqual({ 'Fn::GetAtt': [browserId, 'BrowserArn'] });
  expect(stack.resolve(isolation.vpcId)).toEqual({ Ref: Object.keys(template.findResources('AWS::EC2::VPC'))[0] });
  expect(stack.resolve(isolation.subnetIds)).toEqual([{ Ref: Object.keys(template.findResources('AWS::EC2::Subnet'))[0] }]);
  const securityGroupId = Object.keys(template.findResources('AWS::EC2::SecurityGroup'))[0];
  expect(stack.resolve(isolation.securityGroupId)).toEqual({ 'Fn::GetAtt': [securityGroupId, 'GroupId'] });
});

test('contains no routes, gateways, endpoints, providers, IAM roles, or unrelated infrastructure', () => {
  const { template } = fixture();
  const types = Object.values(template.toJSON().Resources).map((resource: any) => resource.Type);
  expect(types.sort()).toEqual([
    'AWS::BedrockAgentCore::BrowserCustom',
    'AWS::EC2::RouteTable',
    'AWS::EC2::SecurityGroup',
    'AWS::EC2::Subnet',
    'AWS::EC2::SubnetRouteTableAssociation',
    'AWS::EC2::VPC',
    'AWS::Route53Resolver::FirewallDomainList',
    'AWS::Route53Resolver::FirewallRuleGroup',
    'AWS::Route53Resolver::FirewallRuleGroupAssociation',
  ].sort());
});

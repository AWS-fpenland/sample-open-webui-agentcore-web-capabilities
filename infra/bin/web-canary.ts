import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { WebCanaryStack } from '../lib/web-canary-stack';

const app = new cdk.App();
if (app.node.tryGetContext('webCanary') !== 'on') {
  throw new Error('The standalone functional canary is opt-in: -c webCanary=on');
}
const configPath = app.node.tryGetContext('canaryConfigPath');
if (typeof configPath !== 'string' || !path.isAbsolute(configPath)) {
  throw new Error('Pass an absolute private canaryConfigPath with verified resource facts');
}
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
new WebCanaryStack(app, 'OpenWebUI-WebCanary', {
  env: { account: config.account, region: config.region },
  adapterAssetPath: config.adapterAssetPath,
  gatewayArn: config.gatewayArn,
  gatewayUrl: config.gatewayUrl,
  taskRoleArn: config.taskRoleArn,
  subjects: config.subjects,
  availabilityZoneId: config.availabilityZoneId,
  searchEnabled: config.searchEnabled === true,
  fetchEnabled: config.fetchEnabled === true,
  browserEnabled: config.browserEnabled === true,
});

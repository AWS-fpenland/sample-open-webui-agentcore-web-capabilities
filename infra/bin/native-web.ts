import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { NativeWebStack } from '../lib/native-web-stack';

const app = new cdk.App();
if (app.node.tryGetContext('nativeWeb') !== 'on') {
  throw new Error('The standalone native web adapter is opt-in: -c nativeWeb=on');
}
const configPath = app.node.tryGetContext('nativeWebConfigPath');
if (typeof configPath !== 'string' || !path.isAbsolute(configPath)) {
  throw new Error('Pass an absolute private nativeWebConfigPath with verified resource facts');
}
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
new NativeWebStack(app, 'OpenWebUI-NativeWeb', {
  env: { account: config.account, region: config.region },
  adapterAssetPath: config.adapterAssetPath,
  gatewayArn: config.gatewayArn,
  gatewayUrl: config.gatewayUrl,
  browserArn: config.browserArn,
  browserId: config.browserId,
  quotaTableArn: config.quotaTableArn,
  quotaTableName: config.quotaTableName,
  searchEnabled: config.searchEnabled,
  fetchEnabled: config.fetchEnabled,
  browserEnabled: config.browserEnabled,
  browserNetworkPolicyReady: config.browserNetworkPolicyReady,
  alarmsEnabled: config.alarmsEnabled,
});

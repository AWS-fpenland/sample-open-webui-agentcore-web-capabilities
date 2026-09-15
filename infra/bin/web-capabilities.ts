import * as cdk from 'aws-cdk-lib';
import { WebCapabilitiesStack } from '../lib/web-capabilities-stack';

const app = new cdk.App();
if (app.node.tryGetContext('webCapabilities') !== 'on') {
  throw new Error('Standalone search is opt-in: pass -c webCapabilities=on');
}

new WebCapabilitiesStack(app, 'OpenWebUI-WebCapabilities', {
  env: {
    account: app.node.tryGetContext('account'),
    region: app.node.tryGetContext('region'),
  },
  providerAssetPath: app.node.tryGetContext('providerAssetPath'),
});

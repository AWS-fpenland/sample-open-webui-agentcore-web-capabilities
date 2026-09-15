#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Supported deployment path for "AI-Q on AgentCore" (additive, run-scoped, idempotent):
#   phase 1  cdk deploy aiq-<runId>            (everything except the Runtime)
#   build    aiq/scripts/build-image.sh        (CodeBuild, ARM64, from the committed tree)
#   phase 2  cdk deploy aiq-<runId> -c imageDigest=sha256:...   (adds/updates the Runtime, pinned by digest)
#
# Usage: aiq/scripts/deploy.sh <runId> <userPoolId> <allowedClientIds,comma> [--engine aiq|mock] [--skip-build]
# Requires: AWS credentials in the environment (explicit account/region resolved from STS), node deps in infra/.
set -euo pipefail
RUN_ID="${1:?runId}"; POOL="${2:?userPoolId}"; CLIENTS="${3:?allowedClientIds}"; shift 3
ENGINE="aiq"; SKIP_BUILD=0
while [[ $# -gt 0 ]]; do case "$1" in --engine) ENGINE="$2"; shift 2;; --skip-build) SKIP_BUILD=1; shift;; *) echo "unknown $1" >&2; exit 2;; esac; done
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/infra"
export PATH="$ROOT/infra/node_modules/.bin:$PATH" AWS_EC2_METADATA_DISABLED=true
CTX=(-c aiq=on -c runId="$RUN_ID" -c account="$ACCOUNT" -c region="$REGION" -c userPoolId="$POOL" -c allowedClients="$CLIENTS" -c engine="$ENGINE")
APP=(-a 'npx ts-node --prefer-ts-exts bin/aiq.ts' --output "cdk.out.aiq-$RUN_ID" --no-lookups --require-approval never --progress events)
echo "== phase 1: aiq-$RUN_ID in $ACCOUNT/$REGION (engine=$ENGINE)"
npx cdk deploy "${APP[@]}" "${CTX[@]}"
if [[ "$SKIP_BUILD" == "0" ]]; then
  echo "== build image (CodeBuild)"
  BUILD_OUT="$(bash "$ROOT/aiq/scripts/build-image.sh" "$RUN_ID" --wait | tee /dev/stderr)"
  TAG="$(echo "$BUILD_OUT" | sed -n 's/^IMAGE_TAG=//p' | tail -1)"
else
  TAG="${IMAGE_TAG:?set IMAGE_TAG when using --skip-build}"
fi
BUCKET="$(aws cloudformation describe-stacks --stack-name "aiq-$RUN_ID" --query "Stacks[0].Outputs[?OutputKey=='ArtifactsBucket'].OutputValue" --output text)"
DIGEST="$(aws s3 cp "s3://$BUCKET/builds/$TAG/build.json" - | uv run --no-project --quiet python -c 'import json,sys; print(json.load(sys.stdin)["digest"])')"
echo "== phase 2: runtime image $TAG @ $DIGEST"
npx cdk deploy "${APP[@]}" "${CTX[@]}" -c imageTag="$TAG" -c imageDigest="$DIGEST" --outputs-file "$ROOT/infra/cdk.out.aiq-$RUN_ID/outputs.json"
RT_ID="$(aws cloudformation describe-stacks --stack-name "aiq-$RUN_ID" --query "Stacks[0].Outputs[?OutputKey=='RuntimeId'].OutputValue" --output text)"
# Platform-created log group: set retention (idempotent; the group appears after the first session).
aws logs put-retention-policy --log-group-name "/aws/bedrock-agentcore/runtimes/$RT_ID-DEFAULT" --retention-in-days 14 2>/dev/null || true
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT_ID" --query '{status:status,version:agentRuntimeVersion,image:agentRuntimeArtifact.containerConfiguration.containerUri}' --output json

#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build the AI-Q on AgentCore runtime image REMOTELY with the run's CodeBuild
# project. Source of truth = the current git commit of this checkout: the
# tracked tree is zipped, uploaded to the stack's private bucket under
# source/<commit>.zip, and CodeBuild builds+pushes ECR :<commit>. The digest
# and provenance land in s3://<bucket>/builds/<tag>/build.json.
#
# Usage: aiq/scripts/build-image.sh <runId> [--wait]
# Requires: AWS credentials in the environment for the target account; git; zip.
set -euo pipefail
RUN_ID="${1:?runId required (e.g. fable51-79d40d45)}"; shift || true
WAIT=0; for a in "$@"; do [[ "$a" == "--wait" ]] && WAIT=1; done
REGION="${AWS_REGION:-us-east-1}"
STACK="aiq-${RUN_ID}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if [[ -n "$(git status --porcelain -- aiq infra/lib/aiq-stack.ts infra/bin/aiq.ts)" ]]; then
  echo "WARNING: uncommitted changes under aiq/ or infra/ — the image tag will carry a -dirty suffix" >&2
  DIRTY="-dirty"
else
  DIRTY=""
fi
COMMIT="$(git rev-parse --short=12 HEAD)"
FULL_COMMIT="$(git rev-parse HEAD)"
TAG="${COMMIT}${DIRTY}"
UPSTREAM_REF="$(grep -E '^AIQ_UPSTREAM_REF=' aiq/runtime/upstream.pin | cut -d= -f2)"
out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BUCKET="$(out ArtifactsBucket)"; PROJECT="$(out ImageBuildProject)"
[[ -n "$BUCKET" && -n "$PROJECT" ]] || { echo "stack $STACK has no ArtifactsBucket/ImageBuildProject outputs" >&2; exit 2; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Committed content only (HEAD), never node_modules/venvs/private files. Uncommitted edits are NOT built.
git archive --format=zip -o "$TMP/src.zip" HEAD aiq
aws s3 cp --only-show-errors "$TMP/src.zip" "s3://$BUCKET/source/$TAG.zip" --region "$REGION"
echo "source: s3://$BUCKET/source/$TAG.zip  tag: $TAG  upstream: $UPSTREAM_REF"
BUILD_ID="$(aws codebuild start-build --project-name "$PROJECT" --region "$REGION" \
  --source-location-override "$BUCKET/source/$TAG.zip" \
  --environment-variables-override name=IMAGE_TAG,value="$TAG",type=PLAINTEXT name=SOURCE_COMMIT,value="$FULL_COMMIT",type=PLAINTEXT name=AIQ_UPSTREAM_REF,value="$UPSTREAM_REF",type=PLAINTEXT \
  --query 'build.id' --output text)"
echo "codebuild: $BUILD_ID"
if [[ "$WAIT" == "1" ]]; then
  while :; do
    STATUS="$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" --query 'builds[0].buildStatus' --output text)"
    PHASE="$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" --query 'builds[0].currentPhase' --output text)"
    echo "$(date -u +%T) $STATUS $PHASE"
    case "$STATUS" in IN_PROGRESS) sleep 30;; SUCCEEDED) break;; *) echo "build $STATUS" >&2; exit 1;; esac
  done
  aws s3 cp --only-show-errors "s3://$BUCKET/builds/$TAG/build.json" - --region "$REGION"
fi
echo "IMAGE_TAG=$TAG"

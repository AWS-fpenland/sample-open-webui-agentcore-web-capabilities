#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# OPTIONAL teardown of everything AI-Q created for one run. NEVER runs automatically. Read before running.
#
#   aiq/scripts/teardown.sh <runId> <owuiUrl> <userPoolId> <owuiClientId> [--yes]
#
# Removes: the Open WebUI functions (aiq_agentcore pipe, aiq_actions) and their model rows (chats keep their content);
# the CDK stack aiq-<runId> (runtime, gateway, KB + vectors, tables, bucket incl. packages/, ECR, CodeBuild, reaper,
# Workbench distribution + bucket + Cognito app client + Route 53 alias); the synthetic Cognito users/group.
# Requires: AWS credentials for the deployment account; OWUI_TOKEN (admin session JWT) for the Open WebUI steps.
set -euo pipefail
RUN_ID="${1:?runId}"; OWUI="${2:?owuiUrl}"; POOL="${3:?userPoolId}"; CLIENT="${4:?owuiClientId}"; YES="${5:-}"
REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "About to tear down run ${RUN_ID} in ${REGION}: stack aiq-${RUN_ID}, Open WebUI functions, synthetic users."
if [[ "$YES" != "--yes" ]]; then read -r -p "Type the run id to confirm: " CONFIRM; [[ "$CONFIRM" == "$RUN_ID" ]] || { echo "aborted"; exit 1; }; fi
if [[ -n "${OWUI_TOKEN:-}" ]]; then
  for fid in aiq_actions aiq_agentcore; do
    curl -fsS -X DELETE -H "Authorization: Bearer $OWUI_TOKEN" "$OWUI/api/v1/functions/id/$fid/delete" && echo "deleted function $fid" || echo "function $fid not deleted (absent?)"
  done
  for m in auto shallow deep deep_clarify; do
    curl -fsS -X POST -H "Authorization: Bearer $OWUI_TOKEN" "$OWUI/api/v1/models/model/delete?id=aiq_agentcore.$m" >/dev/null 2>&1 && echo "deleted model row aiq_agentcore.$m" || true
  done
else
  echo "OWUI_TOKEN not set — skipping Open WebUI cleanup (deactivate/delete the functions in Admin → Functions)."
fi
cd "$ROOT/infra"
npx cdk destroy -a 'npx ts-node --prefer-ts-exts bin/aiq.ts' --force -c aiq=on -c runId="$RUN_ID" -c account="$(aws sts get-caller-identity --query Account --output text)" \
  -c region="$REGION" -c userPoolId="$POOL" -c allowedClients="$CLIENT" -c workbench=on 2>&1 | tail -20 || {
  echo "cdk destroy with workbench=on failed (dist missing?) — retrying without the workbench context"; \
  npx cdk destroy -a 'npx ts-node --prefer-ts-exts bin/aiq.ts' --force -c aiq=on -c runId="$RUN_ID" -c account="$(aws sts get-caller-identity --query Account --output text)" \
    -c region="$REGION" -c userPoolId="$POOL" -c allowedClients="$CLIENT" 2>&1 | tail -20; }
for u in a b; do
  aws cognito-idp admin-delete-user --user-pool-id "$POOL" --username "aiq-${RUN_ID}-user-${u}@aiq-test.invalid" --region "$REGION" 2>/dev/null && echo "deleted user ${u}" || true
done
aws cognito-idp delete-group --user-pool-id "$POOL" --group-name "aiq-${RUN_ID}-testers" --region "$REGION" 2>/dev/null && echo "deleted group" || true
echo "Remaining: Open WebUI users created by OIDC logins (Admin → Users), CloudWatch log groups (retention applies)."

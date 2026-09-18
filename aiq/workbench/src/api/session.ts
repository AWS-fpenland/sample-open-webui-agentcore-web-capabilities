// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Stable per-tab AgentCore runtime session id (33–128 chars). sessionStorage = one id per tab.
const KEY = 'aiq-wb-session-id';

export function getRuntimeSessionId(): string {
  let id = sessionStorage.getItem(KEY);
  if (!id || id.length < 33 || id.length > 128) {
    id = `aiq-wb-${crypto.randomUUID()}-${Date.now().toString(36)}`;
    sessionStorage.setItem(KEY, id);
  }
  return id;
}

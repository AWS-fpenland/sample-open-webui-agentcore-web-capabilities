<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Captured implementation learnings

[Documentation home](../README.md)

These notes preserve reusable maintainer patterns learned while building the
sample. They are not supported product interfaces or deployment instructions.
Verify every pattern against current source before reusing it.

## Architecture patterns

- [`admin-console-on-existing-cognito-pool.md`](architecture-patterns/admin-console-on-existing-cognito-pool.md)
  — pattern for adding a separately authorized SPA/API to an existing Cognito
  pool.
- [`native-tool-loop-managed-web-capabilities.md`](architecture-patterns/native-tool-loop-managed-web-capabilities.md)
  — lessons on pinned native-loop integration, app-attested identity, managed
  Browser boundaries, attempt quotas and evidence-driven acceptance.

Current implementations and operator contracts:

- [`../../infra/lib/metering-console.ts`](../../infra/lib/metering-console.ts)
- [`../../infra/lib/metering-stack.ts`](../../infra/lib/metering-stack.ts)
- [Metering guide](../METERING.md)
- [Console maintainer README](../../console/README.md)

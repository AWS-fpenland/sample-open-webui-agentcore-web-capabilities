# AI-Q Research Workbench (SPA)

The workbench is where research results **live**: Library, Package viewer, Compare, Model Lab and the Export center
(`11-architecture.md` §1–§2). The conversation in Open WebUI is where intent is formed; every package here links back to
it and every chat card links here. Same Cognito user pool, same design tokens (`design/tokens.css`, used verbatim as
the global stylesheet), same vocabulary (package, source, artifact, run, role).

```
aiq/workbench/
├── index.html                 theme/density applied before first paint (no flash)
├── public/config.json         local/mock config; the deploy step OVERWRITES this file
├── src/
│   ├── main.tsx               boot: /config.json → mock adapter | Cognito PKCE + real runtime API
│   ├── App.tsx                routes + sign-in gate
│   ├── api/
│   │   ├── decode.ts          NDJSON / SSE / double-encoded stream decoder (mirrors the pipe's _invoke)
│   │   ├── client.ts          WorkbenchApi interface + real adapter (one POST per op, Bearer token)
│   │   └── session.ts         stable per-tab X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
│   ├── mock/                  fixtures + mock adapter (12 packages, real 176-entry matrix, evals)
│   ├── auth/AuthBridge.tsx    react-oidc-context → Session/Api contexts
│   ├── components/            Shell, Chip/StatusChip, Kpi, Dialog, Menu, Tabs, TagEditor, RoleSelector, Toast, States
│   ├── pages/                 LibraryPage, PackagePage (+package/tabs, RerunDialog, CompareDialog), ComparePage,
│   │                          ModelLabPage, ExportsPage, AuthPages
│   ├── lib/                   format, theme, markdown (marked + DOMPurify), models (shared matrix), owui links, useAsync
│   └── styles/                tokens.css (verbatim) + app.css (components)
└── scripts/
    ├── selfcheck.mjs          decoder + helper self-check (node scripts/selfcheck.mjs)
    └── build-mock-matrix.py   regenerates src/mock/matrix.json from evidence/model-lab/*.json
```

## Runtime configuration: `/config.json`

Fetched at startup (`cache: no-store`); nothing is baked into the bundle.

```json
{
  "region": "us-east-1",
  "userPoolId": "us-east-1_XXXXXXXXX",
  "clientId": "<public PKCE app client id>",
  "cognitoDomain": "<prefix>.auth.us-east-1.amazoncognito.com",
  "runtimeArn": "arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/<id>",
  "owuiUrl": "https://oui.<domain>",
  "workbenchUrl": "https://aiq.<domain>",
  "mock": false,
  "connectedApps": false
}
```

| key | used for |
|---|---|
| `region`, `userPoolId`, `clientId` | OIDC authority `https://cognito-idp.<region>.amazonaws.com/<userPoolId>`, PKCE code flow, scope `openid email profile`, `redirect_uri = <origin>/auth/callback`, tokens in **sessionStorage**, silent renew |
| `cognitoDomain` | Managed Login sign-out (`/logout?client_id=…&logout_uri=<origin>/`) |
| `runtimeArn`, `region` | every op is `POST https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencoded ARN>/invocations?qualifier=DEFAULT` with `Authorization: Bearer <access_token>` and the per-tab session header |
| `owuiUrl` | deep links: `/c/<conversation_id>` (Continue in chat) or `/?models=aiq_agentcore.deep&q=…&submit=false`; "Use once in chat" copies `/model role=<id> …` |
| `mock` | `true` → fixtures, no sign-in, "mock mode" chip. Also `?mock=1` on any URL (remembered for the tab; `?mock=0` clears) |
| `connectedApps` | enables the Notion / Google Docs / Gmail buttons in the Export center (otherwise disabled with a tooltip) |

## Mock mode

`public/config.json` ships with `"mock": true`, so `npm run preview` (or `npm run dev`) works without any AWS
resources. Fixtures live in `src/mock/`:

- **12 packages** with mixed statuses (one running with live progress, one queued, one clarifying, one failed, one
  cancelled, seven completed), a re-run pair and a follow-up pair with lineage, tags, pins, artifacts and exports.
  Running packages advance with wall-clock time; re-runs and evaluations you start create new live items.
- **Capability matrix** built from the REAL `evidence/model-lab/{catalog,prices,probes,roles}.json`
  (176 lane entries, 74 offered, exclusion reasons + raw errors, AWS Price List rates, deterministic role scores).
  Regenerate with `uv run --no-project python scripts/build-mock-matrix.py <evidence/model-lab dir>`.
- Exports return `url: "#mock"`; the UI says so instead of pretending a file was produced.

## Build

```
cd aiq/workbench
npm ci            # or: ln -s <prebuilt node_modules> node_modules
npm run build     # tsc -b && vite build → dist/
npm run preview   # serves dist/ on http://localhost:4173 (SPA fallback included)
node scripts/selfcheck.mjs
```

## Deploy assumptions

- **S3 + CloudFront with Origin Access Control**; S3 Block Public Access stays ON (no website hosting, no public ACLs).
- SPA fallback: CloudFront custom error responses `403/404 → /index.html (200)` so `/p/<id>`, `/compare/…`,
  `/lab`, `/exports/<id>`, `/auth/callback` deep links load.
- `config.json` written by the deploy step next to the bundle with `Cache-Control: no-store`; hashed assets in
  `dist/assets/` can be cached immutably; `index.html` short TTL.
- Cognito: a **public app client (PKCE, no secret)** on the same user pool as Open WebUI with callback
  `https://aiq.<domain>/auth/callback` and sign-out URL `https://aiq.<domain>/`; the runtime's JWT authorizer
  `allowedClients` includes this client id (tenant key is derived from `iss|sub`, so both clients see the same packages).
- The SPA calls the runtime data plane **directly** (CORS preflight verified in phase 3: `access-control-allow-origin: *`
  with the session header allowed).
- Suggested CloudFront response-headers policy (CSP):
  `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https://bedrock-agentcore.<region>.amazonaws.com https://cognito-idp.<region>.amazonaws.com https://<cognitoDomain>; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://<cognitoDomain>`
  plus `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`.
  (`style-src 'unsafe-inline'` is needed for the inline style attributes React sets on progress bars; `img-src https:`
  for presigned S3 artifact previews.)

## Security notes

- Report markdown is rendered with `marked` and sanitised with DOMPurify; external links get `rel="noopener noreferrer"`.
- Tokens never appear in URLs; the access token is read from react-oidc-context per request.
- Errors from the runtime (`{type:"error", data:{error:{code,message}}}`) are shown verbatim with a Retry (and
  "Sign in again" for 401/403) — never converted into fake success.

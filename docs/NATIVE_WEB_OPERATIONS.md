# Native AgentCore web search — shared-service phase 1

This opt-in integration connects the **native Open WebUI Web Search toggle** to
AgentCore Web Search and the configured external web loader to bounded HTTPS or
AgentCore Browser. It deliberately accepts **shared-service accounting**, not
verified per-user AWS attribution. No Open WebUI fork, runtime patch, explicit
Python toolkit or separate research agent is required for this path.

Tested extension contract: official Open WebUI **v0.11.3**, source
[`2a960a59fe1dbbd35282f0556b3666d81102e781`](https://github.com/open-webui/open-webui/tree/2a960a59fe1dbbd35282f0556b3666d81102e781).
Pin the deployment's tested official image digest; this standalone deployment does
not run `deploy.sh` or change the application image. Revalidate before an upgrade.

## User experience

1. Use a compatible model with native function calling and web-search capability.
   The optional **AgentCore native web (Haiku)** and **AgentCore native web
   (Responses)** presets enable only the web-search builtin category, with no
   attached toolkit, model Filter or knowledge collection.
2. Turn on the normal **Web Search** control and accept its usage notice. Ask for
   a current public reference with source citations. The model may invoke builtin
   `search_web` and then `fetch_url`; the application owns the tool loop.
3. For Browser, ask it to fetch `https://quotes.toscrape.com/js/` and quote the first
   visible quotation with a citation. The loader's exact route selects Browser;
   **the builtin tool has no `render` argument**. Other public HTML/text pages use
   ordinary HTTPS. Merely pasting a URL does not guarantee a tool call.

Existing model connections are preserved. Presets are private workspace models;
the admin configurator does not create users, change permissions or grant ordinary
users access to otherwise unavailable base models. The global toggle is available
subject to Open WebUI's existing user permissions and model capabilities.

## Architecture and contracts

```mermaid
sequenceDiagram
    participant User
    participant OWUI as Official Open WebUI
    participant Adapter as Shared HTTPS Lambda adapter
    participant Search as IAM MCP Gateway / Web Search
    participant Browser as Isolated AgentCore Browser
    User->>OWUI: Enable Web Search and send chat
    OWUI->>OWUI: Model chooses builtin search_web
    OWUI->>Adapter: POST /search + server-held bearer key
    Adapter->>Search: Reserve shared quota; signed managed search
    Search-->>Adapter: Indexed links, titles, snippets
    Adapter-->>OWUI: Native external-search array
    OWUI->>OWUI: Model may choose builtin fetch_url
    OWUI->>Adapter: POST /load {urls}
    alt Exact configured JavaScript route
        Adapter->>Browser: Reserve quota; fresh signed session
        Adapter->>Browser: Fulfill policy-approved public bytes over CDP
        Browser-->>Adapter: Rendered text; confirmed cleanup
    else Other public HTTPS HTML/text
        Adapter->>Adapter: Reserve quota; DNS-pinned bounded HTTPS extraction
    end
    Adapter-->>OWUI: page_content + provenance metadata
    OWUI-->>User: Tool results, grounded answer and source citations
```

| Interface | Request | Response / release limitation |
| --- | --- | --- |
| External search | `POST /search`, bearer key, `{"query":"public query","count":3}` | Array of `link`, `title`, `snippet`; no live page-fetch claim. Input count1–25 is clamped to3 |
| External loader | `POST /load`, same bearer key, `{"urls":["https://example.com/"]}` | Array of `page_content` and `metadata` containing source/final URL, requested URL, title, capability and truncation; at most3 URLs |
| Native search builtin | `search_web(query,count?)` | Pinned upstream clamps to configured count and returns snippets directly, without vector retrieval |
| Native URL builtin | `fetch_url(url)` | Pinned upstream joins document text and applies its content limit; loader metadata is not fully forwarded to the model |
| Legacy search | Same search provider | Configured snippets-only, bypassing automatic page loads and vector ingestion; not full traditional search/RAG |

Source anchors at the pin: `retrieval/web/external.py::search_external`,
`retrieval/loaders/external_web.py::ExternalWebLoader.lazy_load`,
`retrieval/web/utils.py::get_web_loader`, `tools/builtin.py::search_web/fetch_url`,
`retrieval/utils.py::_get_content_from_url_sync` and
`utils/tools.py::get_builtin_tools`, all under `backend/open_webui/`.
CI executes the two exact pinned external hooks with offline dependencies.

The upstream search hook logs results at INFO and turns provider errors into empty
results. Its loader swallows batch errors by default, sometimes yielding empty tool
text. Neither hook supplies an HTTP timeout. The adapter bounds its own work, but
cannot guarantee a useful UI error or cancel a request that never reaches it.
Use adapter correlation/status logs to distinguish empty search from failure.

## Security, routing and accounting

- The Lambda Function URL is intentionally **publicly reachable HTTPS**, with
  Function URL IAM mode `NONE` because the supported hooks send bearer tokens, not
  SigV4. Application authentication checks a generated64-character server-held
  Secrets Manager key before admission or content/service calls. No CORS access,
  AWS credentials, Browser endpoint or user session is exposed to ordinary clients.
- IAM permits only the exact secret, Gateway, custom Browser and existing quota
  table. Public Lambda permissions are scoped to its URL invocation; there is no
  ordinary unauthenticated direct Lambda invocation grant.
- Caller user/chat headers are ignored. Both hooks authenticate the **application**;
  all requests share the fixed `native-shared-service` quota subject. Search usage
  is not represented as per-user spending in the inference ledger.
- Shared daily limits:10 searches and5 page reads, with the existing deployment-wide
  limits30 searches/15 reads still applying across old and new adapters. Static and
  Browser reads use the same reading bucket. Attempts, including admitted failures,
  are never refunded; no table reset or new table evades historical deployment caps.
- Static loading accepts public HTTPS443 only: no credentials or IP literals, all
  DNS answers must be global unicast, connections are pinned, and redirects remain
  on the initial hostname. Maximum2 redirects/3 requests,1MiB entity bytes and12s
  per document. Only UTF-8 HTML/text is accepted; downloads and encoded bodies are
  rejected. Text is bounded to20,000 characters.
- Browser routing is an exact configured URL match, currently the JavaScript demo.
  Other sites never silently fall back to Browser. A disabled Browser route fails
  rather than switching policy. Browser reuses the verified no-egress VPC/resource,
  not sessions: every load creates and terminates a fresh session, with brokered
  subresource controls. Its managed sandbox remains trusted.
- Each HTTP request is limited to8192 bytes and50s including authentication and
  cleanup, with Lambda timeout60s and concurrency2. A failed batch returns an error;
  earlier attempts remain charged. No caching, crawling or application retries.
  Client disconnect does not cancel Lambda; deadlines and session cleanup bound it.

**Boundary:** Open WebUI performs its own SSRF-guarded content-type probe before
invoking the loader. Native binary documents, YouTube and some attachment routes
bypass the external loader. This adapter is not global ingestion governance and
does not claim to replace those upstream controls. Leave local/private URL fetching
disabled, preserve TLS verification, and do not enable unsafe redirect overrides.
Authenticated sites, actions, forms, uploads and bypassing access controls are out
of scope. Disabling file capability on presets is not a global API access boundary.

## Persistence and acceptable use

The configurator persists supported settings through the admin API:
`ENABLE_WEB_SEARCH=true`, external search/loader URLs and keys, result count3,
concurrency1, `BYPASS_WEB_SEARCH_WEB_LOADER=true` and
`BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=true`. It merges the entire current web
block because this pinned API replaces that block. Existing runtime seeders do not
write these settings; no environment override or full-stack redeploy is needed.

These bypass flags avoid **automatic search-result vector ingestion**; they do not
erase chat history/tool outputs, existing logs, exports or an admin's manual Save
to Knowledge action. Do not bulk collect, index or save managed search results into
knowledge pending retention review. Keep source links/citations in every answer
using them. Search-source attribution remains required even though **user**
attribution is deliberately shared. No blanket retention/compliance claim is made.
See [AWS connector guidance](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html)
and the [service research boundary](AGENTCORE_WEB_CAPABILITIES.md).

## Deploy, validate and roll back

1. Verify account/region and ownership of the existing managed search Gateway,
   isolated custom Browser and quota table. Follow the existing
   [canary resource contract](WEB_CANARY_OPERATIONS.md) for those prerequisites;
   do not duplicate a Browser VPC or redeploy the original Compute/Auth stacks.
2. Commit the tested source, then build an external immutable bundle:
   `sh scripts/build-native-web.sh /absolute/new/bundle`. `UV` may select an installed
   `uv` binary. ARM64 binary wheels use the existing hash-locked dependency file.
3. Create a private JSON configuration with `account`, `region`, `adapterAssetPath`,
   `gatewayArn`, `gatewayUrl`, `browserArn`, `browserId`, `quotaTableArn`,
   `quotaTableName` and initially false `searchEnabled`, `fetchEnabled`,
   `browserEnabled`. `browserNetworkPolicyReady=true` is an operator attestation
   after verifying effective routing/SG/DNS isolation, not a substitute for it.
4. From `infra/`, prepare the standalone stack with an explicitly verified profile:

```sh
npx cdk deploy OpenWebUI-NativeWeb \
  --app 'npx ts-node --prefer-ts-exts bin/native-web.ts' \
  -c nativeWeb=on -c nativeWebConfigPath=/absolute/private/native.json \
  --output /absolute/new/assembly --method prepare-change-set \
  --change-set-name reviewed-native-web --exclusively --profile VERIFIED_PROFILE
```

5. Inspect the complete change set and synthesized policies before execution. Only
   the adapter, service secret, scoped role, retained logs and optional alarms are
   new. Existing Gateway/Browser/quota/application resources are referenced, not
   modified. Enable service flags through a reviewed update after negative auth
   and default-off checks pass.
6. Fetch the secret using the authorized operator's AWS session into process memory.
   Set `NATIVE_WEB_SERVICE_KEY` and your own `OWUI_ADMIN_TOKEN` without logging them.
   Run `scripts/configure-native-web.py --base-url https://YOUR_APP --endpoint
   https://YOUR_FUNCTION_URL --backup /private/native-backup.json` to inspect;
   append `--apply --create-models --maintenance-window` to configure. Exclude other
   admin web-configuration edits during this short maintenance window: the upstream
   API has no atomic compare-and-swap. The script rereads before each write and
   preserves unrelated fresh settings, but cannot lock out another admin remotely.
   New presets are staged inactive and activated only after configuration verifies.
   The backup is0600 and contains
   credentials: never commit or copy it into research/public artifacts.
7. Verify native search, result-dependent fetch, Browser/static differences,
   citations, model lanes, configuration persistence and cleanup with bounded
   synthetic public-page chats. Mocked tests do not establish live service proof.

Rollback uses the same configurator and private backup with `--rollback --maintenance-window`, restoring
only its changed fields and preserving unrelated admin edits/chats. It refuses
consequential drift; review instead of force-overwriting. Then disable backend
flags using the same source bundle and reviewed standalone update. Optional native
presets are deactivated before restoring previous providers; chats remain intact.
The script refuses altered or unowned presets rather than deactivating someone
else's model. Never point an alias to a superseded version
without confirming it still exists. Stack removal retains its secret/logs; inventory
and explicitly clean them only when no longer needed. Existing shared resources
must not be removed with this adapter.

Validation commands: `python -m pytest web_capabilities/tests pipe/tests -q`,
`npm --prefix infra test -- --runInBand`, `npm --prefix infra run build`, and
`node scripts/docs-integrity.mjs`. Use the workflow's offline CDK context for tests.

## Cost and phase two

No new always-on compute, NAT or Browser VPC is required. Incremental recurring
cost is principally one Secrets Manager secret (standard list price about$0.40/month,
plus API calls), Lambda execution/requests and logs; optional two alarms add their
regional charge. Search and Gateway charges are separate; Browser charges depend
on session runtime, and model/context tokens still apply. The key is read for each
syntactically valid request, including wrong-key requests; concurrency limits are
not a WAF or full denial-of-service defense. Do not infer a measured bill from quotas.
Price sources, accessed2026-09-15: [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/),
[Lambda](https://aws.amazon.com/lambda/pricing/),
[AgentCore](https://aws.amazon.com/bedrock/agentcore/pricing/),
[CloudWatch](https://aws.amazon.com/cloudwatch/pricing/).

Phase two: upstream identity-aware loader context; consistent deadlines/error
mapping; fuller provenance/citation metadata; explicit per-fetch Browser selection;
retention-safe knowledge controls; unified binary/attachment URL policy; verified
user quotas and ledger integration. Broader Browser routes require policy/extraction
tests, not simply adding an unrestricted render flag. None of these is required
to claim the narrower shared native search/HTML-loader phase when live tests pass.

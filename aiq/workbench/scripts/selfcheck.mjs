// Self-check for src/api/decode.ts (NDJSON / SSE / double-encoded / chunked) and a few pure helpers.
// Run: node scripts/selfcheck.mjs   (transpiles the TS sources with esbuild, which ships with vite)
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { transformSync } from 'esbuild';

const here = dirname(fileURLToPath(import.meta.url));
const tmp = mkdtempSync(join(tmpdir(), 'aiq-wb-selfcheck-'));
function load(rel) {
  const src = readFileSync(join(here, '..', 'src', rel), 'utf8');
  const out = transformSync(src, { loader: 'ts', format: 'esm', target: 'es2022' }).code;
  const file = join(tmp, rel.replace(/\//g, '_').replace(/\.ts$/, '.mjs'));
  writeFileSync(file, out);
  return import(pathToFileURL(file).href);
}

let failures = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${extra ? ` ${extra}` : ''}`);
  if (!cond) failures++;
};

const { decodeAll, decodeLine, NdjsonDecoder } = await load('api/decode.ts');

const ev = (type, data = {}) => JSON.stringify({ type, data });
// 1. plain NDJSON
let out = decodeAll(`${ev('packages', { items: [] })}\n${ev('health', { version: '1' })}\n`);
check('plain NDJSON → 2 events', out.length === 2 && out[0].type === 'packages');
// 2. SSE framing with comments and control lines
out = decodeAll(`: keep-alive\nevent: message\nid: 7\ndata: ${ev('package', { manifest: {} })}\n\nretry: 1000\ndata: ${ev('error', { error: { code: 'not_found', message: 'x' } })}\n`);
check('SSE data:/event:/id:/retry: lines → 2 events, error kept', out.length === 2 && out[1].type === 'error' && out[1].data.error.code === 'not_found');
// 3. double-encoded JSON string lines (BedrockAgentCoreApp wraps yielded strings)
out = decodeAll(JSON.stringify(ev('models', { entries: [] })) + '\n');
check('double-encoded JSON string → event', out.length === 1 && out[0].type === 'models');
// 4. unquoted escaped string
out = decodeAll(`{\\"type\\":\\"export\\",\\"data\\":{\\"format\\":\\"pdf\\"}}\n`);
check('unquoted escaped JSON → event', out.length === 1 && out[0].type === 'export' && out[0].data.format === 'pdf');
// 5. chunk boundaries
const d = new NdjsonDecoder();
const full = `${ev('status', { seq: 1 })}\n${ev('source', { url: 'https://a' })}\n${ev('completed')}`;
let got = [];
for (let i = 0; i < full.length; i += 7) got = got.concat(d.push(full.slice(i, i + 7)));
got = got.concat(d.flush());
check('chunked push + flush (no trailing newline) → 3 events', got.length === 3 && got[2].type === 'completed');
// 6. garbage / non-objects ignored
out = decodeAll(`not json\n[1,2,3]\n42\n"just a string"\n\n${ev('health')}\n`);
check('garbage, arrays, scalars ignored', out.length === 1 && out[0].type === 'health');
// 7. data: prefix + double encoding combined
out = decodeAll(`data: ${JSON.stringify(ev('packages.deleted', { job_id: 'job_x' }))}\n`);
check('data: + double-encoded → event', out.length === 1 && out[0].type === 'packages.deleted');
check('decodeLine(empty) → undefined', decodeLine('') === undefined && decodeLine('   ') === undefined);

// helpers
const fmt = await load('lib/format.ts');
check('usd small values → 4 decimals', fmt.usd(0.0034) === '$0.0034');
check('usd normal', fmt.usd(38.42) === '$38.42');
check('tokens 1.9M', fmt.tokens(1912340) === '1.91M');
check('duration 6m 12s', fmt.duration(372) === '6m 12s');
check('shortId', fmt.shortId('job_3f9a1c2e4b5d6f708192a3b4c5d6e7f8') === 'pkg_3f9a1c2e…');
check('hostOf strips www', fmt.hostOf('https://www.leru.org/publications') === 'leru.org');

const owui = await load('lib/owui.ts');
const cfg = { owuiUrl: 'https://oui.example.com/' };
check('owui chat url', owui.owuiChatUrl(cfg, 'abc') === 'https://oui.example.com/c/abc');
check('owui new research url', owui.owuiNewResearchUrl(cfg, 'a b?') === 'https://oui.example.com/?models=aiq_agentcore.deep&q=a%20b%3F&submit=false');
check('model command', owui.modelCommand({ writer: { model_id: 'x', lane: 'converse' }, planner: { model_id: 'y', lane: 'converse' } }) === '/model writer=x planner=y');

const { runtimeEndpoint } = await load('api/client.ts').catch(() => ({ runtimeEndpoint: null }));
if (runtimeEndpoint) {
  const url = runtimeEndpoint({ region: 'us-east-1', runtimeArn: 'arn:aws:bedrock-agentcore:us-east-1:123:runtime/x-abc' });
  check('runtime endpoint encodes ARN', url === 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123%3Aruntime%2Fx-abc/invocations?qualifier=DEFAULT', url);
}

console.log(failures ? `\n${failures} check(s) FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);

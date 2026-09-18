// Render smoke + mock-adapter checks, headless (no browser available on the build host).
//  * bundles the app with esbuild (same sources vite builds), stubs storage, and server-renders every route in mock mode
//  * exercises the mock adapter end-to-end (list/filter/get/compare/rerun/export/validate/eval)
// Run: node scripts/smoke.mjs
import { build } from 'esbuild';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const tmp = mkdtempSync(join(tmpdir(), 'aiq-wb-smoke-'));
const entry = join(tmp, 'entry.tsx');
writeFileSync(
  entry,
  `
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { StrictMode } from 'react';
import App from '${join(here, '..', 'src', 'App.tsx')}';
import { ApiContext } from '${join(here, '..', 'src', 'api', 'index.tsx')}';
import { ConfigContext } from '${join(here, '..', 'src', 'config.tsx')}';
import { SessionContext } from '${join(here, '..', 'src', 'session.tsx')}';
import { createMockApi } from '${join(here, '..', 'src', 'mock', 'adapter.ts')}';
export { createMockApi };
export function render(path: string, api: any) {
  const cfg = { region: 'us-east-1', userPoolId: 'x', clientId: 'x', cognitoDomain: 'x', runtimeArn: 'x', owuiUrl: 'https://oui.example.com', workbenchUrl: 'http://localhost', mock: true };
  const session = { mock: true, authenticated: true, loading: false, error: null, email: 'r@example.edu', signIn() {}, signOut() {} };
  return renderToString(
    <StrictMode>
      <ConfigContext.Provider value={cfg}>
        <ApiContext.Provider value={api}>
          <SessionContext.Provider value={session}>
            <MemoryRouter initialEntries={[path]}>
              <App />
            </MemoryRouter>
          </SessionContext.Provider>
        </ApiContext.Provider>
      </ConfigContext.Provider>
    </StrictMode>,
  );
}
`,
);
const out = join(tmp, 'bundle.cjs');
await build({ entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: out, nodePaths: [join(here, '..', 'node_modules')], absWorkingDir: join(here, '..'), jsx: 'automatic', loader: { '.css': 'empty' }, logLevel: 'silent', define: { 'process.env.NODE_ENV': '"production"' } });

// browser-ish globals the app touches during render (storage reads in useState initialisers)
const store = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), clear: () => m.clear() };
};
globalThis.localStorage = store();
globalThis.sessionStorage = store();
globalThis.window = globalThis;
globalThis.window.location = { pathname: '/', search: '', origin: 'http://localhost', assign() {} };

const mod = createRequire(import.meta.url)(out);
let failures = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${extra ? ` — ${extra}` : ''}`);
  if (!cond) failures++;
};

// ---- mock adapter
const api = mod.createMockApi();
const all = await api.packagesList({ limit: 50 });
check('packagesList returns 12 fixtures', all.items.length === 12, String(all.items.length));
check('statuses are mixed', new Set(all.items.map((p) => p.status)).size >= 5, [...new Set(all.items.map((p) => p.status))].join(','));
check('one running with progress', all.items.some((p) => p.status === 'running' && p.progress && p.progress.pct > 0));
check('one clarifying', all.items.some((p) => p.status === 'clarifying'));
check('pinned first', all.items[0].pinned === true);
check('every summary has human writer name', all.items.every((p) => p.models.writer.human_name));
const page1 = await api.packagesList({ limit: 8 });
check('cursor pagination', page1.items.length === 8 && page1.next_cursor !== null);
const page2 = await api.packagesList({ limit: 8, cursor: page1.next_cursor });
check('second page', page2.items.length === 4 && page2.next_cursor === null);
check('filter status=completed', (await api.packagesList({ limit: 50, status: 'completed' })).items.every((p) => p.status === 'completed'));
check('filter tag=policy → 2', (await api.packagesList({ limit: 50, tag: 'policy' })).items.length === 2);
check('search q=vector', (await api.packagesList({ limit: 50, q: 'vector' })).items.length >= 2);
const eu = all.items.filter((p) => p.title.startsWith('EU AI Act'));
const root = eu.find((p) => p.lineage.relation === 'root');
const rerun = eu.find((p) => p.lineage.relation === 'rerun');
check('re-run pair linked', !!root && !!rerun && rerun.lineage.parent_package_id === root.job_id);
const pkg = await api.packagesGet(root.job_id);
check('packagesGet manifest schema fields', pkg.manifest.schema_version === 'aiq-agentcore/package/v1' && pkg.manifest.sources.length === 38 && pkg.manifest.artifacts.length === 1 && pkg.report_md.includes('## Sources'));
check('citations verified all', pkg.manifest.citations.unverified === 0 && pkg.manifest.citations.verified >= 7, `${pkg.manifest.citations.verified}/${pkg.manifest.citations.unverified}`);
check('cost lines sum to total', Math.abs(pkg.manifest.cost.lines.reduce((s, l) => s + l.usd, 0) - pkg.manifest.cost.total_usd) < 0.001);
const cmp = await api.packagesCompare(root.job_id, rerun.job_id);
check('compare sections/claims/sources', cmp.sections.length >= 4 && cmp.claims.length >= 3 && cmp.sources.shared.length > 0 && cmp.sources.only_b.length > 0);
const up = await api.packagesUpdate(root.job_id, { tags: ['policy', 'eu'], pinned: true });
check('update tags/pin', up.manifest.organization.tags.includes('eu') && up.manifest.organization.pinned);
let rejected = false;
try { await api.packagesUpdate(root.job_id, { tags: ['Bad Tag'] }); } catch (e) { rejected = e.code === 'invalid_request'; }
check('invalid tag rejected honestly', rejected);
const models = await api.models();
check('matrix from real evidence', models.entries.length === 176 && models.entries.filter((e) => e.offered).length === 74);
check('exclusions carry reasons', models.entries.filter((e) => !e.offered).every((e) => e.exclusion_reasons.length > 0));
const excluded = models.entries.find((e) => !e.offered && e.exclusion_reasons.some((r) => r.startsWith('probe:plain')));
const v = await api.modelsValidate({ writer: { model_id: excluded.id, lane: excluded.lane } });
check('validate flags excluded writer', v.ok === false && v.problems.length === 1, v.problems.map((p) => p.reason).join(';'));
const good = models.entries.find((e) => e.roles_selectable.writer && e.id !== pkg.manifest.models.roles.writer.model_id);
const v2 = await api.modelsValidate({ writer: { model_id: good.id, lane: good.lane } });
check('validate ok + estimate', v2.ok && v2.estimate_usd.high >= v2.estimate_usd.low && v2.estimate_usd.low > 0);
const rr = await api.packagesRerun(root.job_id, { writer: { model_id: good.id, lane: good.lane } });
const rrPkg = await api.packagesGet(rr.job_id);
check('rerun creates running child with lineage', rrPkg.manifest.status === 'running' && rrPkg.manifest.lineage.parent_package_id === root.job_id && rrPkg.manifest.models.roles.writer.model_id === good.id && rrPkg.manifest.models.roles.writer.human_name === good.name);
check('rerun visible in list', (await api.packagesList({ limit: 50 })).items.some((p) => p.job_id === rr.job_id));
let rej2 = false;
try { await api.packagesRerun(root.job_id, { writer: { model_id: excluded.id, lane: excluded.lane } }); } catch (e) { rej2 = e.code === 'invalid_request'; }
check('rerun with excluded model rejected', rej2);
const ex = await api.exportPackage(root.job_id, 'pdf', 'print');
check('export returns #mock honestly', ex.url === '#mock' && ex.filename.endsWith('.pdf') && ex.sha256.length === 64);
let rej3 = false;
try { await api.exportPackage(rr.job_id, 'pdf'); } catch (e) { rej3 = e.code === 'invalid_state'; }
check('export of report-less package rejected', rej3);
const art = await api.artifactUrl(root.job_id, pkg.manifest.artifacts[0].artifact_id);
check('artifact url (svg data)', art.url.startsWith('data:image/svg+xml'));
const evs = await api.events(all.items.find((p) => p.status === 'running').job_id, 0);
check('events journal for running job', evs.length > 3 && evs.every((e) => typeof e.seq === 'number'));
const ev = await api.evalStart({ writer: { model_id: good.id, lane: good.lane } }, ['q1', 'q2'], good.id);
const evl = await api.evalList();
check('eval start + list', ev.job_ids.length === 2 && evl.items[0].eval_id === ev.eval_id && evl.items.length === 2);
const prefs = await api.modelsPrefs({ writer: { model_id: good.id, lane: good.lane } });
check('prefs saved', prefs.prefs.writer.model_id === good.id && (await api.models()).prefs.writer.model_id === good.id);
let nf = false;
try { await api.packagesGet('job_00000000000000000000000000000000'); } catch (e) { nf = e.code === 'not_found'; }
check('unknown package → not_found', nf);

// ---- server render of every route (initial/loading state; effects do not run in SSR)
const routes = ['/', `/p/${root.job_id}`, `/compare/${root.job_id}/${rerun.job_id}`, '/lab', `/exports/${root.job_id}`, '/nope'];
for (const r of routes) {
  try {
    const html = mod.render(r, api);
    const expect = r === '/' ? 'Research Library' : r.startsWith('/p/') ? 'Library' : r.startsWith('/compare') ? 'Compare two runs' : r === '/lab' ? 'Model Lab' : r.startsWith('/exports') ? 'Export center' : 'Nothing here';
    check(`render ${r}`, html.includes(expect) && html.includes('AI-Q Workbench') && html.includes('mock mode'), `${html.length} chars`);
  } catch (e) {
    check(`render ${r}`, false, String(e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : e));
  }
}
console.log(failures ? `\n${failures} check(s) FAILED` : '\nall smoke checks passed');
process.exit(failures ? 1 : 0);

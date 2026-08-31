// Boot the real UI in jsdom against the live API and drive it through every
// route, asserting that each view actually renders content.
import { JSDOM } from 'jsdom';

const BASE = process.env.GIT_SYNAPSE_URL || 'http://localhost:8080';
const errors = [];

const html = await (await fetch(BASE + '/')).text();

const dom = new JSDOM(html, {
  url: BASE + '/',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  resources: undefined,
  beforeParse(window) {
    // app.js passes a URL object; jsdom's window has no fetch of its own.
    window.fetch = (input, init) => {
      const url =
        typeof input === 'string' ? input
        : input instanceof URL ? input.href
        : (input && input.url) || String(input);
      return fetch(url.startsWith('http') ? url : BASE + url, init);
    };
    window.URL = URL;
    // Canvas is not implemented in jsdom; stub just enough for graph.js.
    window.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, {
      get: (_t, prop) => {
        if (prop === 'canvas') return { width: 800, height: 600 };
        if (prop === 'measureText') return () => ({ width: 10 });
        return () => {};
      },
      set: () => true,
    });
    window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    window.scrollTo = () => {};
    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    window.addEventListener('error', (e) => errors.push('window.error: ' + (e.error?.stack || e.message)));
    window.addEventListener('unhandledrejection', (e) => errors.push('unhandled: ' + (e.reason?.stack || e.reason)));
    const origError = window.console.error;
    window.console.error = (...a) => { errors.push('console.error: ' + a.map(String).join(' ')); origError(...a); };
  },
});

const { window } = dom;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// jsdom cannot execute <script type="module">, so the two modules are
// flattened into one classic script by build-bundle (see bundle.js).
import { readFileSync } from 'fs';
const script = window.document.createElement('script');
script.textContent = readFileSync('./bundle.js', 'utf8');
window.document.body.appendChild(script);

await sleep(2500);

const $ = (s) => window.document.querySelector(s);
const view = () => $('#view');

function report(label, ok, detail) {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label.padEnd(34)} ${detail}`);
  if (!ok) errors.push(`${label}: ${detail}`);
}

// Measure bar must be populated from /api/measures.
report('measure chips rendered', $('#measure-chips').children.length >= 6,
       `${$('#measure-chips').children.length} chips`);
report('measure select populated', $('#measure-select').options.length > 25,
       `${$('#measure-select').options.length} options`);

// Resolve real ids from the live corpus to build deep-link routes.
const repos = await (await fetch(BASE + '/api/repos?limit=1&order_by=pair_count')).json();
const repoId = repos.repos[0].id;
const files = await (await fetch(`${BASE}/api/repos/${repoId}/hotspots?limit=1`)).json();
const fileId = files.hotspots[0].id;
const coupled = await (await fetch(`${BASE}/api/files/${fileId}/coupled?limit=1&min_support=2`)).json();
const otherId = coupled.partners[0]?.other_id;
const dirs = await (await fetch(`${BASE}/api/repos/${repoId}/directories?limit=1`)).json();
const dirId = dirs.directories[0].id;
const runs = await (await fetch(BASE + '/api/runs?limit=1')).json();
const runId = runs.runs[0].id;

const repoWithImpact = await (await fetch(BASE + '/api/impact/graph?limit=5')).json();
const impactRepoId = (repoWithImpact.edges[0] || {}).target_repo_id || repoId;
const impactSrcId = (repoWithImpact.edges[0] || {}).source_repo_id || repoId;

const routes = [
  ['#/',                                  'Overview'],
  ['#/repos',                             'Repositories'],
  ['#/accounts',                          'Accounts'],
  [`#/repo/${repoId}`,                    'Repo (lands on files)'],
  [`#/repo/${repoId}?tab=overview`,       'Repo overview'],
  [`#/repo/${repoId}?tab=pairs`,          'Repo pairs'],
  [`#/repo/${repoId}?tab=files`,          'Repo files'],
  ['#/insights?tab=files&repo=5',         'Insights files'],
  [`#/repo/${repoId}?tab=dirs`,           'Repo dirs'],
  [`#/repo/${impactRepoId}?tab=impact`,   'Repo cross-repo impact'],
  [`#/repo/${repoId}?tab=modules`,        'Repo de-facto modules'],
  [`#/repo/${repoId}?tab=risk`,           'Repo risk'],
  [`#/repo/${repoId}?tab=meta`,           'Repo metadata'],
  [`#/file/${fileId}`,                    'File coupled'],
  [`#/file/${fileId}?tab=history`,        'File history'],
  [`#/file/${fileId}?tab=authors`,        'File authors'],
  otherId ? [`#/pair/${fileId}/${otherId}`, 'Pair detail'] : null,
  [`#/dir/${dirId}`,                      'Directory coupling'],
  ['#/impact',                            'Impact (org-wide)'],
  [`#/impact?repo=${impactRepoId}&dir=upstream`,   'Impact upstream'],
  [`#/impact?repo=${impactSrcId}&dir=downstream`,  'Impact downstream'],
  [`#/repopair/${impactSrcId}/${impactRepoId}`,    'Repo pair detail'],
  ['#/insights?tab=risk',                 'Insights risk'],
  ['#/insights?tab=drift',                'Insights drift (emerging)'],
  ['#/insights?tab=drift&trend=decaying', 'Insights drift (decaying)'],
  [`#/insights?tab=modules&repo=${repoId}`, 'Insights modules'],
  ['#/explore',                           'Explore (default)'],
  ['#/explore?q=Dockerfile',              'Explore (search)'],
  [`#/graph?repo=${repoId}&limit=60&min=5`, 'Graph (files)'],
  ['#/graph?mode=repos&min=0.4',          'Graph (repositories)'],
  ['#/measures',                          'Measures catalogue'],
  ['#/runs',                              'Jobs'],
  [`#/run/${runId}`,                      'Run detail'],
  ['#/feedback',                          'Feedback (open)'],
  ['#/feedback?status=all',               'Feedback (all)'],
].filter(Boolean);

console.log('\n=== driving every route ===');
for (const [hash, label] of routes) {
  window.history.pushState({}, '', hash.replace(/^#/, ''));
  window.dispatchEvent(new window.PopStateEvent('popstate'));
  let text = '';
  for (let i = 0; i < 60; i++) {
    await sleep(120);
    text = view().textContent || '';
    if (!text.includes('Loading…') && text.trim().length > 40) break;
  }
  const failed = text.includes('Could not load') || text.includes('Not found');
  const rows = view().querySelectorAll('table.data tbody tr').length;
  const tiles = view().querySelectorAll('.stat').length;
  const cards = view().querySelectorAll('.card, .measure-card').length;
  report(label, !failed && text.trim().length > 40,
         `${text.trim().length} chars, ${rows} rows, ${tiles} tiles, ${cards} cards`);
}

// Interaction: clicking a table row must navigate.
console.log('\n=== drill-down trail ===');
for (const [path, label, expect] of [
  ['/repo/5',  'repository', ['Accounts', 'google']],
  ['/dir/1',   'directory',  ['Accounts', 'google', 'brotli']],
  ['/insights?repo=5', 'scoped insights', ['Accounts', 'google', 'guava', 'Insights']],
  ['/impact?repo=5',   'scoped impact',   ['Accounts', 'google', 'guava', 'Impact']],
  ['/graph?repo=5',    'scoped graph',    ['Accounts', 'google', 'guava']],
]) {
  window.history.pushState({}, '', path);
  window.dispatchEvent(new window.PopStateEvent('popstate'));
  let trail = '';
  for (let i = 0; i < 60; i++) {
    await sleep(120);
    const el = window.document.querySelector('.crumbs');
    trail = el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
    if (trail) break;
  }
  const missing = expect.filter((w) => !trail.includes(w));
  report(`${label} trail`, !missing.length, trail || 'no breadcrumb');
}

// Opening a repository lands on its files, with the scope already applied and
// the Files tab in focus -- not a page the reader has to configure first.
window.history.pushState({}, '', '/insights?tab=files&repo=5');
window.dispatchEvent(new window.PopStateEvent('popstate'));
{
  let rows = 0, scoped = '', active = '';
  for (let i = 0; i < 60; i++) {
    await sleep(120);
    rows = window.document.querySelectorAll('#view table tbody tr').length;
    const sel = window.document.querySelector('.toolbar select');
    scoped = sel ? sel.options[sel.selectedIndex]?.text || '' : '';
    const tab = window.document.querySelector('.tab.active');
    active = tab ? tab.textContent.trim() : '';
    if (rows) break;
  }
  report('files open scoped and focused', rows > 0 && scoped === 'guava' && active === 'Files',
         `${rows} rows, scope "${scoped}", tab "${active}"`);
}

console.log('\n=== interaction ===');
window.history.pushState({}, '', '/repos');
window.dispatchEvent(new window.PopStateEvent('popstate'));
await sleep(1400);
const firstRow = view().querySelector('table.data tbody tr');
const before = window.location.pathname;
if (!firstRow) {
  report('row click navigates', false, 'no table row rendered to click');
} else {
  firstRow.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(1400);
  report('row click navigates', window.location.pathname !== before,
         `${before} -> ${window.location.pathname}`);
}

// Interaction: column sort must reorder.
window.history.pushState({}, '', '/repos');
window.dispatchEvent(new window.PopStateEvent('popstate'));
await sleep(1400);
const th = [...view().querySelectorAll('th.sortable')].find((t) => t.textContent.includes('Commits'));
const firstBefore = view().querySelector('tbody tr').textContent.slice(0, 30);
th.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(300);
const firstAfter = view().querySelector('tbody tr').textContent.slice(0, 30);
report('column sort reorders', firstBefore !== firstAfter, `${firstBefore.trim()} -> ${firstAfter.trim()}`);

// Interaction: switching the global measure must re-render.
// setMeasure() repaints the chip row, so the clicked node is replaced.
// Re-query by label rather than holding the (now detached) original.
const chip = [...$('#measure-chips').children].find((c) => !c.classList.contains('active'));
const chipName = chip.textContent;
const headerBefore = view().querySelector('table.data thead').textContent;
chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(1800);
const nowActive = [...$('#measure-chips').children].find((c) => c.classList.contains('active'));
const headerAfter = view().querySelector('table.data thead').textContent;
report('measure switch applies',
       nowActive && nowActive.textContent === chipName && headerBefore !== headerAfter,
       `active chip = ${nowActive && nowActive.textContent}; table column re-labelled`);

// Interaction: theme toggle.
const themeBefore = window.document.documentElement.dataset.theme;
$('#theme-toggle').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(120);
report('theme toggle', window.document.documentElement.dataset.theme !== themeBefore,
       `${themeBefore} -> ${window.document.documentElement.dataset.theme}`);

// Interaction: omnibox search.
const box = $('#omnibox');
box.value = 'cluster';
box.dispatchEvent(new window.Event('input', { bubbles: true }));
await sleep(1600);
const results = $('#omnibox-results');
report('omnibox returns results', !results.hidden && results.children.length > 1,
       `${results.children.length} nodes`);

console.log('\n=== JS errors ===');
if (errors.length) {
  for (const e of errors.slice(0, 12)) console.log('  ' + String(e).slice(0, 220));
  console.log(`\nRESULT: ${errors.length} problem(s)`);
  process.exit(1);
}
console.log('  none');
console.log('\nRESULT: UI renders every route with no JS errors');

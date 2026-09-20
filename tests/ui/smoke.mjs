// Boot the real UI in jsdom against the live API and drive it through every
// route, asserting that each view actually renders content.
import { JSDOM } from 'jsdom';

const BASE = process.env.GIT_SYNAPSE_URL || 'http://localhost:8080';

/* The deployment may be behind a sign-in. Get a session before anything else,
   creating the first administrator if nobody exists yet, so the suite works on
   a fresh stack and on one that already has people. Credentials come from the
   environment; the defaults suit a local dev stack and nothing else. */
const TEST_EMAIL = process.env.GS_TEST_EMAIL || 'ui-tests@git-synapse.local';
const TEST_PASSWORD = process.env.GS_TEST_PASSWORD || 'ui-tests-password-1234';

async function signIn() {
  const me = await (await fetch(BASE + '/api/auth/me')).json();
  if (!me.auth_required) return null;   // an open deployment needs no session

  const post = (path, body) => fetch(BASE + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });

  const res = await post('/api/auth/login', { email: TEST_EMAIL, password: TEST_PASSWORD });

  if (!res.ok) {
    throw new Error(
      `could not sign in as ${TEST_EMAIL} (${res.status}). Accounts come from the `
      + 'environment: set ADMIN_EMAIL and ADMIN_PASSWORD on the deployment, and '
      + 'GS_TEST_EMAIL and GS_TEST_PASSWORD here to match.');
  }
  const cookie = (res.headers.getSetCookie?.() || [])
    .map((c) => c.split(';')[0]).join('; ');
  if (!cookie) throw new Error('signed in but no session cookie came back');
  return cookie;
}

const COOKIE = await signIn();
const errors = [];

const withCookie = (init = {}) => (COOKIE
  ? { ...init, headers: { ...(init.headers || {}), cookie: COOKIE } }
  : init);

const html = await (await fetch(BASE + '/', withCookie())).text();

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
      return fetch(url.startsWith('http') ? url : BASE + url, withCookie(init));
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
// A searchable combobox now, not a native select: 31 measures is past the
// point where scrolling one is reasonable.
{
  const trigger = $('#measure-select').querySelector('.picker-trigger');
  trigger.click();
  const options = $('#measure-select').querySelectorAll('.picker-option').length;
  report('measure picker populated', options > 25, `${options} options, searchable`);
  window.document.body.click();
}

// Resolve real ids from the live corpus to build deep-link routes.
const repos = await (await fetch(BASE + '/api/repos?limit=1&order_by=pair_count', withCookie())).json();
const repoId = repos.repos[0].id;
const repoOwner = repos.repos[0].owner;
const repoName = repos.repos[0].name;
const sourceId = repos.repos[0].account_id;
const files = await (await fetch(`${BASE}/api/repos/${repoId}/hotspots?limit=1`, withCookie())).json();
const fileId = files.hotspots[0].id;
const coupled = await (await fetch(`${BASE}/api/files/${fileId}/coupled?limit=1&min_support=2`, withCookie())).json();
const otherId = coupled.partners[0]?.other_id;
const filePath = files.hotspots[0].path;
const tree = await (await fetch(`${BASE}/api/repos/${repoId}/tree`, withCookie())).json();
const dirPath = (tree.directories[0] || {}).path || '';
const runs = await (await fetch(BASE + '/api/runs?limit=1', withCookie())).json();
const runId = runs.runs[0].id;
const logged = await (await fetch(BASE + '/api/calls?limit=1', withCookie())).json();
const callId = (logged.calls[0] || {}).id;

// Edges are {source, target}; reading source_repo_id here quietly fell back to
// a self-edge, so the impact routes rendered an empty page and asserted nothing.
const repoWithImpact = await (await fetch(BASE + '/api/impact/graph?limit=5', withCookie())).json();
const bumped = repoWithImpact.edges.find((e) => e.bump_count > 0) || repoWithImpact.edges[0] || {};
const impactSrcId = bumped.source ?? repoId;
const impactRepoId = bumped.target ?? repoId;

const routes = [
  ['#/',                                   'Overview'],
  ['#/repos',                              'Repositories'],
  ['#/sources',                            'Sources'],
  [`#/repos/${repoId}`,                    'Repo (lands on files)'],
  [`#/repos/${repoId}?tab=overview`,       'Repo overview'],
  [`#/repos/${repoId}?tab=pairs`,          'Repo pairs'],
  [`#/repos/${repoId}?tab=meta`,           'Repo metadata'],
  [`#/repos/${repoId}?tab=files&f=test`,   'Repo file search'],
  dirPath ? [`#/repos/${repoId}/tree/${dirPath}`, 'Folder'] : null,
  [`#/repos/${repoId}/files/${filePath}`,             'File coupled'],
  [`#/repos/${repoId}/files/${filePath}?tab=history`, 'File history'],
  [`#/repos/${repoId}/files/${filePath}?tab=authors`, 'File authors'],
  otherId ? [`#/repos/${repoId}/pairs/${fileId}/${otherId}`, 'Pair detail'] : null,
  [`#/insights/graph?repo=${repoId}&limit=60&min=5`,  'Map (one repository\u2019s files)'],
  ['#/insights/graph',                                'Map (all repositories)'],
  ['#/insights/graph?mode=repos&min=0.4',             'Map (explicit repos mode)'],
  ['#/insights/graph?mode=repos&all=1',               'Map (stale all= is ignored)'],
  ['#/insights/graph?mode=files',                     'Map (files, nothing scoped)'],
  ['#/insights',                           'Insights (lands on the map)'],
  ['#/insights/impact',                    'Impact (pick a repository)'],
  [`#/insights/impact?repo=${impactRepoId}&dir=upstream`,   'Impact upstream'],
  [`#/insights/impact?repo=${impactSrcId}&dir=downstream`,  'Impact downstream'],
  [`#/insights/impact/${impactSrcId}/${impactRepoId}`,      'Impact edge detail'],
  ['#/insights/shape',                     'Distributions (all)'],
  ['#/insights/shape/pair_support',        'Distribution (log, bucketed)'],
  ['#/insights/shape/languages',           'Distribution (bars link out)'],
  ['#/insights/risk',                      'Insights risk'],
  ['#/insights/drift',                     'Insights drift (emerging)'],
  ['#/insights/drift?trend=decaying',      'Insights drift (decaying)'],
  [`#/insights/modules?repo=${repoId}`,    'Insights modules'],
  ['#/activity',                            'Activity'],
  ['#/activity?surface=mcp',                'Activity (MCP only)'],
  ['#/activity?status=error',               'Activity (errors)'],
  ['#/people',                             'People'],
  ['#/tokens',                             'API tokens'],
  ['#/measures',                           'Measures catalogue'],
  ['#/jobs',                               'Jobs'],
  ['#/jobs?tab=settings',                  'Jobs settings'],
  [`#/jobs/${runId}`,                      'Run detail'],
  callId ? [`#/activity/${callId}`,         'Call detail'] : null,
  ['#/feedback',                           'Feedback (open)'],
  ['#/feedback?status=all',                'Feedback (all)'],
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
  // On failure the page is usually saying exactly what went wrong -- "X is not
  // defined" -- and reporting a character count instead threw that away and
  // cost several rounds of guessing.
  const detail = failed || text.trim().length <= 40
    ? `${hash} -> ${text.trim().replace(/\s+/g, ' ').slice(0, 120) || '(empty)'}`
    : `${text.trim().length} chars, ${rows} rows, ${tiles} tiles, ${cards} cards`;
  report(label, !failed && text.trim().length > 40, detail);
}

// Interaction: clicking a table row must navigate.
console.log('\n=== drill-down trail ===');
for (const [path, label, expect] of [
  [`/repos/${repoId}`, 'repository', ['Sources', repoOwner]],
  [`/repos/${repoId}/tree/${dirPath}`, 'folder',
   ['Sources', ...dirPath.split('/')]],
  [`/repos/${repoId}/files/${filePath}`, 'file',
   ['Sources', filePath.split('/').pop()]],
  [`/insights/risk?repo=${repoId}`, 'scoped insights',
   ['Sources', repoOwner, repoName, 'Insights']],
  [`/insights/impact?repo=${repoId}`, 'scoped impact',
   ['Sources', repoOwner, repoName, 'Insights']],
  [`/insights/graph?repo=${repoId}`, 'scoped graph',
   ['Sources', repoOwner, repoName, 'Insights']],
]) {
  // The previous page's breadcrumb is still in the DOM until the new view
  // replaces it, and "the first non-empty trail" was therefore sometimes the
  // old one -- a race that only showed under load.
  const previous = (() => {
    const el = window.document.querySelector('.crumbs');
    return el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  })();
  window.history.pushState({}, '', path);
  window.dispatchEvent(new window.PopStateEvent('popstate'));
  let trail = '';
  for (let i = 0; i < 60; i++) {
    await sleep(120);
    const el = window.document.querySelector('.crumbs');
    trail = el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
    if (trail && trail !== previous) break;
  }
  const missing = expect.filter((w) => !trail.includes(w));
  report(`${label} trail`, !missing.length, trail || 'no breadcrumb');
}

// Opening a repository lands on its files, with the Files tab in focus -- not
// a page the reader has to configure first.
window.history.pushState({}, '', `/repos/${repoId}`);
window.dispatchEvent(new window.PopStateEvent('popstate'));
{
  let rows = 0, active = '', folders = 0;
  for (let i = 0; i < 60; i++) {
    await sleep(120);
    rows = window.document.querySelectorAll('#view table tbody tr').length;
    folders = [...window.document.querySelectorAll('#view table tbody tr')]
      .filter((tr) => tr.textContent.includes('\u{1F4C1}')).length;
    const tab = window.document.querySelector('.tab.active');
    active = tab ? tab.textContent.trim() : '';
    if (rows) break;
  }
  report('repository opens on its files', rows > 0 && active === 'Files' && folders > 0,
         `${rows} rows (${folders} folders), tab "${active}"`);
}

console.log('\n=== interaction ===');

// /repos groups repositories by source, and a shut group holds no table. Both
// of the checks below want a row, so open the first group the way a reader
// would -- jsdom does not fire `toggle` off an `open` assignment, so say it.
const openFirstGroup = async () => {
  for (let i = 0; i < 60; i++) {
    const panel = view().querySelector('details.repo-group');
    if (panel) {
      if (!panel.open) {
        panel.open = true;
        panel.dispatchEvent(new window.Event('toggle'));
      }
      await sleep(120);
      if (panel.querySelector('table.data tbody tr')) return panel;
    }
    await sleep(120);
  }
  return null;
};

/* The first group is whichever source sorts first, which on a small corpus is
   often a single repository. Where a check needs rows to compare, it needs a
   group that actually has them. */
const openGroupWithRows = async (least) => {
  for (let i = 0; i < 60; i++) {
    for (const panel of view().querySelectorAll('details.repo-group')) {
      if (!panel.open) {
        panel.open = true;
        panel.dispatchEvent(new window.Event('toggle'));
        await sleep(120);
      }
      if (panel.querySelectorAll('table.data tbody tr').length >= least) return panel;
    }
    await sleep(120);
  }
  return null;
};

window.history.pushState({}, '', '/repos');
window.dispatchEvent(new window.PopStateEvent('popstate'));
await sleep(1400);
const openedGroup = await openFirstGroup();
report('repository group expands', Boolean(openedGroup),
       openedGroup ? `${openedGroup.querySelectorAll('tbody tr').length} rows under the first source`
                   : 'no repository group rendered');
const firstRow = openedGroup && openedGroup.querySelector('table.data tbody tr');
const before = window.location.pathname;
if (!firstRow) {
  report('row click navigates', false, 'no table row rendered to click');
} else {
  firstRow.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(1400);
  report('row click navigates', window.location.pathname !== before,
         `${before} -> ${window.location.pathname}`);
}

// Interaction: column sort must reorder. A group holding one repository cannot
// reorder, and reading that as a broken sort is how this check failed on every
// corpus but the one it was written on, so it takes a group that has two rows
// to put in an order and says so when the corpus offers none.
window.history.pushState({}, '', '/repos');
window.dispatchEvent(new window.PopStateEvent('popstate'));
await sleep(1400);
const sortable = await openGroupWithRows(2);
if (!sortable) {
  report('column sort reorders', true,
         'no source here holds two repositories, so there is nothing to order');
} else {
  const th = [...sortable.querySelectorAll('th.sortable')]
    .find((t) => t.textContent.includes('Commits'));
  const firstBefore = sortable.querySelector('tbody tr').textContent.slice(0, 30);
  th.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(300);
  const firstAfter = sortable.querySelector('tbody tr').textContent.slice(0, 30);
  report('column sort reorders', firstBefore !== firstAfter,
         `${firstBefore.trim()} -> ${firstAfter.trim()}`);
}

// Interaction: switching the global measure must re-render.
// setMeasure() repaints the chip row, so the clicked node is replaced.
// Re-query by label rather than holding the (now detached) original.
// This has to run on a page the measure actually ranks something on. Run from
// /repos it passed for the wrong reason: the measure bar is hidden there, so
// no column can be re-labelled, and what the check really saw was the sort
// arrow left by the check before it falling off the header.
window.history.pushState({}, '', `/repos/${repoId}?tab=pairs`);
window.dispatchEvent(new window.PopStateEvent('popstate'));
await sleep(1800);
const chip = [...$('#measure-chips').children].find((c) => !c.classList.contains('active'));
const chipName = chip.textContent;
const headerBefore = view().querySelector('table.data thead').textContent;
chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(1800);
const nowActive = [...$('#measure-chips').children].find((c) => c.classList.contains('active'));
const headerAfter = view().querySelector('table.data thead').textContent;
report('measure switch applies',
       nowActive && nowActive.textContent === chipName && headerBefore !== headerAfter,
       `active chip = ${nowActive && nowActive.textContent}; `
       + `header ${headerBefore === headerAfter ? 'unchanged' : 're-labelled'}`);

// Interaction: theme toggle.
const themeBefore = window.document.documentElement.dataset.theme;
$('#theme-toggle').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(120);
report('theme toggle', window.document.documentElement.dataset.theme !== themeBefore,
       `${themeBefore} -> ${window.document.documentElement.dataset.theme}`);

// Interaction: omnibox search. The term comes from a file that is really in
// the corpus: a word picked by hand only matches on the machine it was picked
// on, and elsewhere an empty result set is indistinguishable from a broken box.
const box = $('#omnibox');
box.value = filePath.split('/').pop().replace(/\.[^.]+$/, '').slice(0, 6);
box.dispatchEvent(new window.Event('input', { bubbles: true }));
await sleep(1600);
const results = $('#omnibox-results');
report('omnibox returns results', !results.hidden && results.children.length > 1,
       `${results.children.length} nodes`);

// Reported, not exited on: the checks below are the ones this restructure was
// for, and stopping here would hide them behind an unrelated failure.
console.log('\n=== JS errors ===');
if (errors.length) {
  for (const e of errors.slice(0, 12)) console.log('  ' + String(e).slice(0, 220));
} else {
  console.log('  none');
}

// Every deep view names its place. A path like /file/584531 says nothing about
// which repository the file is in, so a link cannot be read, shared or trusted.
console.log('\n=== canonical paths ===');
{
  const cases = [
    [`/sources/${sourceId}`,            'source'],
    [`/repos/${repoId}`,                'repository'],
    [`/repos/${repoId}/files/${filePath}`,      'file'],
    dirPath ? [`/repos/${repoId}/tree/${dirPath}`, 'folder'] : null,
    // The root directory is a legitimate coupling partner, and its path is the
    // empty string -- which built /repos/N/tree/ and rendered "Not found".
    [`/repos/${repoId}/tree/`,          'tree root (trailing slash)'],
    [`/repos/${repoId}/tree`,           'tree root'],
    [`/repos/${repoId}/`,               'repository (trailing slash)'],
    ['/insights',                       'insights (lands on the map)'],
    ['/insights/graph?mode=repos',      'repository graph'],
    ['/jobs',                           'jobs'],
  ].filter(Boolean);
  for (const [path, label] of cases) {
    window.history.pushState({}, '', path);
    window.dispatchEvent(new window.PopStateEvent('popstate'));
    let text = '';
    for (let i = 0; i < 60; i++) {
      await sleep(120);
      text = view().textContent || '';
      if (!text.includes('Loading…') && text.trim().length > 40) break;
    }
    // The address must also end up naming what is actually on screen: a path
    // claiming the wrong repository is worse than none, being confidently wrong.
    const landed = window.location.pathname;
    report(`${label} path`, !text.includes('Not found') && text.trim().length > 40,
           `${path}${landed === path ? '' : ' → ' + landed}`);
  }
}

/* The complaint this restructure came from: clicking down the hierarchy jumped
   sideways, so the address stopped describing where you were. Walking it by
   clicking -- never by pushing a URL -- is the only way to catch that, and it
   is what the route-driving loop above cannot see. */
console.log('\n=== measure bar shows only where it ranks something ===');
for (const [path, shouldShow] of [
  // Overview ranks nothing by measure since it became an operator page.
  ['/', false],
  [`/repos/${repoId}?tab=pairs`, true],
  [`/repos/${repoId}/files/${filePath}`, true],
  [`/repos/${repoId}`, false],
  ['/insights/risk', false],
  ['/insights/graph', false],
  ['/jobs', false],
  ['/repos', false],
]) {
  window.history.pushState({}, '', path);
  window.dispatchEvent(new window.PopStateEvent('popstate'));
  await sleep(360);
  const shown = !window.document.getElementById('measure-bar').hidden;
  report(`measure bar on ${path}`, shown === shouldShow,
         shown ? 'shown' : 'hidden');
}

console.log('\n=== click walk: account -> repo -> folder -> file ===');
{
  const settle = async () => {
    for (let i = 0; i < 60; i++) {
      await sleep(120);
      const t = view().textContent || '';
      if (!t.includes('Loading\u2026') && t.trim().length > 40) return;
    }
  };
  const crumbCount = () => {
    const el = window.document.querySelector('.crumbs');
    return el ? el.textContent.split('/').filter((x) => x.trim()).length : 0;
  };
  // The trail repaints with the view, a beat behind the URL.
  const crumbsDeeperThan = async (depth) => {
    for (let i = 0; i < 30; i++) {
      if (crumbCount() > depth) return true;
      await sleep(120);
    }
    return false;
  };
  // A row click is a navigation, so wait for the *path* to change rather than
  // for the view to look settled: under load the old page still reads as
  // rendered content, and the walk then clicks a row of the page it just left.
  const clickRow = async (match) => {
    // Wait for the row rather than reading once: a table that has not painted
    // yet is indistinguishable from one with nothing in it, and giving up on
    // the first look is what made this walk fail under load.
    let row = null;
    for (let i = 0; i < 60; i++) {
      const rows = [...view().querySelectorAll('table.data tbody tr')];
      row = match ? rows.find((r) => match(r)) : rows[0];
      if (row) break;
      await sleep(120);
    }
    if (!row) return false;
    const before = window.location.pathname;
    const tableBefore = view().querySelector('table.data tbody')?.textContent || '';
    row.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    for (let i = 0; i < 60; i++) {
      await sleep(120);
      if (window.location.pathname !== before) break;
    }
    if (window.location.pathname === before) return false;
    // The URL changes first and the table a beat later. Read the old table and
    // the walk clicks the folder it just came from, which navigates nowhere.
    for (let i = 0; i < 60; i++) {
      if ((view().querySelector('table.data tbody')?.textContent || '') !== tableBefore) break;
      await sleep(120);
    }
    await settle();
    return true;
  };

  // Deliberately the old path: links to it exist, so the redirect to
  // /sources is part of what has to keep working.
  window.history.pushState({}, '', '/accounts');
  window.dispatchEvent(new window.PopStateEvent('popstate'));
  // The redirect is a second navigation, so settling on the page still on
  // screen proves nothing. Wait for the address to arrive before reading it.
  for (let i = 0; i < 60 && window.location.pathname !== '/sources'; i++) await sleep(120);
  await settle();

  const steps = [];
  const step = (label, passed) => steps.push([label, passed, window.location.pathname]);

  // A source that has actually been scanned: the walk descends folder → file,
  // which needs history to descend into. A paused or never-scanned source is a
  // legitimate row and simply has no tree below it. Prefer the source holding
  // the repository the routes above resolved, because whichever source happens
  // to sort first may be one file deep and have no folder to descend into.
  let ok = await clickRow((r) => r.textContent.includes(repoOwner))
        || await clickRow((r) => {
             const n = Number((r.children[2]?.textContent || '0').replace(/[^0-9]/g, ''));
             return n > 0 && !/paused/i.test(r.textContent);
           });
  step('source row opens the source', ok && /^\/sources\/\d+$/.test(window.location.pathname));

  ok = await clickRow((r) => r.textContent.includes(repoName)) || await clickRow();
  step('repository row opens the repository',
       ok && /^\/repos\/\d+$/.test(window.location.pathname));

  const depthAtRepo = crumbCount();
  ok = await clickRow((r) => r.textContent.includes('\u{1F4C1}'));
  step('folder row descends into the folder',
       ok && /^\/repos\/\d+\/tree\//.test(window.location.pathname)
          && await crumbsDeeperThan(depthAtRepo));

  const depthAtFolder = crumbCount();
  // Descend until a level with files in it, so the walk works on any corpus.
  // An empty table means the listing has not painted yet, not that the folder
  // holds nothing -- read it only once it has rows, or the walk gives up one
  // level short of a file.
  const rowsHere = async () => {
    for (let i = 0; i < 30; i++) {
      const rows = [...view().querySelectorAll('table.data tbody tr')];
      if (rows.length) return rows;
      await sleep(120);
    }
    return [];
  };
  for (let i = 0; i < 6; i++) {
    const rows = await rowsHere();
    if (rows.some((r) => r.textContent.includes('\u{1F4C4}'))) break;
    if (!(await clickRow((r) => r.textContent.includes('\u{1F4C1}')))) break;
  }
  ok = await clickRow((r) => r.textContent.includes('\u{1F4C4}'));
  step('file row opens the file, still under its repository',
       ok && /^\/repos\/\d+\/files\//.test(window.location.pathname)
          && await crumbsDeeperThan(depthAtFolder - 1));

  // The file page's own tab bar rebuilt its URL from the numeric id, so every
  // tab on every file 404'd. Clicking a tab is the only way to see that.
  const tabs = [...view().querySelectorAll('.tab')];
  const history = tabs.find((t) => /history/i.test(t.textContent));
  if (history) {
    history.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await settle();
    // The tab swaps the panel under the same URL, so there is no path change to
    // wait on -- poll the panel instead of trusting one settle.
    let kept = false;
    for (let i = 0; i < 30; i++) {
      kept = !(view().textContent || '').includes('Not found')
             && /^\/repos\/\d+\/files\//.test(window.location.pathname);
      if (kept) break;
      await sleep(120);
    }
    step('a file tab keeps the file', kept);
  } else {
    step('a file tab keeps the file', false);
  }

  for (const [label, passed, where] of steps) report(label, passed, where);
}

if (errors.length) {
  console.log(`\nRESULT: ${errors.length} problem(s)`);
  process.exit(1);
}
console.log('\nRESULT: every route renders, every link resolves, the walk holds');

/**
 * Layout checks in a real browser.
 *
 * jsdom does no layout and loads no stylesheet, so the smoke test cannot see a
 * component sitting flush against the next one, or an element whose `hidden`
 * attribute is beaten by an author `display` rule. Both shipped. This measures
 * what a reader actually sees: real boxes, real computed styles.
 */
import puppeteer from 'puppeteer';

const BASE = process.env.GIT_SYNAPSE_URL || 'http://localhost:8080';
/* The page rhythm is 16px between stacked blocks. Two deliberate exceptions:
   a breadcrumb sits 12px above its title, and a section title sits 24px below
   the previous block but only 10px above its own card -- a heading belongs to
   what follows it, so it must hug it. Anything tighter than 10px is two blocks
   reading as one, which is the defect this catches. */
const MIN_GAP = 10;
const MAX_GAP = 28;

const PAGES = [
  ['/', 'Overview'],
  ['/repos', 'Repositories'],
  ['/repos/4', 'Repository'],
  ['/repos/4?tab=pairs', 'Repository pairs'],
  ['/repos/4/tree/src', 'Folder'],
  ['/insights/graph', 'Insights map'],
  ['/insights/impact?repo=5&dir=upstream', 'Impact'],
  // Unscoped as well as scoped: the empty state is a different page, and it
  // carried prose no other page did.
  ['/insights/impact', 'Impact (unscoped)'],
  ['/insights/shape', 'Distributions'],
  ['/insights/shape/commit_width', 'One distribution'],
  ['/insights/risk', 'Risk'],
  ['/insights/drift', 'Drift'],
  ['/sources', 'Sources'],
  ['/jobs', 'Jobs'],
  ['/jobs?tab=settings', 'Jobs settings'],
  ['/activity', 'Activity'],
  ['/measures', 'Measures'],
  ['/people', 'People'],
  ['/tokens', 'API tokens'],
  ['/repos/4?tab=meta', 'Repository metadata'],
  ['/insights/modules?repo=5', 'Modules'],
  ['/feedback', 'Feedback'],
];

// Where the ranking measure orders something on screen, and where it does not.
const MEASURE_BAR = [
  ['/', false], ['/repos/4?tab=pairs', true],
  ['/repos', false], ['/insights/risk', false], ['/jobs', false],
];

const problems = [];
const ok = (label, detail) => console.log(`  ok   ${label.padEnd(36)} ${detail}`);
const bad = (label, detail) => { console.log(`  FAIL ${label.padEnd(36)} ${detail}`); problems.push(`${label}: ${detail}`); };

/* The deployment may be behind a sign-in. Get a session cookie first and give
   it to the browser, so these pages render rather than showing the door. */
const TEST_EMAIL = process.env.GS_TEST_EMAIL || 'ui-tests@git-synapse.local';
const TEST_PASSWORD = process.env.GS_TEST_PASSWORD || 'ui-tests-password-1234';
// Only needed against a deployment nobody has claimed yet, where creating the
// account is the only way in and the token is what authorises that.
const SETUP_TOKEN = process.env.GS_SETUP_TOKEN || '';

async function sessionCookie() {
  const me = await (await fetch(BASE + '/api/auth/me')).json();
  if (!me.auth_required && !me.needs_setup) return null;
  const res = await fetch(BASE + (me.needs_setup ? '/api/auth/setup' : '/api/auth/login'), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(me.needs_setup
      ? { email: TEST_EMAIL, name: 'UI tests', password: TEST_PASSWORD,
          setup_token: SETUP_TOKEN }
      : { email: TEST_EMAIL, password: TEST_PASSWORD }),
  });
  if (!res.ok) {
    throw new Error(`could not sign in as ${TEST_EMAIL} (${res.status}). `
      + (me.needs_setup
        ? 'This deployment has no accounts yet, so one must be created, which '
          + 'needs the setup token. Set GS_SETUP_TOKEN to the value the API '
          + 'printed at startup, or start it with ADMIN_SETUP_TOKEN.'
        : 'Set GS_TEST_EMAIL and GS_TEST_PASSWORD to an account on this deployment.'));
  }
  const raw = (res.headers.getSetCookie?.() || [])[0] || '';
  const [pair] = raw.split(';');
  const [name, value] = pair.split('=');
  return { name, value };
}

const AUTH_COOKIE = await sessionCookie();

const browser = await puppeteer.launch({
  executablePath: '/usr/bin/chromium-browser',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1000 });
if (AUTH_COOKIE) {
  // page.setCookie, not browser.setCookie: this puppeteer is older than the
  // browser-level API, and the difference is a TypeError rather than a warning.
  const { hostname } = new URL(BASE);
  await page.setCookie({ ...AUTH_COOKIE, domain: hostname, path: '/' });
}

async function settle() {
  for (let i = 0; i < 50; i++) {
    const done = await page.evaluate(() => {
      const v = document.getElementById('view');
      return v && !(v.textContent || '').includes('Loading') && (v.textContent || '').trim().length > 40;
    });
    if (done) return;
    await new Promise((r) => setTimeout(r, 150));
  }
}

console.log('=== spacing between stacked blocks ===');
for (const [path, label] of PAGES) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const gaps = await page.evaluate((minGap) => {
    // Each route returns one wrapper element, so the blocks a reader sees are
    // that wrapper's children, not #view's.
    let host = document.getElementById('view');
    while (host.children.length === 1 && host.firstElementChild.children.length > 1) {
      host = host.firstElementChild;
    }
    const kids = [...host.children].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.height > 0 && r.width > 0;
    });
    const out = [];
    for (let i = 1; i < kids.length; i++) {
      const prev = kids[i - 1].getBoundingClientRect();
      const cur = kids[i].getBoundingClientRect();
      // Only vertically stacked siblings; ignore anything laid out side by side.
      if (cur.top < prev.bottom - 1) continue;
      out.push({
        gap: Math.round(cur.top - prev.bottom),
        after: kids[i - 1].className || kids[i - 1].tagName,
        before: kids[i].className || kids[i].tagName,
      });
    }
    return out;
  }, MIN_GAP);

  if (process.env.VERBOSE) {
    for (const g of gaps) console.log(`       ${String(g.gap).padStart(3)}px  ${g.after}  ->  ${g.before}`);
  }
  const tight = gaps.filter((g) => g.gap < MIN_GAP);
  const loose = gaps.filter((g) => g.gap > MAX_GAP);
  if (tight.length) {
    bad(label, tight.map((g) => `${g.gap}px between "${g.after}" and "${g.before}"`).join('; '));
  } else if (loose.length) {
    bad(label, loose.map((g) => `${g.gap}px gap after "${g.after}"`).join('; '));
  } else {
    ok(label, gaps.length ? `${gaps.length} gaps, ${Math.min(...gaps.map((g) => g.gap))}–${Math.max(...gaps.map((g) => g.gap))}px` : 'single block');
  }
}

console.log('\n=== the measure bar is really gone where it does not apply ===');
for (const [path, shouldShow] of MEASURE_BAR) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const box = await page.evaluate(() => {
    const el = document.getElementById('measure-bar');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { h: Math.round(r.height), display: getComputedStyle(el).display };
  });
  const visible = !!box && box.h > 0;
  if (visible === shouldShow) ok(`measure bar on ${path}`, visible ? `${box.h}px tall` : `display:${box.display}`);
  else bad(`measure bar on ${path}`, visible ? `still ${box.h}px tall (display:${box.display})` : 'missing but expected');
}

/* A row that ends short reads as broken layout even when every card in it is
   correct. auto-fit chose five columns for seven charts and left a thousand
   pixels of gap, and seven columns for eight tiles, orphaning the eighth. */
console.log('\n=== rows are full, not ragged ===');
for (const [path, label] of PAGES) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const ragged = await page.evaluate(() => {
    const bad = [];
    for (const grid of document.querySelectorAll('#view .grid-stats, #view .grid-charts')) {
      const rows = new Map();
      for (const cell of grid.children) {
        const r = cell.getBoundingClientRect();
        if (!r.height) continue;
        const key = Math.round(r.top);
        rows.set(key, (rows.get(key) || 0) + 1);
      }
      const counts = [...rows.values()];
      // Only the last row may be short, and never by more than half.
      const full = counts[0] || 0;
      const last = counts[counts.length - 1] || 0;
      if (counts.length > 1 && last * 2 <= full) {
        bad.push(`${grid.className.split(' ').pop()} ${counts.join('+')}`);
      }
    }
    return bad;
  });
  if (ragged.length) bad(`${label} rows`, ragged.join('; '));
  else ok(`${label} rows`, 'every row full');
}

/* Every card is a way in. A summary always has a fuller view behind it, and a
   card that gives no sign of one is a dead end the reader has to guess past.
   Two are deliberately static and named here, so the exception is a decision on
   the record rather than a gap the check quietly tolerates. */
const STATIC_BY_DESIGN = new Set([
  'GitHub token',                 // explains why it is not editable here
  'Fixed for this deployment',    // reference values, changed only in .env
]);

console.log('\n=== every card leads somewhere ===');
for (const [path, label] of PAGES) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const dead = await page.evaluate((allowed) => {
    const out = [];
    for (const c of document.querySelectorAll('#view .card')) {
      const title = ((c.querySelector('.card-title') || {}).textContent || '?').trim();
      if (allowed.includes(title)) continue;
      // An empty card has nothing to lead to, which is not a dead end.
      if (c.querySelector('.empty') && !c.querySelector('tbody tr')) continue;
      const wayOut = c.classList.contains('is-link')
        || c.querySelector('tbody tr.is-link, a[data-nav], .is-link, button, input, select');
      if (!wayOut) out.push(title);
    }
    return out;
  }, [...STATIC_BY_DESIGN]);
  if (dead.length) bad(`${label} cards`, `no way out of: ${dead.join(', ')}`);
  else ok(`${label} cards`, 'every card leads somewhere');
}

/* A figure with no way in is a dead end, and on Overview they are the first
   thing a reader sees. Every tile opens what it counts. */
console.log('\n=== every figure opens what it counts ===');
for (const path of ['/', '/repos/4', '/activity', '/insights/shape/pair_support']) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const inert = await page.evaluate(() => [...document.querySelectorAll('#view .stat')]
    .filter((t) => !t.classList.contains('is-link'))
    .map((t) => (t.querySelector('.stat-label') || {}).textContent));
  if (inert.length) bad(`${path} tiles`, `lead nowhere: ${inert.join(', ')}`);
  else ok(`${path} tiles`, 'every figure opens what it counts');
}

/* A chart drawn into a card that clips is worse than no chart: the reader sees
   a number cut in half and cannot tell it is cut. Both were shipped -- bars
   overflowing the body, and values drawn into columns too narrow for them. */
/* A card that opens a fuller view has one destination, not two. The languages
   card on Overview had its arrow going to the distribution and its bars going
   to a filtered repository list, so where you landed depended on which pixel
   you hit. A preview is a picture of a whole; clicking part of it shows the
   whole. */
console.log('\n=== a chart goes where its card goes ===');
for (const path of ['/', '/insights/shape']) {
  const count = await (async () => {
    await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
    await settle();
    return page.evaluate(() => [...document.querySelectorAll('.card.is-link')]
      .filter((c) => c.querySelector('.cbar, .hbar')).length);
  })();

  for (let i = 0; i < count; i++) {
    const land = async (what) => {
      await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
      await settle();
      const title = await page.evaluate((n, pick) => {
        const card = [...document.querySelectorAll('.card.is-link')]
          .filter((c) => c.querySelector('.cbar, .hbar'))[n];
        const name = card.querySelector('.card-title').textContent;
        (pick === 'arrow' ? card.querySelector('.card-go')
                          : card.querySelector('.cbar, .hbar')).click();
        return name;
      }, i, what);
      await settle();
      return { title, url: await page.evaluate(() => location.pathname + location.search) };
    };
    const arrow = await land('arrow');
    const chart = await land('chart');
    if (arrow.url === chart.url) {
      ok(`${path} "${arrow.title}"`, `arrow and chart both open ${arrow.url}`);
    } else {
      bad(`${path} "${arrow.title}"`, `arrow opens ${arrow.url}, chart opens ${chart.url}`);
    }
  }
}

console.log('\n=== charts fit inside their cards ===');
for (const path of ['/', '/insights/shape', '/insights/shape/pair_support']) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const bad2 = await page.evaluate(() => {
    const out = [];
    for (const card of document.querySelectorAll('#view .card')) {
      const title = ((card.querySelector('.card-title') || {}).textContent || '?').trim();
      const body = card.querySelector('.card-body');
      if (!body || !body.querySelector('.cbars, .hbars')) continue;
      const bottom = [...body.children]
        .reduce((n, e) => Math.max(n, e.getBoundingClientRect().bottom), 0);
      if (bottom > body.getBoundingClientRect().bottom + 1) out.push(`${title}: overflows`);
      const cut = [...card.querySelectorAll('.cbar-value, .cbar-label, .hbar-value')]
        .filter((e) => e.scrollWidth > e.clientWidth + 1).length;
      if (cut) out.push(`${title}: ${cut} label(s) cut`);
    }
    return out;
  });
  if (bad2.length) bad(`${path} charts`, bad2.join('; '));
  else ok(`${path} charts`, 'nothing clipped');
}

/* The splash is fixed and full-screen. If it ever fails to leave, the whole
   application is behind it and every click lands on nothing -- so this checks
   both halves: that it is there during the load, and gone after it. */
console.log('\n=== the splash covers the load and then leaves ===');
{
  const probe = await browser.newPage();
  await probe.setRequestInterception(true);
  probe.on('request', (r) => {
    if (r.url().includes('/api/overview')) setTimeout(() => r.continue(), 2500);
    else r.continue();
  });
  probe.goto(BASE + '/', { waitUntil: 'domcontentloaded' }).catch(() => {});
  await new Promise((r) => setTimeout(r, 900));
  const during = await probe.evaluate(() => {
    const s = document.getElementById('splash');
    return s ? { shown: !s.classList.contains('gone'), covers: s.getBoundingClientRect().height > 200 } : null;
  });
  if (during && during.shown && during.covers) ok('splash during load', 'shown while the first query runs');
  else bad('splash during load', JSON.stringify(during));

  for (let i = 0; i < 40 && (await probe.evaluate(() => !!document.getElementById('splash'))); i++) {
    await new Promise((r) => setTimeout(r, 250));
  }
  const after = await probe.evaluate(() => ({
    splash: !!document.getElementById('splash'),
    rendered: (document.getElementById('view').textContent || '').trim().length > 40,
  }));
  if (!after.splash && after.rendered) ok('splash after load', 'removed once the view rendered');
  else bad('splash after load', JSON.stringify(after));
  await probe.close();
}

/* The door itself. Rendered for a visitor with no session, on a deployment
   that has people -- which is every page they can reach until they sign in. */
console.log('\n=== the sign-in screen ===');
{
  const anon = await browser.createIncognitoBrowserContext
    ? await (await browser.createIncognitoBrowserContext()).newPage()
    : await browser.newPage();
  await anon.deleteCookie(...(await anon.cookies(BASE)));
  await anon.goto(BASE + '/repos/4', { waitUntil: 'domcontentloaded' });
  await new Promise((r) => setTimeout(r, 1800));
  const seen = await anon.evaluate(() => ({
    gated: document.body.classList.contains('gated'),
    form: !!document.querySelector('.gate-form input[type=password]'),
    // The chrome is hidden: a nav that leads nowhere and a search that cannot.
    nav: (document.querySelector('.mainnav') || {}).offsetHeight || 0,
    leaked: (document.getElementById('view').textContent || '').includes('closure-compiler'),
  }));
  if (seen.gated && seen.form && !seen.nav && !seen.leaked) {
    ok('sign-in screen', 'shown instead of the page, with no data behind it');
  } else {
    bad('sign-in screen', JSON.stringify(seen));
  }
  await anon.close();
}

/* A browser told to block site data throws on touching localStorage, and the
   remembered measure was read at module scope -- so the whole application
   failed to evaluate and rendered nothing, with the reason only in a console
   nobody had open. Neither remembered setting is worth the page. */
console.log('\n=== the app survives a browser that blocks site data ===');
{
  const blocked = await browser.newPage();
  if (AUTH_COOKIE) {
    const { hostname } = new URL(BASE);
    await blocked.setCookie({ ...AUTH_COOKIE, domain: hostname, path: '/' });
  }
  await blocked.evaluateOnNewDocument(() => {
    Object.defineProperty(window, 'localStorage', {
      get() { throw new DOMException('The operation is insecure.', 'SecurityError'); },
    });
  });
  const thrown = [];
  blocked.on('pageerror', (e) => thrown.push(String(e).slice(0, 80)));
  await blocked.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await new Promise((r) => setTimeout(r, 2200));
  const rendered = await blocked.evaluate(
    () => (document.getElementById('view').textContent || '').trim().length > 40);
  if (rendered && !thrown.length) ok('storage blocked', 'renders, no page errors');
  else bad('storage blocked', `rendered=${rendered} errors=${thrown.join('; ')}`);
  await blocked.close();
}

/* No page may describe its own history.

   A reader has never seen the version being compared to, so "the table that
   used to sit here ranked repositories by co-change" tells them nothing and
   costs them a paragraph. Code comments are where that belongs, and this
   repository uses them heavily; the difference is who is reading.

   The pattern is deliberately narrow. "Best used to confirm candidates" means
   employed-to, and "where the reduction no longer holds" is a statement about
   the maths -- both are on /measures and both are correct. It matches only
   phrasing that can be about nothing except a previous version. */
const CHANGELOG = new RegExp([
  'used to (sit|be|show|live|say|appear|rank)',
  '(was|were) (tried|removed|replaced|dropped)',
  'we (tried|removed|used to|no longer)',
  'previously (shown|ranked|listed|here)',
  'in an earlier (version|release)',
  'has been replaced',
  'is not how .* (any ?more|is built now)',
].join('|'), 'i');

console.log('\n=== no page talks about its own past ===');
{
  const leaked = [];
  for (const [path, label] of PAGES) {
    await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
    await settle();
    // Collapsed explainers hold the longest prose in the app; unopened, none
    // of it is scanned.
    await page.evaluate(() => {
      for (const d of document.querySelectorAll('details')) d.open = true;
    });
    const text = await page.evaluate(() => document.getElementById('view').innerText || '');
    for (const sentence of text.split(/(?<=[.!?])\s+|\n/)) {
      if (sentence.trim().length > 25 && CHANGELOG.test(sentence)) {
        leaked.push(`${label}: ${sentence.trim().slice(0, 90)}`);
      }
    }
  }
  if (leaked.length) bad('changelog in the UI', leaked.join(' | '));
  else ok('no changelog in the UI', `${PAGES.length} pages, explainers opened`);
}

/* A scope selector that does not stick is worse than none: Distributions read
   corpus-wide aggregates, so choosing a repository navigated to ?repo=N, the
   section ignored it, and the control snapped back to "All repositories" --
   which reads as the page refusing the choice. Either a section scopes, or it
   does not offer to. */
/* A repository name is unique only inside its account: two organisations can
   each have a `guava`, and a flat list renders both as "guava" with no way to
   tell them apart. The account is the group label. */
/* Adding a source is one field, so the field and its button share a line. They
   did not: the hint lives inside the field block, so anything aligned to that
   block's bottom edge lands under the input rather than beside it. */
console.log('\n=== adding a source is one line and one field ===');
{
  await page.goto(BASE + '/sources', { waitUntil: 'domcontentloaded' });
  await settle();
  const m = await page.evaluate(() => {
    const i = document.querySelector('#source-url');
    const b = document.querySelector('#source-lookup');
    if (!i || !b) return null;
    const ib = i.getBoundingClientRect();
    const bb = b.getBoundingClientRect();
    return {
      sameLine: Math.abs(ib.top - bb.top) < 2,
      sameHeight: Math.abs(ib.height - bb.height) < 2,
      gap: Math.round(bb.left - ib.right),
      // The old form had six controls. One is the whole point.
      inputs: document.querySelectorAll('.card .form input:not([hidden])').length,
      hintBelow: document.querySelector('.url-add .field-hint')
                   .getBoundingClientRect().top >= ib.bottom - 1,
    };
  });
  if (m && m.sameLine && m.sameHeight && m.gap > 0 && m.gap < 24 && m.hintBelow) {
    ok('source add row', `input and button share a line, ${m.gap}px apart`);
  } else {
    bad('source add row', JSON.stringify(m));
  }
  if (m && m.inputs <= 2) {
    ok('source add fields', `${m.inputs} visible field(s), not a form of six`);
  } else {
    bad('source add fields', `${m && m.inputs} visible inputs`);
  }

  // The token field is hidden until it is the answer to something.
  const before = await page.evaluate(() => document.querySelector('.token-box').hidden);
  await page.click('.url-add .linkish');
  const after = await page.evaluate(() => document.querySelector('.token-box').hidden);
  if (before && !after) {
    ok('token field', 'hidden until asked for');
  } else {
    bad('token field', `hidden before=${before} after=${after}`);
  }
}

/* A repository is *in* an account, so the corpus-wide list is grouped by one.
   240 undifferentiated rows make it look like a bag of names, and the grouping
   is the same rule the rest of the IA runs on. */
console.log('\n=== repositories are grouped by source ===');
{
  await page.goto(BASE + '/repos', { waitUntil: 'domcontentloaded' });
  await settle();
  const shut = await page.evaluate(() => ({
    groups: document.querySelectorAll('.repo-group').length,
    open: document.querySelectorAll('.repo-group[open]').length,
    rows: document.querySelectorAll('table.data tbody tr').length,
    hosts: [...new Set([...document.querySelectorAll('.repo-group-host')]
      .map((h) => h.textContent))].length,
    flatTable: !!document.querySelector('.card > .card-body > .table-wrap'),
  }));
  if (shut.groups > 1 && !shut.flatTable) {
    ok('repositories grouped', `${shut.groups} sources, ${shut.hosts} host(s)`);
  } else {
    bad('repositories grouped', JSON.stringify(shut));
  }
  // Sixty-six tables built up front is a lot of DOM for a page where most stay
  // shut, so nothing is rendered until a group is opened.
  if (shut.rows === 0) {
    ok('groups build lazily', 'no rows rendered while every group is shut');
  } else {
    bad('groups build lazily', `${shut.rows} rows rendered with nothing open`);
  }

  await page.click('.repo-group .repo-group-head');
  await new Promise((r) => setTimeout(r, 350));
  const opened = await page.evaluate(() =>
    document.querySelectorAll('.repo-group[open] table.data tbody tr').length);
  if (opened > 0) {
    ok('a group opens', `${opened} repositories`);
  } else {
    bad('a group opens', 'no rows after expanding');
  }

  // A filter is a search: what it finds must not sit behind a shut triangle.
  await page.goto(BASE + '/repos?q=inkscape', { waitUntil: 'domcontentloaded' });
  await settle();
  const found = await page.evaluate(() => ({
    groups: document.querySelectorAll('.repo-group').length,
    open: document.querySelectorAll('.repo-group[open]').length,
  }));
  if (found.groups > 0 && found.groups === found.open) {
    ok('a filter opens its results', `${found.groups} matching source(s), all open`);
  } else {
    bad('a filter opens its results', JSON.stringify(found));
  }
}

console.log('\n=== a repository picker is grouped by account ===');
{
  await page.goto(BASE + '/insights/risk', { waitUntil: 'domcontentloaded' });
  await settle();
  await page.click('.toolbar .picker-trigger');
  await new Promise((r) => setTimeout(r, 250));
  const shape = await page.evaluate(() => ({
    groups: [...document.querySelectorAll('.picker-group')].map((g) => g.textContent),
    options: document.querySelectorAll('.picker-option').length,
    searchable: !!document.querySelector('.picker-search'),
  }));
  if (shape.groups.length > 1 && shape.options > shape.groups.length && shape.searchable) {
    ok('repository picker', `${shape.groups.length} accounts, ${shape.options} options, searchable`);
  } else {
    bad('repository picker', JSON.stringify(shape));
  }

  // Typing narrows it, which is the whole reason it is not a native select.
  await page.type('.picker-search', 'guav');
  await new Promise((r) => setTimeout(r, 250));
  const filtered = await page.evaluate(
    () => [...document.querySelectorAll('.picker-option')].map((o) => o.textContent));
  if (filtered.length && filtered.length < shape.options) {
    ok('picker search', `"guav" narrows ${shape.options} to ${filtered.length}`);
  } else {
    bad('picker search', `got ${filtered.length} of ${shape.options}`);
  }
}

console.log('\n=== a scope selector, where offered, holds its choice ===');
for (const path of ['/insights/shape', '/insights/shape/pair_support',
                    '/insights/risk', '/insights/drift', '/insights/impact']) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const trigger = await page.$('.toolbar .picker-trigger');
  if (!trigger) { ok(`${path} scope`, 'none offered, none needed'); continue; }

  await trigger.click();
  await new Promise((r) => setTimeout(r, 250));
  const chosen = await page.evaluate(() => {
    const opt = [...document.querySelectorAll('.picker-option')]
      .find((o) => !/^All /.test(o.textContent));
    if (!opt) return null;
    const label = opt.textContent;
    opt.click();
    return label;
  });
  if (!chosen) { ok(`${path} scope`, 'nothing to choose'); continue; }

  await settle();
  const held = await page.evaluate(() => ({
    label: document.querySelector('.toolbar .picker-value')?.textContent,
    url: location.search,
  }));
  if (held.label === chosen && /repo=\d+/.test(held.url)) {
    ok(`${path} scope`, `held ${chosen}`);
  } else {
    bad(`${path} scope`, `chose ${chosen}, got ${JSON.stringify(held)}`);
  }
}

console.log('\n=== nothing overflows its container horizontally ===');
for (const [path, label] of PAGES) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await settle();
  const over = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (over > 1) bad(`${label} width`, `${over}px of horizontal overflow`);
  else ok(`${label} width`, 'no horizontal scroll');
}

await browser.close();
if (problems.length) {
  console.log(`\nRESULT: ${problems.length} layout problem(s)`);
  process.exit(1);
}
console.log('\nRESULT: spacing consistent, nothing hidden that is still drawn, no overflow');

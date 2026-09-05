/* ==========================================================================
   Git Synapse UI.
   Plain ES modules, no build step and no framework, so the container needs no
   Node toolchain and the whole app is three files a reader can follow.

   Structure:
     - utils / API client
     - global state (active measure, catalog)
     - hash router; every view is a function returning a DOM node
     - shared components: sortable tables, stat tiles, bars, breadcrumbs
   ========================================================================== */

import { renderGraph } from './graph.js';

/* -------------------------------------------------------------- utils -- */

export const h = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
};

const $ = (sel, root = document) => root.querySelector(sel);

/** Compact integer formatting: 12345 -> "12.3k". */
export const num = (n) => {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (Math.abs(v) >= 1e4) return (v / 1e3).toFixed(1) + 'k';
  return v.toLocaleString();
};

/** Fixed-precision float that degrades gracefully on null. */
export const fx = (v, d = 3) =>
  v === null || v === undefined || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(d);

export const pct = (v) => {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  const n = Number(v) * 100;
  if (n === 0) return '0%';
  // Rounding a real probability to "0%" states it never happens. Below the
  // rounding floor, show enough digits to keep it distinguishable from zero.
  if (n < 0.5) return n < 0.05 ? '<0.1%' : n.toFixed(1) + '%';
  return n.toFixed(0) + '%';
};

export const bytes = (kb) => {
  if (!kb) return '—';
  if (kb >= 1048576) return (kb / 1048576).toFixed(2) + ' GB';
  if (kb >= 1024) return (kb / 1024).toFixed(1) + ' MB';
  return kb + ' KB';
};

export const when = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const days = (Date.now() - d.getTime()) / 86400000;
  if (days < 1) return 'today';
  if (days < 2) return 'yesterday';
  if (days < 31) return `${Math.floor(days)}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${(days / 365).toFixed(1)}y ago`;
};

export const dateStr = (iso) => (iso ? new Date(iso).toISOString().slice(0, 10) : '—');

/** Absolute local date + time — for logs/runs where the actual timestamp matters. */
export const stamp = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
};

/** Render a path with the directory dimmed and the basename emphasised. */
export const pathNode = (p) => {
  const i = String(p || '').lastIndexOf('/');
  const span = h('span', { class: 'path', title: p });
  if (i >= 0) {
    span.appendChild(h('span', { class: 'dir' }, p.slice(0, i + 1)));
    span.appendChild(h('span', { class: 'base' }, p.slice(i + 1)));
  } else {
    span.appendChild(h('span', { class: 'base' }, p || '—'));
  }
  return span;
};

/** Small inline magnitude bar; `norm` is expected in [0,1]. */
export const bar = (norm, cls = '') => {
  const clamped = Math.max(0, Math.min(1, Number(norm) || 0));
  return h('span', { class: `bar ${cls}` }, h('i', { style: `width:${clamped * 100}%` }));
};

export const toast = (msg, isError = false) => {
  const node = h('div', { class: `toast${isError ? ' err' : ''}` }, msg);
  $('#toasts').appendChild(node);
  setTimeout(() => node.remove(), isError ? 7000 : 3800);
};

/* ---------------------------------------------------------------- api -- */

/** Turn a failed response into an Error carrying the server's `detail`. */
async function failure(res) {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    if (Array.isArray(body.detail)) {
      // FastAPI validation errors arrive as a list of objects; String() on
      // them renders "[object Object]", which tells the user nothing.
      detail = body.detail
        .map((e) => `${(e.loc || []).slice(1).join('.') || 'input'}: ${e.msg || 'invalid'}`)
        .join('; ');
    } else if (typeof body.detail === 'string') {
      detail = body.detail;
    } else if (body.detail) {
      detail = JSON.stringify(body.detail);
    }
  } catch { /* non-JSON error body; keep the status line */ }
  return new Error(detail);
}

/** Fetch JSON from the API, surfacing the server's `detail` on failure. */
export async function api(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== null && v !== undefined && v !== '') url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw await failure(res);
  return res.json();
}

/** Send a mutating request. Same error surface as `api`. */
export async function apiSend(method, path, body) {
  const res = await fetch(new URL(path, window.location.origin), {
    method,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw await failure(res);
  return res.status === 204 ? null : res.json();
}

/* -------------------------------------------------------------- state -- */

export const state = {
  measure: localStorage.getItem('git-synapse.measure') || 'npmi',
  measures: [],
  byKey: new Map(),
  overview: null,
};

const QUICK_MEASURES = [
  'npmi', 'log_likelihood_ratio', 'confidence_ab', 'jaccard',
  'association_strength', 'phi', 'ochiai', 'fager',
];

export function setMeasure(key, { rerender = true } = {}) {
  if (!state.byKey.has(key)) return;
  state.measure = key;
  localStorage.setItem('git-synapse.measure', key);
  paintMeasureBar();
  if (rerender) route();
}

function paintMeasureBar() {
  const chips = $('#measure-chips');
  chips.replaceChildren(
    ...QUICK_MEASURES.filter((k) => state.byKey.has(k)).map((k) =>
      h(
        'button',
        {
          class: `chip${state.measure === k ? ' active' : ''}`,
          onclick: () => setMeasure(k),
          title: state.byKey.get(k).summary,
        },
        state.byKey.get(k).label.replace(/ \(.*\)/, ''),
      ),
    ),
  );

  const sel = $('#measure-select');
  if (!sel.options.length) {
    const families = new Map();
    for (const m of state.measures) {
      if (!families.has(m.family)) families.set(m.family, []);
      families.get(m.family).push(m);
    }
    sel.appendChild(h('option', { value: '' }, 'All measures…'));
    for (const [family, list] of families) {
      const group = h('optgroup', { label: family });
      for (const m of list) group.appendChild(h('option', { value: m.key }, m.label));
      sel.appendChild(group);
    }
    sel.addEventListener('change', () => sel.value && setMeasure(sel.value));
  }
  sel.value = QUICK_MEASURES.includes(state.measure) ? '' : state.measure;

  const spec = state.byKey.get(state.measure);
  $('#measure-hint').textContent = spec ? spec.summary : '';
}

/* ------------------------------------------------------------- router -- */

const routes = [];
/**
 * Register a client route.
 *
 * `:name` captures one segment; `*name` captures the rest of the path, slashes
 * included, so a file can be addressed by its own path -- /repos/5/files/src/
 * main/java/Cache.java. Ids renumber on a re-ingest, so a URL keyed on one
 * quietly comes to mean a different file; a path does not.
 */
const on = (pattern, handler) => {
  const keys = [];
  const rx = new RegExp(
    '^' +
      pattern.replace(/[:*]([a-zA-Z]+)/g, (m, k) => {
        keys.push(k);
        return m[0] === '*' ? '(.+)' : '([^/]+)';
      }) +
      '$',
  );
  routes.push({ rx, keys, handler });
};

/**
 * Navigate to an in-app route.
 *
 * Accepts either a clean path ("/insights") or the legacy hash form ("#/insights")
 * so older links keep working, and always pushes a real path so the address bar
 * reads /insights rather than /#/insights.
 */
export const go = (to) => {
  const path = String(to || '/').replace(/^#/, '') || '/';
  if (path === currentPath()) return;
  window.history.pushState({}, '', path);
  route();
};

/** Current in-app route: pathname plus query, normalised to start with "/". */
function currentPath() {
  return (window.location.pathname || '/') + (window.location.search || '');
}

/** Whether the chosen measure orders anything on the page being rendered. */
function measureRanksSomething(path, params) {
  if (path === '/') return true;                                  // top pairs
  if (/^\/repos\/\d+$/.test(path)) {
    return ['overview', 'pairs'].includes(params.tab || 'files');
  }
  // A file's partners, a pair's breakdown, a folder's coupled folders.
  if (/^\/repos\/\d+\/(files|pairs|tree)\//.test(path)) return true;
  if (path === '/insights/graph') return params.mode !== 'repos' && !!params.repo;
  return false;
}

/** Current view token, used to discard results from a superseded navigation. */
let navToken = 0;

async function route() {
  // Support a legacy #/foo URL by rewriting it to /foo once, so bookmarks and
  // anything that still emits hashes land on the right view.
  if (window.location.hash.startsWith('#/')) {
    const legacy = window.location.hash.slice(1);
    window.history.replaceState({}, '', legacy);
  }
  const raw = currentPath();
  const [rawPath, qs] = raw.split('?');
  // "/repos/4/tree/" and "/repos/4/tree" are the same address. Normalising here
  // means no route pattern has to tolerate it, and the address bar is tidied to
  // match without a history entry.
  const path = rawPath.length > 1 ? rawPath.replace(/\/+$/, '') || '/' : rawPath;
  if (path !== rawPath) {
    window.history.replaceState({}, '', path + (qs ? `?${qs}` : ''));
  }
  const params = Object.fromEntries(new URLSearchParams(qs || ''));

  // The measure bar ranks pairs. On a risk table, an ingest log or a list of
  // repositories it ranks nothing, and reads as a stray control the reader has
  // to wonder about -- so it appears only where changing it changes the page.
  const bar = document.getElementById('measure-bar');
  if (bar) bar.hidden = !measureRanksSomething(path, params);

  for (const link of document.querySelectorAll('#mainnav a')) {
    const target = (link.getAttribute('href') || '/').replace(/^#/, '');
    link.classList.toggle(
      'active',
      target === path || (target !== '/' && path.startsWith(target)),
    );
  }

  const view = $('#view');
  const token = ++navToken;

  for (const { rx, keys, handler } of routes) {
    const match = path.match(rx);
    if (!match) continue;
    const args = Object.fromEntries(keys.map((k, i) => [k, decodeURIComponent(match[i + 1])]));
    view.replaceChildren(h('div', { class: 'loading' }, h('span', { class: 'spinner' }), 'Loading…'));
    try {
      const node = await handler(args, params);
      if (token !== navToken) return; // a newer navigation won
      view.replaceChildren(node);
      window.scrollTo(0, 0);
    } catch (err) {
      if (token !== navToken) return;
      console.error(err);
      view.replaceChildren(
        h('div', { class: 'empty' }, h('strong', {}, 'Could not load this view'), String(err.message || err)),
      );
      toast(String(err.message || err), true);
    }
    return;
  }

  view.replaceChildren(notFound(`No view for ${path}`));
}

/** The empty state for something that was addressed but does not exist. */
const notFound = (detail) =>
  h('div', { class: 'empty' }, h('strong', {}, 'Not found'), detail);

/* -------------------------------------------------- shared components -- */

export const crumbs = (...items) =>
  h(
    'div',
    { class: 'crumbs' },
    items.flatMap(([label, href], i) => [
      i > 0 ? h('span', { class: 'sep' }, '/') : null,
      href ? h('a', { href, 'data-nav': true }, label) : h('span', {}, label),
    ]),
  );


/* An account owns repositories; a repository owns files and directories. The
   trail starts at whichever rung is known: without the account the reader
   cannot climb past a flat list of every repository in the corpus. Accounts are
   fetched once and reused -- a breadcrumb should not cost a request per page. */
let _accountsOnce = null;
const allAccounts = () => {
  if (!_accountsOnce) {
    _accountsOnce = api('/api/accounts').catch(() => ({ accounts: [] }));
  }
  return _accountsOnce;
};

const _repoOnce = new Map();
const repoById = (id) => {
  if (!_repoOnce.has(id)) _repoOnce.set(id, api(`/api/repos/${id}`).catch(() => null));
  return _repoOnce.get(id);
};

async function repoTrail(repo) {
  const trail = [];
  // A caller with only an id gets the rest looked up, so every page can build
  // the same trail without carrying repository fields it does not otherwise need.
  if (repo && repo.id && !repo.name) {
    repo = { ...(await repoById(repo.id)) || {}, ...repo };
  }
  let accountId = repo && (repo.account_id ?? repo.accountId);
  // Most endpoints return the repository id but not its account. Rather than
  // widen every one of them, resolve it here -- cached, so a breadcrumb costs
  // at most one request per repository per session.
  if (accountId == null && repo && repo.id) {
    accountId = (await repoById(repo.id))?.account_id ?? null;
  }
  if (accountId != null) {
    const { accounts } = await allAccounts();
    const account = (accounts || []).find((a) => a.id === accountId);
    if (account) {
      trail.push(['Accounts', '/accounts'], [account.login, `/accounts/${account.id}`]);
    }
  }
  if (!trail.length) trail.push(['Repositories', '/repos']);
  if (repo && repo.id) trail.push([repo.name || repo.full_name, `/repos/${repo.id}`]);
  return trail;
}

/* A path that names a repository the entity does not belong to is worse than no
   path at all: it reads as authoritative and is wrong. Ids are globally unique,
   so the view still renders the right thing -- the address is quietly corrected
   to match, without a history entry, so Back still goes where it should. */
function canonicalise(expectedPath) {
  const [current] = currentPath().split('?');
  if (current !== expectedPath) {
    const qs = window.location.search || '';
    window.history.replaceState({}, '', expectedPath + qs);
  }
}

export const pageHead = (title, sub, actions = []) =>
  h(
    'div',
    { class: 'page-head' },
    h('div', {}, h('h1', { class: 'page-title' }, title), sub ? h('p', { class: 'page-sub' }, sub) : null),
    actions.length ? h('div', { class: 'page-actions' }, ...actions) : null,
  );

export const statTile = (label, value, meta, onclick) =>
  h(
    'div',
    { class: 'stat', onclick: onclick || (() => {}), role: onclick ? 'button' : null },
    h('div', { class: 'stat-label' }, label),
    h('div', { class: 'stat-value' }, value),
    meta ? h('div', { class: 'stat-meta' }, meta) : null,
  );

export const card = (title, body, sub, headExtra) =>
  h(
    'div',
    { class: 'card' },
    h(
      'div',
      { class: 'card-head' },
      h('div', {}, h('h3', { class: 'card-title' }, title), sub ? h('div', { class: 'card-sub' }, sub) : null),
      headExtra || null,
    ),
    h('div', { class: 'card-body flush' }, body),
  );

/**
 * Build a sortable, clickable data table.
 *
 * @param {object[]} rows   data rows
 * @param {object[]} cols   {key, label, num, render, sort, width}
 * @param {object}   opts   {onRow, empty, initialSort, desc}
 */
export function dataTable(rows, cols, opts = {}) {
  const wrap = h('div', { class: 'table-wrap' });
  if (!rows || !rows.length) {
    wrap.appendChild(h('div', { class: 'empty' }, opts.empty || 'No rows'));
    return wrap;
  }

  let sortKey = opts.initialSort || null;
  let desc = opts.desc !== false;

  const table = h('table', { class: 'data' });
  const thead = h('thead');
  const tbody = h('tbody');
  table.append(thead, tbody);
  wrap.appendChild(table);

  const paint = () => {
    thead.replaceChildren(
      h(
        'tr',
        {},
        ...cols.map((c) =>
          h(
            'th',
            {
              class: `${c.num ? 'num ' : ''}${c.sortable === false ? '' : 'sortable'}`,
              style: c.width ? `width:${c.width}` : null,
              title: c.title || null,
              onclick:
                c.sortable === false
                  ? null
                  : () => {
                      if (sortKey === c.key) desc = !desc;
                      else {
                        sortKey = c.key;
                        desc = c.num !== false;
                      }
                      paint();
                    },
            },
            c.label,
            sortKey === c.key ? h('span', { class: 'arrow' }, desc ? '▾' : '▴') : null,
          ),
        ),
      ),
    );

    let data = rows;
    if (sortKey) {
      const col = cols.find((c) => c.key === sortKey);
      const get = (r) => (col && col.sort ? col.sort(r) : r[sortKey]);
      data = [...rows].sort((a, b) => {
        const x = get(a);
        const y = get(b);
        if (x === y) return 0;
        if (x === null || x === undefined) return 1;
        if (y === null || y === undefined) return -1;
        const cmp = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
        return desc ? -cmp : cmp;
      });
    }

    tbody.replaceChildren(
      ...data.map((row) => {
        const tr = h('tr', {
          onclick: (ev) => {
            if (ev.target.closest('a,button')) return;
            opts.onRow && opts.onRow(row);
          },
        });
        for (const c of cols) {
          const value = c.render ? c.render(row) : row[c.key];
          tr.appendChild(
            h('td', { class: c.num ? 'num' : c.mono ? 'mono' : '' }, value === null || value === undefined ? '—' : value),
          );
        }
        return tr;
      }),
    );
  };

  paint();
  return wrap;
}

/** Colour a score bar by whether the measure is signed and how it is doing. */
export function scoreCell(value, spec) {
  if (value === null || value === undefined) return '—';
  const v = Number(value);
  let norm;
  let cls = '';
  if (spec && spec.lower !== null && spec.upper !== null) {
    norm = (v - spec.lower) / (spec.upper - spec.lower);
    if (spec.signed && v < 0) cls = 'neg';
  } else {
    // Unbounded measure: compress with a log so the bar stays readable.
    norm = Math.min(1, Math.log10(Math.max(1, Math.abs(v))) / 3);
    cls = v < 0 ? 'neg' : 'mid';
  }
  const digits = Math.abs(v) >= 100 ? 1 : 3;
  return h(
    'div',
    { style: 'display:flex;align-items:center;gap:7px;justify-content:flex-end' },
    h('span', {}, v.toFixed(digits)),
    bar(norm, cls),
  );
}

/* ========================================================================
   Views
   ======================================================================== */

/* ------------------------------------------------------------ overview -- */

on('/', async () => {
  const [ov, repos, runs, top] = await Promise.all([
    api('/api/overview'),
    api('/api/repos', { limit: 12, order_by: 'commit_count' }),
    api('/api/runs', { limit: 5 }),
    api('/api/pairs', { measure: state.measure, limit: 12, min_support: 4 }),
  ]);
  state.overview = ov;
  paintFooter(ov);

  const wrap = h('div');
  wrap.append(
    pageHead(
      'Change coupling across the organisation',
      'Files that historically change together, derived from commit co-occurrence.',
      [
        h('button', { class: 'btn primary', onclick: triggerRefresh }, 'Refresh data'),
        h('a', { class: 'btn', href: '/insights/impact', 'data-nav': true }, 'Cross-repo impact'),
        h('a', { class: 'btn', href: '/insights/graph?mode=repos', 'data-nav': true }, 'Repository graph'),
      ],
    ),
  );

  wrap.append(
    h(
      'div',
      { class: 'grid grid-stats' },
      statTile('Repositories', num(ov.repos), `${num(ov.repos_ready)} ready · ${num(ov.repos_failed)} failed`, () => go('/repos')),
      statTile('Commits', num(ov.commits), 'analysed', () => go('/repos')),
      statTile('File changes', num(ov.file_changes), 'atomic (commit × file) facts'),
      statTile('Files tracked', num(ov.files), `${num(ov.directories)} directories`, () => go('/repos')),
      statTile('Coupling pairs', num(ov.file_pairs), `${num(ov.dir_pairs)} directory pairs`, () => go('/repos')),
      statTile('Contributors', num(ov.authors), 'distinct authors'),
      statTile('Mirror size', bytes(ov.mirror_kb), 'bare git mirrors'),
      statTile('History span', ov.first_commit_at ? `${dateStr(ov.first_commit_at).slice(0, 4)}→` : '—', `to ${dateStr(ov.last_commit_at)}`),
    ),
  );

  const spec = state.byKey.get(state.measure);
  wrap.append(h('div', { class: 'section-title' }, 'Strongest couplings org-wide'));
  wrap.append(
    card(
      `Top pairs by ${spec ? spec.label : state.measure}`,
      dataTable(
        top.pairs,
        [
          { key: 'repo', label: 'Repository', render: (r) => h('span', { class: 'mono', style: 'font-size:11.5px' }, r.repo.split('/')[1]) },
          { key: 'path_a', label: 'File A', render: (r) => pathNode(r.path_a) },
          { key: 'path_b', label: 'File B', render: (r) => pathNode(r.path_b) },
          { key: 'n_ab', label: 'Together', num: true },
          { key: 'confidence_ab', label: 'P(B|A)', num: true, render: (r) => pct(r.confidence_ab) },
          { key: 'score', label: spec ? spec.label : 'Score', num: true, render: (r) => scoreCell(r.score, spec) },
        ],
        {
          initialSort: 'score',
          onRow: (r) => go(`/repos/${r.repo_id}/pairs/${r.file_a_id}/${r.file_b_id}`),
          empty: 'No pairs yet — run an ingest first.',
        },
      ),
      'Click any row for the full statistical breakdown',
    ),
  );

  wrap.append(h('div', { class: 'section-title' }, 'Repositories by history depth'));
  wrap.append(
    card(
      'Largest corpora',
      dataTable(
        repos.repos,
        [
          { key: 'full_name', label: 'Repository', render: (r) => h('span', { class: 'mono' }, r.full_name) },
          { key: 'primary_language', label: 'Language', render: (r) => (r.primary_language ? h('span', { class: 'badge muted' }, r.primary_language) : '—') },
          { key: 'commit_count', label: 'Commits', num: true, render: (r) => num(r.commit_count) },
          { key: 'file_count', label: 'Files', num: true, render: (r) => num(r.file_count) },
          { key: 'pair_count', label: 'Pairs', num: true, render: (r) => num(r.pair_count) },
          { key: 'ingest_status', label: 'Status', render: (r) => statusBadge(r.ingest_status) },
        ],
        { initialSort: 'commit_count', onRow: (r) => go(`/repos/${r.id}`) },
      ),
      'Click a repository to explore it',
      h('a', { class: 'btn sm', href: '/repos', 'data-nav': true }, 'All repositories'),
    ),
  );

  // Cross-repo and mining summaries, so the landing page reflects every layer.
  try {
    const mine = await api('/api/mining/overview');
    wrap.append(h('div', { class: 'section-title' }, 'Cross-repository & mining layers'));
    wrap.append(h('div', { class: 'grid grid-stats' },
      statTile('Impact edges', num(mine.impact_edges), `${num(mine.declared_edges)} declared`, () => go('/insights/impact')),
      statTile('Manifest bumps', num(mine.dep_bumps), 'ground-truth propagation', () => go('/insights/impact')),
      statTile('De-facto modules', num(mine.modules), `${num(mine.cross_dir_modules)} cross-directory`, () => go('/insights/modules')),
      statTile('Emerging coupling', num(mine.emerging), `${num(mine.decaying)} decaying`, () => go('/insights/drift')),
    ));
  } catch (err) {
    console.warn('cross-repo layers unavailable', err);
  }

  if (runs.runs.length) {
    wrap.append(h('div', { class: 'section-title' }, 'Recent ingest jobs'));
    wrap.append(card('Job history', runsTable(runs.runs), 'Daily refresh plus manual runs'));
  }

  return wrap;
});

const statusBadge = (status) => {
  const map = { ready: 'ok', failed: 'danger', ingesting: 'info', pending: 'muted' };
  return h('span', { class: `badge ${map[status] || 'muted'}` }, status || 'unknown');
};

const runsTable = (runs) =>
  dataTable(
    runs,
    [
      { key: 'id', label: 'Run', num: true },
      { key: 'kind', label: 'Kind', render: (r) => h('span', { class: 'badge muted' }, r.kind) },
      { key: 'trigger', label: 'Trigger' },
      { key: 'status', label: 'Status', render: (r) => h('span', { class: `badge ${r.status === 'success' ? 'ok' : r.status === 'failed' ? 'danger' : r.status === 'partial' ? 'warn' : 'info'}` }, r.status) },
      { key: 'started_at', label: 'Started', render: (r) => stamp(r.started_at) },
      { key: 'duration_s', label: 'Duration', num: true, render: (r) => (r.duration_s ? `${r.duration_s.toFixed(0)}s` : '—') },
      { key: 'repos_ok', label: 'OK', num: true },
      { key: 'repos_failed', label: 'Failed', num: true },
      { key: 'commits_added', label: 'Commits', num: true, render: (r) => num(r.commits_added) },
    ],
    { initialSort: 'id', onRow: (r) => go(`/jobs/${r.id}`) },
  );

async function triggerRefresh() {
  try {
    await fetch('/api/ingest/refresh', { method: 'POST' });
    toast('Ingest started — watch progress under Jobs');
    setTimeout(() => go('/jobs'), 700);
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

/* --------------------------------------------------------------- repos -- */

/* Registered twice: `/repos` is every repository, `/accounts/:id` is one
   account's. Same view, and the account arrives in the path rather than as a
   query, because it is a place in the hierarchy and not a filter over it. */
const reposView = async (args, params) => {
  const accountId = args.id ? Number(args.id) : null;
  const [repos, langs, accounts] = await Promise.all([
    api('/api/repos', { limit: 1000, search: params.q, language: params.lang,
                        status: params.status, account_id: accountId }),
    api('/api/repos/languages'),
    accountId ? api('/api/accounts') : Promise.resolve({ accounts: [] }),
  ]);
  const account = (accounts.accounts || []).find((a) => a.id === accountId);

  const wrap = h('div');
  if (account) {
    // An account owns repositories, so this is a rung of the hierarchy rather
    // than a filtered list that happens to look like one.
    wrap.append(crumbs(['Accounts', '/accounts'], [account.login]));
    wrap.append(pageHead(`${account.login} repositories`,
      `${repos.count} of the ${account.kind === 'org' ? 'organisation' : 'user'}'s repositories are scanned`));
  } else {
    wrap.append(pageHead('Repositories', `${repos.count} repositories in the corpus`));
  }

  const search = h('input', {
    class: 'input',
    type: 'search',
    placeholder: 'Filter repositories…',
    value: params.q || '',
    style: 'width:250px',
  });
  search.addEventListener('input', debounce(() => applyFilters(), 280));

  const langSel = h(
    'select',
    { class: 'input' },
    h('option', { value: '' }, 'All languages'),
    ...langs.languages.map((l) => h('option', { value: l.language, selected: params.lang === l.language }, `${l.language} (${l.n})`)),
  );
  const statusSel = h(
    'select',
    { class: 'input' },
    ...['', 'ready', 'failed', 'ingesting', 'pending'].map((s) =>
      h('option', { value: s, selected: params.status === s }, s || 'Any status'),
    ),
  );
  langSel.addEventListener('change', () => applyFilters());
  statusSel.addEventListener('change', () => applyFilters());

  const applyFilters = () => {
    const p = new URLSearchParams();
    if (search.value) p.set('q', search.value);
    if (langSel.value) p.set('lang', langSel.value);
    if (statusSel.value) p.set('status', statusSel.value);
    const base = accountId ? `/accounts/${accountId}` : '/repos';
    go(`${base}${p.toString() ? '?' + p : ''}`);
  };

  wrap.append(
    h(
      'div',
      { class: 'toolbar' },
      h('div', { class: 'field' }, search),
      h('div', { class: 'field' }, langSel),
      h('div', { class: 'field' }, statusSel),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn primary', onclick: triggerRefresh }, 'Refresh all'),
    ),
  );

  wrap.append(
    card(
      'All repositories',
      dataTable(
        repos.repos,
        [
          { key: 'full_name', label: 'Repository', render: (r) => h('div', {}, h('span', { class: 'mono', style: 'font-weight:550' }, r.name), r.description ? h('div', { style: 'font-size:11px;color:var(--text-faint);max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, r.description) : null) },
          { key: 'primary_language', label: 'Lang', render: (r) => (r.primary_language ? h('span', { class: 'badge muted' }, r.primary_language) : '—') },
          { key: 'is_private', label: 'Vis', render: (r) => h('span', { class: `badge ${r.is_private ? 'violet' : 'info'}` }, r.is_private ? 'private' : 'public') },
          { key: 'commit_count', label: 'Commits', num: true, render: (r) => num(r.commit_count) },
          { key: 'file_count', label: 'Files', num: true, render: (r) => num(r.file_count) },
          { key: 'pair_count', label: 'Pairs', num: true, render: (r) => num(r.pair_count) },
          { key: 'author_count', label: 'Authors', num: true, render: (r) => num(r.author_count) },
          { key: 'clone_mode', label: 'Mirror', render: (r) => h('span', { class: `badge ${r.has_churn ? 'muted' : 'warn'}`, title: r.has_churn ? 'Full clone: line-level churn available' : 'Blobless mirror: coupling is complete, but no line counts' }, r.clone_mode || '—') },
          { key: 'last_commit_at', label: 'Last commit', render: (r) => when(r.last_commit_at) },
          { key: 'ingest_status', label: 'Status', render: (r) => (r.ingest_error ? h('span', { class: 'badge danger', title: r.ingest_error }, 'failed') : statusBadge(r.ingest_status)) },
        ],
        { initialSort: 'commit_count', onRow: (r) => go(`/repos/${r.id}`), empty: 'No repositories match those filters.' },
      ),
    ),
  );
  return wrap;
};

on('/repos', reposView);
on('/accounts/:id', reposView);

/* ---------------------------------------------------------- repo detail -- */

on('/repos/:id', async ({ id }, params) => {
  const tab = params.tab || 'files';
  const repo = await api(`/api/repos/${id}`);

  const wrap = h('div');
  wrap.append(crumbs(...(await repoTrail({ ...repo, id: null })), [repo.name]));
  wrap.append(
    pageHead(
      h('span', { class: 'mono' }, repo.full_name),
      repo.description || 'No description',
      [
        repo.html_url ? h('a', { class: 'btn', href: repo.html_url, target: '_blank', rel: 'noopener' }, 'GitHub ↗') : null,
        h('a', { class: 'btn primary', href: `/insights/graph?repo=${repo.id}`, 'data-nav': true }, 'Coupling graph'),
      ].filter(Boolean),
    ),
  );

  wrap.append(
    h(
      'div',
      { class: 'grid grid-stats' },
      statTile('Commits', num(repo.commit_count), `${num(repo.pair_population)} pair-eligible`),
      statTile('Files', num(repo.file_count), `${num(repo.pair_count)} coupling pairs`, () => go(`/repos/${id}?tab=files`)),
      statTile('Authors', num(repo.author_count), 'distinct contributors'),
      statTile('Churn', repo.has_churn ? `+${num(repo.total_insertions)}` : 'n/a', repo.has_churn ? `−${num(repo.total_deletions)} lines` : 'blobless mirror'),
      statTile('Stars', num(repo.stargazers), `${num(repo.forks_count)} forks`),
      statTile('Mirror', bytes(repo.mirror_size_kb), repo.clone_mode || '—'),
      statTile('First commit', dateStr(repo.first_commit_at), `last ${when(repo.last_commit_at)}`),
      statTile('Last ingest', when(repo.last_ingest_at), repo.ingest_status),
    ),
  );

  // Tabs are what is *in* the repository. Every analysis derived from it lives
  // under Insights, scoped -- so no question has two homes.
  const tabs = h(
    'div',
    { class: 'tabs' },
    ...[
      ['files', 'Files'],
      ['pairs', 'Coupled pairs'],
      ['overview', 'Overview'],
      ['meta', 'Metadata'],
    ].map(([key, label]) =>
      h('button', { class: `tab${tab === key ? ' active' : ''}`, onclick: () => go(`/repos/${id}?tab=${key}`) }, label),
    ),
  );
  wrap.append(tabs);
  wrap.append(analyseBar(id));

  const body = h('div');
  wrap.append(body);

  if (repo.ingest_error) {
    body.append(h('div', { class: 'help', style: 'border-left-color:var(--danger)' }, `Last ingest failed: ${repo.ingest_error}`));
  }

  const spec = state.byKey.get(state.measure);

  if (tab === 'overview') {
    const [hot, pairs] = await Promise.all([
      api(`/api/repos/${id}/hotspots`, { limit: 15 }),
      api(`/api/repos/${id}/pairs`, { measure: state.measure, limit: 15, min_support: 3 }),
    ]);
    body.append(
      h(
        'div',
        { class: 'grid grid-2' },
        card('Change hotspots', hotspotTable(hot.hotspots), 'Files that change most often'),
        card(`Strongest coupling by ${spec ? spec.label : state.measure}`, pairTable(pairs.pairs, spec), 'Click a row for the breakdown'),
      ),
    );
  } else if (tab === 'pairs') {
    const pairs = await api(`/api/repos/${id}/pairs`, { measure: state.measure, limit: 300, min_support: 2 });
    body.append(
      h('div', { class: 'help' }, `Ranked by ${spec ? spec.label : state.measure}. ${spec ? spec.detail : ''}`),
      card(
        pairs.pairs.length >= 300
          ? `Top 300 pairs by ${spec ? spec.label : state.measure}`
          : `${pairs.pairs.length} pairs`,
        pairTable(pairs.pairs, spec, true)),
    );
  } else if (tab === 'files') {
    body.append(await treePanel(id, '', params));
  } else {
    body.append(metadataPanel(repo));
  }

  return wrap;
});

/** Impact rows, with the evidence tier always visible. */
const impactTable = (rows, otherKey, selfId) =>
  dataTable(rows, [
    { key: 'name', label: 'Repository', render: (r) => h('span', { class: 'mono', style: 'font-weight:550' }, r.name) },
    { key: 'evidence', label: 'Evidence', sortable: false, render: (r) => tierBadge(r) },
    { key: 'bump_count', label: 'Bumps', num: true },
    { key: 'median_adoption_days', label: 'Adopted after', num: true, render: (r) => adoptedAfter(r.median_adoption_days) },
    { key: 'score', label: 'Score', num: true, render: (r) => h('div', { class: 'confbar', style: 'justify-content:flex-end' }, h('span', {}, fx(r.score, 3)), bar(r.score)) },
  ], {
    initialSort: 'score',
    onRow: (r) => {
      const other = r[otherKey];
      const a = otherKey === 'source_repo_id' ? other : selfId;
      const b = otherKey === 'source_repo_id' ? selfId : other;
      go(`/insights/impact/${a}/${b}`);
    },
    empty: 'No cross-repository edges recorded.',
  });

/** Repository-level impact graph: nodes are repos, edges are validated impact. */
async function repoImpactGraphView(params) {
  const minScore = Number(params.min || 0.4);
  const validated = params.all !== '1';
  const data = await api('/api/impact/graph', { min_score: minScore, validated_only: validated, limit: 600 });

  // The same frame as every other Insights section, so the tab bar does not
  // vanish when the graph switches from files to repositories.
  const wrap = await insightsShell('graph', null);
  wrap.append(
    h('div', { class: 'toolbar' },
      h('button', { class: 'btn primary' }, 'All repositories'),
      // Zooming to files needs a repository, and Scope is where one is chosen;
      // a link here with nothing scoped would land back on this same view.
      h('span', { class: 'card-sub' },
        'Choose a repository in Scope to zoom to its files.'),
      h('span', { class: 'spacer' }),
      h('span', { class: 'card-sub' },
        'Nodes are repositories; edges are directional impact.')),
  );

  const scoreInput = h('input', { class: 'input', type: 'range', min: '0', max: '0.95', step: '0.05', value: String(minScore), style: 'width:150px' });
  const scoreLabel = h('span', { class: 'card-sub', style: 'min-width:76px' }, `min ${minScore.toFixed(2)}`);
  scoreInput.addEventListener('input', () => (scoreLabel.textContent = `min ${Number(scoreInput.value).toFixed(2)}`));
  scoreInput.addEventListener('change', () => go(`/insights/graph?mode=repos&min=${scoreInput.value}&all=${validated ? '0' : '1'}`));

  wrap.append(h('div', { class: 'toolbar' },
    h('div', { class: 'field' }, h('label', {}, 'Min score'), scoreInput, scoreLabel),
    h('button', { class: `btn${validated ? ' primary' : ''}`, onclick: () => go(`/insights/graph?mode=repos&min=${minScore}&all=0`) }, 'Validated only'),
    h('button', { class: `btn${validated ? '' : ' primary'}`, onclick: () => go(`/insights/graph?mode=repos&min=${minScore}&all=1`) }, 'Include discovery'),
    h('span', { class: 'spacer' }),
    h('span', { class: 'card-sub' }, `${data.stats.node_count} repos, ${data.stats.edge_count} edges`)));

  if (!data.nodes.length) {
    wrap.append(h('div', { class: 'empty' }, h('strong', {}, 'Nothing to draw'), 'Lower the minimum score.'));
    return wrap;
  }

  const shell = h('div', { class: 'graph-shell' });
  wrap.append(shell);
  requestAnimationFrame(() => renderGraph(shell, data, {
    measureLabel: 'impact score',
    centerId: null,
    onNodeClick: (id) => go(`/insights/impact?repo=${id}&dir=upstream`),
    onNodeFocus: (id) => go(`/repos/${id}`),
  }));
  wrap.append(h('div', { class: 'help', style: 'margin-top:13px' },
    'Click a repository to open its impact view; shift-click for its detail page. ',
    validated
      ? 'Showing only edges with declared-dependency or manifest-bump evidence.'
      : 'Discovery edges included — these are statistical only and skew toward busy repositories.'));
  return wrap;
}

const hotspotTable = (rows) =>
  dataTable(
    rows,
    [
      { key: 'path', label: 'File', render: (r) => pathNode(r.path) },
      { key: 'change_count', label: 'Changes', num: true, render: (r) => num(r.change_count) },
      { key: 'author_count', label: 'Authors', num: true },
      { key: 'partner_count', label: 'Partners', num: true, render: (r) => num(r.partner_count) },
      { key: 'last_change_at', label: 'Last', render: (r) => when(r.last_change_at) },
    ],
    { initialSort: 'change_count', onRow: (r) => go(`/repos/${r.repo_id}/files/${r.path}`), empty: 'No files ingested yet.' },
  );

const pairTable = (rows, spec, wide = false) =>
  dataTable(
    rows,
    [
      { key: 'path_a', label: 'File A', render: (r) => pathNode(r.path_a) },
      { key: 'path_b', label: 'File B', render: (r) => pathNode(r.path_b) },
      { key: 'n_ab', label: 'Together', num: true },
      wide ? { key: 'n_a', label: 'A total', num: true } : null,
      wide ? { key: 'n_b', label: 'B total', num: true } : null,
      { key: 'confidence_ab', label: 'P(B|A)', num: true, render: (r) => pct(r.confidence_ab) },
      wide ? { key: 'confidence_ba', label: 'P(A|B)', num: true, render: (r) => pct(r.confidence_ba) } : null,
      wide ? { key: 'log_likelihood_ratio', label: 'G²', num: true, render: (r) => fx(r.log_likelihood_ratio, 1) } : null,
      { key: 'score', label: spec ? spec.label : 'Score', num: true, render: (r) => scoreCell(r.score, spec) },
    ].filter(Boolean),
    { initialSort: 'score', onRow: (r) => go(`/repos/${r.repo_id}/pairs/${r.file_a_id}/${r.file_b_id}`), empty: 'No pairs above the support threshold.' },
  );

const fileTable = (rows, empty) => dataTable(
  rows,
  [
    { key: 'path', label: 'Path', render: (f) => h('span', {}, pathNode(f.path),
        f.is_deleted ? h('span', { class: 'badge muted', style: 'margin-left:6px' }, 'deleted') : null) },
    { key: 'extension', label: 'Ext', render: (f) => (f.extension ? h('span', { class: 'badge muted' }, f.extension) : '\u2014') },
    { key: 'change_count', label: 'Changes', num: true, render: (f) => num(f.change_count) },
    { key: 'insertions', label: '+', num: true, render: (f) => num(f.insertions) },
    { key: 'deletions', label: '\u2212', num: true, render: (f) => num(f.deletions) },
    { key: 'author_count', label: 'Authors', num: true },
    { key: 'last_change_at', label: 'Last change', render: (f) => when(f.last_change_at) },
  ],
  { initialSort: 'change_count', onRow: (f) => go(`/repos/${f.repo_id}/files/${f.path}`), empty },
);

/* Links to every analysis of one repository. They are links rather than tabs
   because the answers live under Insights: a tab that navigates elsewhere is a
   lie about where you are, and two homes for one question is what made the
   navigation feel scattered. */
const analyseBar = (repoId) =>
  h('div', { class: 'toolbar' },
    h('span', { class: 'card-sub' }, 'Analyse:'),
    ...[
      ['Cross-repo impact', `/insights/impact?repo=${repoId}`],
      ['Coupling graph', `/insights/graph?repo=${repoId}`],
      ['Risk', `/insights/risk?repo=${repoId}`],
      ['Coupling drift', `/insights/drift?repo=${repoId}`],
      ['De-facto modules', `/insights/modules?repo=${repoId}`],
    ].map(([label, href]) => h('a', { class: 'btn', href, 'data-nav': true }, label)));

/* Folders and files as rows of one table, so a repository is browsed the way it
   is laid out. Sorting by churn floats the busiest folder to the top, which is
   how you would actually hunt; folders aggregate their subtree, so they tend to
   sort above the files beside them without needing to be pinned there. */
const treeTable = (repoId, tree) => dataTable(
  [
    ...tree.directories.map((d) => ({
      kind: 'dir', id: d.id, name: `${d.path.split('/').pop()}/`, target: d.path,
      file_count: d.file_count, change_count: d.change_count,
      insertions: d.insertions, deletions: d.deletions,
      author_count: null, last_change_at: d.last_change_at,
    })),
    ...tree.files.map((f) => ({
      kind: 'file', id: f.id, name: f.basename, target: f.path,
      file_count: null, change_count: f.change_count,
      insertions: f.insertions, deletions: f.deletions,
      author_count: f.author_count, last_change_at: f.last_change_at,
      is_deleted: f.is_deleted,
    })),
  ],
  [
    { key: 'name', label: 'Name', render: (r) => h('span', { class: 'path' },
        h('span', { class: 'dir' }, r.kind === 'dir' ? '\u{1F4C1}\u00a0' : '\u{1F4C4}\u00a0'),
        h('span', { class: 'base' }, r.name),
        r.is_deleted ? h('span', { class: 'badge muted', style: 'margin-left:6px' }, 'deleted') : null) },
    { key: 'file_count', label: 'Files', num: true, render: (r) => (r.kind === 'dir' ? num(r.file_count) : '\u2014') },
    { key: 'change_count', label: 'Changes', num: true, render: (r) => num(r.change_count) },
    { key: 'insertions', label: '+', num: true, render: (r) => num(r.insertions) },
    { key: 'deletions', label: '\u2212', num: true, render: (r) => num(r.deletions) },
    { key: 'author_count', label: 'Authors', num: true, render: (r) => (r.kind === 'file' ? r.author_count : '\u2014') },
    { key: 'last_change_at', label: 'Last change', render: (r) => when(r.last_change_at) },
  ],
  {
    initialSort: 'change_count',
    onRow: (r) => go(r.kind === 'dir'
      ? `/repos/${repoId}/tree/${r.target}`
      : `/repos/${repoId}/files/${r.target}`),
    empty: 'This directory has no recorded changes.',
  },
);

/**
 * The contents of one directory, with a search that escapes it.
 *
 * Descending a level at a time is tedious on a deep tree -- closure-compiler
 * has paths ten segments long -- so searching switches to flat results across
 * the whole subtree rather than filtering the current level only.
 */
async function treePanel(repoId, path, params) {
  const box = h('div');
  const term = params.f || '';

  const search = h('input', { class: 'input', type: 'search', style: 'width:300px',
    placeholder: path ? `Search under ${path}/\u2026` : 'Search all files\u2026', value: term });
  const apply = () => {
    const p = new URLSearchParams();
    if (search.value) p.set('f', search.value);
    if (params.tab) p.set('tab', params.tab);
    const base = path ? `/repos/${repoId}/tree/${path}` : `/repos/${repoId}?tab=files`;
    go(`${base}${p.toString() ? (base.includes('?') ? '&' : '?') + p : ''}`);
  };
  search.addEventListener('input', debounce(apply, 300));

  if (term) {
    const files = await api('/api/files', {
      repo_id: repoId, search: path ? `${path}/${term}` : term, limit: 1000, order_by: 'change_count',
    });
    box.append(h('div', { class: 'toolbar' }, search, h('span', { class: 'spacer' }),
      h('span', { class: 'card-sub' }, `${files.count} matching files`)));
    box.append(card('Search results', fileTable(files.files, 'Nothing matched that search.')));
    return box;
  }

  const tree = await api(`/api/repos/${repoId}/tree`, { path });
  box.append(h('div', { class: 'toolbar' }, search, h('span', { class: 'spacer' }),
    h('span', { class: 'card-sub' },
      `${tree.directories.length} folders, ${tree.files.length} files`)));
  box.append(card(path || 'Repository root', treeTable(repoId, tree)));

  // Directory-level coupling is computed independently of the file level, so it
  // is an answer about *this* folder and belongs on its page rather than in a
  // separate list of every directory in the repository.
  if (tree.directory) {
    const coupled = await api(`/api/directories/${tree.directory.id}/coupled`,
                              { measure: state.measure, limit: 30 });
    const spec = state.byKey.get(state.measure);
    box.append(card('Folders that change with this one',
      dataTable(coupled.partners, [
        { key: 'path', label: 'Directory', render: (d) => h('span', { class: 'mono' }, d.path || '<root>') },
        { key: 'file_count', label: 'Files', num: true },
        { key: 'n_ab', label: 'Together', num: true },
        { key: 'n_other', label: 'Its commits', num: true },
        { key: 'confidence_out', label: 'P(it|this)', num: true, render: (d) => pct(d.confidence_out) },
        { key: 'score', label: spec ? spec.label : state.measure, num: true,
          render: (d) => scoreCell(d.score, spec) },
      ], {
        initialSort: 'score',
        onRow: (d) => go(d.path ? `/repos/${repoId}/tree/${d.path}`
                                : `/repos/${repoId}?tab=files`),
        empty: 'No directory-level coupling recorded for this folder.',
      }),
      'A directory changes in a commit if any file beneath it changed.'));
  }
  return box;
}

function metadataPanel(repo) {
  const rows = [
    ['Full name', repo.full_name],
    ['GitHub id', repo.github_id],
    ['Default branch', repo.default_branch],
    ['Visibility', repo.visibility],
    ['License', repo.license_spdx],
    ['Topics', (repo.topics || []).join(', ') || '—'],
    ['Fork', String(repo.is_fork)],
    ['Archived', String(repo.is_archived)],
    ['Template', String(repo.is_template)],
    ['GitHub size', bytes(repo.disk_usage_kb)],
    ['Mirror size', bytes(repo.mirror_size_kb)],
    ['Clone mode', repo.clone_mode],
    ['Churn available', String(repo.has_churn)],
    ['Open issues', repo.open_issues],
    ['Watchers', repo.watchers],
    ['Created', dateStr(repo.github_created_at)],
    ['Pushed', dateStr(repo.github_pushed_at)],
    ['HEAD sha', repo.head_sha ? repo.head_sha.slice(0, 12) : '—'],
    ['Last ingested sha', repo.last_ingested_sha ? repo.last_ingested_sha.slice(0, 12) : '—'],
    ['Mirror path', repo.mirror_path],
    ['Ingest duration', repo.ingest_duration_s ? `${repo.ingest_duration_s.toFixed(1)}s` : '—'],
  ];
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of rows) {
    dl.append(h('dt', {}, k), h('dd', {}, v === null || v === undefined ? '—' : String(v)));
  }
  return h('div', { class: 'grid grid-2' }, card('Repository metadata', h('div', { class: 'card-body' }, dl)));
}

/* ---------------------------------------------------------- file detail -- */

/* Addressed by path under its repository, so the URL says where the file lives
   and survives a re-ingest. Renames resolve through the alias table, so a link
   to a path that has since moved still lands on the file it became -- and the
   address is then corrected to the current path. */
on('/repos/:repo/files/*path', async ({ repo, path }, params) => {
  const tab = params.tab || 'coupled';
  const repoId = Number(repo);
  const clean = path.replace(/^\/+/, '');
  const file = await api('/api/files/resolve', { repo_id: repoId, path: clean })
    .catch(() => null);
  if (!file) return notFound(`No file ${clean} in repository ${repo}`);
  canonicalise(`/repos/${file.repo_id}/files/${file.path}`);
  const spec = state.byKey.get(state.measure);
  const id = file.id;

  const wrap = h('div');
  wrap.append(
    crumbs(...(await repoTrail({ id: file.repo_id, name: file.repo.split('/')[1],
                                 account_id: file.account_id })),
           ...folderTrail(file.repo_id, file.dir_path || ''), [file.basename]),
  );
  wrap.append(
    pageHead(
      h('span', { class: 'mono' }, file.path),
      `${file.repo} · ${num(file.change_count)} changes by ${file.author_count} authors`,
      [
        h('a', { class: 'btn', href: `/insights/graph?repo=${file.repo_id}&center=${file.id}`, 'data-nav': true }, 'Graph neighbourhood'),
      ],
    ),
  );

  wrap.append(
    h(
      'div',
      { class: 'grid grid-stats' },
      statTile('Total changes', num(file.change_count), `${num(file.pair_change_count)} pair-eligible`),
      statTile('Insertions', file.insertions ? `+${num(file.insertions)}` : '—', file.deletions ? `−${num(file.deletions)}` : 'no churn data'),
      statTile('Authors', num(file.author_count), 'have touched this file'),
      statTile('First change', dateStr(file.first_change_at), `last ${when(file.last_change_at)}`),
      statTile('Population', num(file.pair_population), 'commits in contingency N'),
      statTile('State', file.is_deleted ? 'deleted' : 'active', file.extension ? `.${file.extension}` : 'no extension'),
    ),
  );

  wrap.append(
    h(
      'div',
      { class: 'tabs' },
      ...[
        ['coupled', 'Coupled files'],
        ['history', 'Commit history'],
        ['authors', 'Authors'],
      ].map(([key, label]) =>
        h('button', { class: `tab${tab === key ? ' active' : ''}`, onclick: () => go(`/repos/${file.repo_id}/files/${file.path}?tab=${key}`) }, label),
      ),
    ),
  );

  const body = h('div');
  wrap.append(body);

  if (tab === 'coupled') {
    const minSupport = Number(params.min || 2);
    const data = await api(`/api/files/${id}/coupled`, { measure: state.measure, limit: 200, min_support: minSupport });

    const supportInput = h('input', { class: 'input', type: 'number', min: '1', value: String(minSupport), style: 'width:78px' });
    supportInput.addEventListener('change', () => go(`/repos/${file.repo_id}/files/${file.path}?tab=coupled&min=${supportInput.value || 1}`));

    body.append(
      h(
        'div',
        { class: 'help' },
        `Ranked by ${spec ? spec.label : state.measure}. `,
        spec ? spec.detail : '',
        spec && spec.rare_item_bias ? h('strong', {}, ' Raise the minimum support to suppress coincidental pairs.') : null,
      ),
      h(
        'div',
        { class: 'toolbar' },
        h('div', { class: 'field' }, h('label', {}, 'Min shared commits'), supportInput),
        h('span', { class: 'spacer' }),
        h('span', { class: 'card-sub' },
          data.partners.length >= 200 ? `top ${data.partners.length} partners` : `${data.partners.length} partners`),
      ),
      card(
        'What changes together with this file',
        dataTable(
          data.partners,
          [
            { key: 'path', label: 'File', render: (p) => h('span', {}, pathNode(p.path), p.is_deleted ? h('span', { class: 'badge muted', style: 'margin-left:6px' }, 'deleted') : null) },
            { key: 'n_ab', label: 'Together', num: true, title: 'Commits where both files changed' },
            { key: 'n_other', label: 'Its total', num: true, title: "The partner's own change count" },
            { key: 'confidence_out', label: 'P(it|this)', num: true, title: 'Probability the partner changes given this file changed', render: (p) => h('div', { style: 'display:flex;align-items:center;gap:7px;justify-content:flex-end' }, h('span', {}, pct(p.confidence_out)), bar(p.confidence_out)) },
            { key: 'confidence_in', label: 'P(this|it)', num: true, render: (p) => pct(p.confidence_in) },
            { key: 'npmi', label: 'NPMI', num: true, render: (p) => fx(p.npmi, 3) },
            { key: 'log_likelihood_ratio', label: 'G²', num: true, render: (p) => fx(p.log_likelihood_ratio, 1) },
            { key: 'last_co_change', label: 'Last together', render: (p) => when(p.last_co_change) },
            { key: 'score', label: spec ? spec.label : state.measure, num: true, render: (p) => scoreCell(p.score, spec) },
          ],
          {
            initialSort: 'score',
            onRow: (p) => go(`/repos/${file.repo_id}/pairs/${id}/${p.other_id}`),
            empty: 'No coupling partners above the support threshold.',
          },
        ),
        'Click a row for the full 2×2 table and all 29 measures',
      ),
    );
  } else if (tab === 'history') {
    const data = await api(`/api/files/${id}/commits`, { limit: 200 });
    body.append(
      card(
        'Commit history',
        dataTable(
          data.commits,
          [
            { key: 'sha', label: 'SHA', mono: true, render: (c) => h('span', { class: 'mono' }, c.sha.slice(0, 9)) },
            { key: 'change_type', label: 'Type', render: (c) => h('span', { class: `badge ${c.change_type === 'A' ? 'ok' : c.change_type === 'D' ? 'danger' : c.change_type === 'R' ? 'violet' : 'muted'}` }, c.change_type) },
            { key: 'subject', label: 'Subject', render: (c) => h('span', { style: 'display:block;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, c.subject) },
            { key: 'author', label: 'Author' },
            { key: 'insertions', label: '+', num: true },
            { key: 'deletions', label: '−', num: true },
            { key: 'n_files', label: 'Files', num: true, title: 'Total files in that commit' },
            { key: 'committed_at', label: 'When', render: (c) => when(c.committed_at) },
          ],
          { initialSort: 'committed_at', empty: 'No commits recorded.' },
        ),
      ),
    );
  } else {
    const data = await api(`/api/files/${id}/authors`, { limit: 100 });
    body.append(
      card(
        'Who works on this file',
        dataTable(
          data.authors,
          [
            { key: 'display_name', label: 'Author' },
            { key: 'email', label: 'Email', mono: true },
            { key: 'n_commits', label: 'Commits', num: true },
            { key: 'insertions', label: '+', num: true, render: (a) => num(a.insertions) },
            { key: 'deletions', label: '−', num: true, render: (a) => num(a.deletions) },
            { key: 'first_at', label: 'First', render: (a) => dateStr(a.first_at) },
            { key: 'last_at', label: 'Last', render: (a) => when(a.last_at) },
          ],
          { initialSort: 'n_commits', empty: 'No author data.' },
        ),
      ),
    );
  }

  return wrap;
});

/* ---------------------------------------------------------- pair detail -- */

on('/repos/:repo/pairs/:a/:b', async ({ a, b }) => {
  const [detail, commits] = await Promise.all([
    api(`/api/pairs/${a}/${b}`),
    api(`/api/pairs/${a}/${b}/commits`, { limit: 30 }),
  ]);
  const c = detail.cells;

  const wrap = h('div');
  wrap.append(crumbs(...(await repoTrail({ id: detail.repo_id, name: detail.repo.split('/')[1],
                                           account_id: detail.account_id })), ['Pair']));
  wrap.append(
    pageHead(
      'Coupling breakdown',
      `${detail.repo} — every measure computed from one 2×2 contingency table`,
      [
        h('a', { class: 'btn', href: `/repos/${detail.repo_id}/files/${detail.path_a}`, 'data-nav': true }, 'Open file A'),
        h('a', { class: 'btn', href: `/repos/${detail.repo_id}/files/${detail.path_b}`, 'data-nav': true }, 'Open file B'),
      ],
    ),
  );

  wrap.append(
    h(
      'div',
      { class: 'grid grid-2', style: 'margin-bottom:14px' },
      card(
        'File A',
        h('div', { class: 'card-body' }, h('a', { href: `/repos/${detail.repo_id}/files/${detail.path_a}`, 'data-nav': true }, pathNode(detail.path_a)), h('div', { class: 'card-sub', style: 'margin-top:5px' }, `${num(c.n_a)} changes`)),
      ),
      card(
        'File B',
        h('div', { class: 'card-body' }, h('a', { href: `/repos/${detail.repo_id}/files/${detail.path_b}`, 'data-nav': true }, pathNode(detail.path_b)), h('div', { class: 'card-sub', style: 'margin-top:5px' }, `${num(c.n_b)} changes`)),
      ),
    ),
  );

  // The 2x2 table every measure is derived from.
  const cont = h(
    'div',
    { class: 'contingency' },
    h('div', { class: 'hd' }, ''),
    h('div', { class: 'hd' }, 'B changed'),
    h('div', { class: 'hd' }, 'B unchanged'),
    h('div', { class: 'hd' }, 'total'),
    h('div', { class: 'rh' }, 'A changed'),
    h('div', { class: 'cell a' }, h('b', {}, num(c.a)), h('span', {}, 'a — both')),
    h('div', { class: 'cell' }, h('b', {}, num(c.b)), h('span', {}, 'b — A only')),
    h('div', { class: 'tot' }, num(c.n_a)),
    h('div', { class: 'rh' }, 'A unchanged'),
    h('div', { class: 'cell' }, h('b', {}, num(c.c)), h('span', {}, 'c — B only')),
    h('div', { class: 'cell' }, h('b', {}, num(c.d)), h('span', {}, 'd — neither')),
    h('div', { class: 'tot' }, num(c.n_total - c.n_a)),
    h('div', { class: 'rh' }, 'total'),
    h('div', { class: 'tot' }, num(c.n_b)),
    h('div', { class: 'tot' }, num(c.n_total - c.n_b)),
    h('div', { class: 'tot' }, `N = ${num(c.n_total)}`),
  );

  const lift = detail.association_strength || 0;
  wrap.append(
    h(
      'div',
      { class: 'grid grid-2' },
      card(
        'Contingency table',
        h(
          'div',
          { class: 'card-body' },
          cont,
          h(
            'p',
            { style: 'margin:12px 0 0;font-size:12.5px;color:var(--text-dim);line-height:1.55' },
            `These files changed together ${num(c.a)} times out of ${num(c.n_total)} pair-eligible commits, against `,
            h('strong', {}, c.expected.toFixed(1)),
            ` expected under independence — `,
            h('strong', { style: 'color:var(--accent)' }, `${lift.toFixed(1)}×`),
            ' more often than chance.',
          ),
        ),
        'Every measure below is a function of exactly these four cells',
      ),
      card(
        'Directional reading',
        h(
          'div',
          { class: 'card-body' },
          directionRow(detail.path_a, detail.path_b, detail.confidence_ab),
          directionRow(detail.path_b, detail.path_a, detail.confidence_ba),
          h('dl', { class: 'kv', style: 'margin-top:14px' },
            h('dt', {}, 'First together'), h('dd', {}, dateStr(detail.first_co_change)),
            h('dt', {}, 'Last together'), h('dd', {}, `${dateStr(detail.last_co_change)} (${when(detail.last_co_change)})`),
            h('dt', {}, 'Distinct authors'), h('dd', {}, num(detail.distinct_authors)),
            h('dt', {}, 'Recency-weighted'), h('dd', {}, fx(detail.w_ab, 2)),
          ),
        ),
        'Coupling is rarely symmetric',
      ),
    ),
  );

  wrap.append(h('div', { class: 'section-title' }, 'All measures'));
  wrap.append(card('29 association measures + 2 directional', measuresTable(detail), 'Click a measure to make it the active ranking'));

  wrap.append(h('div', { class: 'section-title' }, 'Evidence'));
  wrap.append(
    card(
      (() => {
        // A commit above the fan-out cap changed both files but contributed to
        // no statistic. Saying so is the difference between evidence and a
        // number that quietly disagrees with the table above it.
        const shown = commits.commits.length;
        const uncounted = commits.commits.filter((c) => c.counted === false).length;
        const total = detail.n_ab || 0;
        let title = shown < total ? `${shown} of ${num(total)} commits where both files changed`
                                  : `${shown} commits where both files changed`;
        if (uncounted) title += ` — ${uncounted} above the fan-out cap, not counted in the score`;
        return title;
      })(),
      dataTable(
        commits.commits,
        [
          { key: 'sha', label: 'SHA', render: (x) => h('span', { class: 'mono' }, x.sha.slice(0, 9)) },
          { key: 'subject', label: 'Subject', render: (x) => h('span', { style: 'display:block;max-width:620px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, x.subject) },
          { key: 'author', label: 'Author' },
          { key: 'n_files', label: 'Files', num: true },
          { key: 'insertions', label: '+', num: true },
          { key: 'deletions', label: '−', num: true },
          { key: 'committed_at', label: 'When', render: (x) => when(x.committed_at) },
        ],
        { initialSort: 'committed_at', empty: 'No shared commits found.' },
      ),
      'The raw facts the statistics are computed from',
    ),
  );

  return wrap;
});

const directionRow = (from, to, confidence) =>
  h(
    'div',
    { style: 'margin-bottom:11px' },
    h(
      'div',
      { style: 'font-size:12px;color:var(--text-dim);margin-bottom:3px' },
      'when ',
      h('span', { class: 'mono', style: 'color:var(--text)' }, from.split('/').pop()),
      ' changes, ',
      h('span', { class: 'mono', style: 'color:var(--text)' }, to.split('/').pop()),
      ' changes',
    ),
    h(
      'div',
      { style: 'display:flex;align-items:center;gap:10px' },
      h('span', { style: 'font-size:21px;font-weight:620;font-family:var(--mono);min-width:62px' }, pct(confidence)),
      bar(confidence),
    ),
  );

function measuresTable(detail) {
  const rows = state.measures
    .filter((m) => detail[m.key] !== null && detail[m.key] !== undefined)
    .map((m) => ({ ...m, value: detail[m.key] }));

  return dataTable(
    rows,
    [
      { key: 'label', label: 'Measure', render: (m) => h('div', {}, h('span', { style: `font-weight:${m.key === state.measure ? 650 : 500};color:${m.key === state.measure ? 'var(--accent)' : 'inherit'}` }, m.label), h('div', { style: 'font-size:11px;color:var(--text-faint)' }, m.summary)) },
      { key: 'family', label: 'Family', render: (m) => h('span', { class: 'badge muted' }, m.family.replace(/ .*/, '')) },
      { key: 'formula', label: 'Formula', render: (m) => h('code', { style: 'font-family:var(--mono);font-size:11px;color:var(--info)' }, m.formula) },
      { key: 'value', label: 'Value', num: true, render: (m) => scoreCell(m.value, m) },
      { key: 'range', label: 'Range', sortable: false, render: (m) => h('span', { style: 'font-family:var(--mono);font-size:11px;color:var(--text-faint)' }, `${m.lower ?? '−∞'} … ${m.upper ?? '∞'}`) },
      { key: 'flags', label: 'Notes', sortable: false, render: (m) => h('div', { class: 'pillrow' },
          m.hit_rate != null ? h('span', { class: 'badge ok' }, (m.hit_rate * 100).toFixed(1) + '%') : null,
          m.is_significance ? h('span', { class: 'badge info' }, 'significance') : null,
          m.rare_item_bias ? h('span', { class: 'badge warn' }, 'rare-item bias') : null,
          m.saturates_on_sparse ? h('span', { class: 'badge muted' }, 'saturates') : null,
        ) },
    ],
    { initialSort: 'family', desc: false, onRow: (m) => setMeasure(m.key) },
  );
}

/* ----------------------------------------------------- directory detail -- */

/* A folder inside a repository. The path is in the URL rather than a directory
   id because ids renumber on a re-ingest: a link keyed on one silently comes to
   mean a different folder, which is worse than failing. */
on('/repos/:repo/tree/*path', async ({ repo, path }, params) => {
  const repoId = Number(repo);
  const clean = path.replace(/^\/+|\/+$/g, '');
  const info = await repoById(repoId);
  if (!info) return notFound(`No repository ${repo}`);

  const wrap = h('div');
  wrap.append(crumbs(...(await repoTrail(info)), ...folderTrail(repoId, clean)));
  wrap.append(pageHead(
    h('span', { class: 'mono' }, clean),
    `In ${info.full_name}`,
    [h('a', { class: 'btn', href: `/repos/${repoId}`, 'data-nav': true }, 'Repository root'),
     h('a', { class: 'btn', href: `/insights/graph?repo=${repoId}`, 'data-nav': true }, 'Coupling graph')]));
  wrap.append(await treePanel(repoId, clean, params));
  return wrap;
});

// The repository root is a folder too, but it already has an address: the
// repository itself. Redirect rather than render it twice.
on('/repos/:repo/tree', ({ repo }) => {
  go(`/repos/${repo}?tab=files`);
  return h('div');
});

/** Every ancestor of a folder, each one clickable -- the way back up. */
const folderTrail = (repoId, path) => {
  const segs = path ? path.split('/') : [];
  return segs.map((seg, i) => [
    seg,
    i === segs.length - 1 ? null : `/repos/${repoId}/tree/${segs.slice(0, i + 1).join('/')}`,
  ]);
};

/* Both graphs live here: files within one repository, and repositories across
   the corpus. Neither owns data -- each draws couplings computed elsewhere --
   but drawing them is a question in its own right, so it is a section rather
   than a button hidden on a page. */
on('/insights/graph', async (_args, params) => {
  // No repository chosen means the question is about the corpus, so that is
  // what is drawn. Choosing one in Scope zooms to its files.
  if (params.mode === 'repos' || (!params.repo && params.mode !== 'files')) {
    return repoImpactGraphView(params);
  }
  const repoId = Number(params.repo);
  const wrap = await insightsShell('graph', repoId || null);
  wrap.append(
    h('div', { class: 'toolbar' },
      h('a', { class: 'btn', href: '/insights/graph?mode=repos', 'data-nav': true },
        'All repositories'),
      h('button', { class: 'btn primary' }, 'Files in this repository')),
  );
  if (!repoId) {
    wrap.append(h('div', { class: 'empty' }, h('strong', {}, 'Pick a repository'),
      'A file graph is drawn for one repository at a time. Choose one in Scope.'));
    return wrap;
  }
  if (!repoId) {
    wrap.append(h('div', { class: 'empty' }, h('strong', {}, 'No coupling data yet'),
      'Run an ingest to populate the graph.'));
    return wrap;
  }
  const edgeCount = h('input', { class: 'input', type: 'range', min: '20', max: '600', step: '20', value: params.limit || '160', style: 'width:130px' });
  const edgeLabel = h('span', { class: 'card-sub', style: 'min-width:66px' }, `${params.limit || 160} edges`);
  const minSupport = h('input', { class: 'input', type: 'number', min: '1', value: params.min || '3', style: 'width:70px' });

  const navigate = () => {
    const p = new URLSearchParams({ repo: String(repoId), limit: edgeCount.value, min: minSupport.value });
    if (params.center) p.set('center', params.center);
    go(`/insights/graph?${p}`);
  };
  minSupport.addEventListener('change', navigate);
  edgeCount.addEventListener('input', () => (edgeLabel.textContent = `${edgeCount.value} edges`));
  edgeCount.addEventListener('change', navigate);

  wrap.append(
    h(
      'div',
      { class: 'toolbar' },
      h('div', { class: 'field' }, h('label', {}, 'Edges'), edgeCount, edgeLabel),
      h('div', { class: 'field' }, h('label', {}, 'Min support'), minSupport),
      params.center
        ? h('button', { class: 'btn sm', onclick: () => go(`/insights/graph?repo=${repoId}&limit=${edgeCount.value}&min=${minSupport.value}`) }, 'Clear focus')
        : null,
      h('span', { class: 'spacer' }),
    ),
  );

  const data = await api(`/api/repos/${repoId}/graph`, {
    measure: state.measure,
    limit: Number(params.limit || 160),
    min_support: Number(params.min || 3),
    center_file_id: params.center || undefined,
  });

  if (!data.nodes.length) {
    wrap.append(h('div', { class: 'empty' }, h('strong', {}, 'Nothing to draw'), 'No pairs matched those thresholds — lower the minimum support.'));
    return wrap;
  }

  const spec = state.byKey.get(state.measure);
  const shell = h('div', { class: 'graph-shell' });
  wrap.append(shell);
  // The graph module owns its own canvas, simulation and interaction loop.
  requestAnimationFrame(() =>
    renderGraph(shell, data, {
      measureLabel: spec ? spec.label : state.measure,
      centerId: params.center ? Number(params.center) : null,
      // Nodes carry their path, and files are addressed by path.
      onNodeClick: (nodeId) => {
        const node = data.nodes.find((n) => n.id === nodeId);
        if (node) go(`/repos/${repoId}/files/${node.path}`);
      },
      onNodeFocus: (nodeId) => go(`/insights/graph?repo=${repoId}&limit=${params.limit || 160}&min=${params.min || 3}&center=${nodeId}`),
    }),
  );

  wrap.append(
    h(
      'div',
      { class: 'help', style: 'margin-top:13px' },
      `${data.nodes.length} files, ${data.edges.length} couplings, ranked by ${spec ? spec.label : state.measure}. `,
      'Node size reflects how often a file changes; edge thickness reflects the coupling score. ',
      'Click a node to open the file, or shift-click to re-centre the graph on it.',
    ),
  );
  return wrap;
});

/* ------------------------------------------------------------- measures -- */

on('/measures', async () => {
  const wrap = h('div');
  wrap.append(
    pageHead(
      'Association measures',
      `${state.measures.length} measures, each a pure function of the same 2×2 contingency table. Click one to make it the active ranking.`,
    ),
  );
  wrap.append(
    h(
      'div',
      { class: 'help' },
      'Given two files A and B over N commits: ',
      h('strong', {}, 'a'), ' = both changed, ',
      h('strong', {}, 'b'), ' = only A, ',
      h('strong', {}, 'c'), ' = only B, ',
      h('strong', {}, 'd'), ' = neither. ',
      'Because only these counts are stored, any measure can be recomputed at any time without re-reading git.',
    ),
  );

  const families = new Map();
  for (const m of state.measures) {
    if (!families.has(m.family)) families.set(m.family, []);
    families.get(m.family).push(m);
  }

  for (const [family, list] of families) {
    wrap.append(h('div', { class: 'section-title' }, `${family} — ${list.length}`));
    wrap.append(
      h(
        'div',
        { class: 'measure-grid' },
        ...list.map((m) =>
          h(
            'div',
            { class: `measure-card${m.key === state.measure ? ' active' : ''}`, onclick: () => setMeasure(m.key) },
            h('h4', {}, m.label),
            h('div', { class: 'formula' }, m.formula),
            h('p', {}, m.detail),
            h(
              'div',
              { class: 'flags' },
              h('span', { class: 'badge muted' }, `range ${m.lower ?? '−∞'} … ${m.upper ?? '∞'}`),
              m.hit_rate != null ? h('span', { class: 'badge ok' }, (m.hit_rate * 100).toFixed(1) + '%') : null,
              m.is_significance ? h('span', { class: 'badge info' }, 'significance test') : null,
              m.signed ? h('span', { class: 'badge violet' }, 'signed') : null,
              m.rare_item_bias ? h('span', { class: 'badge warn' }, 'rare-item bias') : null,
              m.saturates_on_sparse ? h('span', { class: 'badge muted' }, 'saturates on sparse data') : null,
            ),
          ),
        ),
      ),
    );
  }
  return wrap;
});

/* ----------------------------------------------------------------- jobs -- */

on('/jobs', async () => {
  const [runs, cfg] = await Promise.all([api('/api/runs', { limit: 60 }), api('/api/config')]);
  const wrap = h('div');
  wrap.append(
    pageHead('Ingest jobs', `Daily refresh at cron "${cfg.refresh_cron}" (${cfg.scheduler_timezone})`, [
      h('button', { class: 'btn primary', onclick: triggerRefresh }, 'Run now'),
    ]),
  );
  wrap.append(
    h(
      'div',
      { class: 'grid grid-stats' },
      statTile('Schedule', cfg.refresh_cron, cfg.scheduler_enabled ? 'enabled' : 'disabled'),
      statTile('Fan-out cap', String(cfg.max_files_per_commit), 'max files per commit for pairing'),
      statTile('Min support', String(cfg.min_pair_support), 'co-changes to persist a pair'),
      statTile('Half-life', `${cfg.recency_half_life_days}d`, 'recency weighting'),
      statTile('Rename detection', `${cfg.rename_similarity}%`, 'similarity threshold'),
      statTile('Blobless above', bytes(cfg.blobless_threshold_kb), 'switch to metadata-only mirror'),
    ),
  );
  wrap.append(h('div', { class: 'section-title' }, 'Run history'));
  wrap.append(card('All runs', runsTable(runs.runs), 'Click a run for the per-repository breakdown'));
  return wrap;
});

on('/jobs/:id', async ({ id }) => {
  const run = await api(`/api/runs/${id}`);
  const wrap = h('div');
  wrap.append(crumbs(['Jobs', '/jobs'], [`Run ${id}`]));
  wrap.append(
    pageHead(
      `Run ${run.id} — ${run.status}`,
      `${run.kind} triggered ${run.trigger} · ${stamp(run.started_at)}${run.duration_s ? ` · ${run.duration_s.toFixed(0)}s` : ''}`,
    ),
  );
  wrap.append(
    h(
      'div',
      { class: 'grid grid-stats' },
      statTile('Repositories', num(run.repos_total), `${run.repos_ok} ok · ${run.repos_failed} failed`),
      statTile('Commits added', num(run.commits_added)),
      statTile('Files added', num(run.files_added)),
      statTile('Pairs written', num(run.pairs_written)),
    ),
  );
  if (run.error) wrap.append(h('div', { class: 'help', style: 'border-left-color:var(--danger)' }, run.error));
  wrap.append(h('div', { class: 'section-title' }, 'Per-repository detail'));
  wrap.append(
    card(
      'Repositories in this run',
      dataTable(
        run.repos,
        [
          { key: 'full_name', label: 'Repository', render: (r) => h('span', { class: 'mono' }, r.full_name) },
          { key: 'status', label: 'Status', render: (r) => h('span', { class: `badge ${r.status === 'success' ? 'ok' : 'danger'}` }, r.status) },
          { key: 'commits_added', label: 'Commits', num: true, render: (r) => num(r.commits_added) },
          { key: 'duration_s', label: 'Duration', num: true, render: (r) => (r.duration_s ? `${r.duration_s.toFixed(1)}s` : '—') },
          { key: 'error', label: 'Error', render: (r) => (r.error ? h('span', { style: 'color:var(--danger);font-size:11.5px', title: r.error }, r.error.slice(0, 90)) : '—') },
        ],
        { initialSort: 'duration_s', onRow: (r) => go(`/repos/${r.repo_id}`) },
      ),
    ),
  );
  return wrap;
});

/* ------------------------------------------------------------- omnibox -- */

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function wireOmnibox() {
  const input = $('#omnibox');
  const panel = $('#omnibox-results');
  let items = [];
  let sel = -1;

  const close = () => {
    panel.hidden = true;
    sel = -1;
  };

  const paint = () => {
    panel.replaceChildren();
    if (!items.length) {
      panel.appendChild(h('div', { class: 'empty', style: 'padding:16px' }, 'No matches'));
      panel.hidden = false;
      return;
    }
    let lastGroup = null;
    items.forEach((it, i) => {
      if (it.group !== lastGroup) {
        panel.appendChild(h('div', { class: 'omnibox-group' }, it.group));
        lastGroup = it.group;
      }
      panel.appendChild(
        h(
          'div',
          {
            class: `omnibox-item${i === sel ? ' sel' : ''}`,
            onclick: () => {
              close();
              input.value = '';
              go(it.href);
            },
          },
          h('span', { class: 'p' }, it.label),
          h('span', { class: 'r' }, it.meta || ''),
          h('span', { class: 'n' }, it.count || ''),
        ),
      );
    });
    panel.hidden = false;
  };

  const search = debounce(async () => {
    const term = input.value.trim();
    if (term.length < 2) return close();
    try {
      const [files, repos] = await Promise.all([
        api('/api/files', { search: term, limit: 12 }),
        api('/api/repos', { search: term, limit: 5 }),
      ]);
      items = [
        ...repos.repos.map((r) => ({
          group: 'Repositories',
          label: r.full_name,
          meta: r.primary_language || '',
          count: `${num(r.commit_count)} commits`,
          href: `/repos/${r.id}`,
        })),
        ...files.files.map((f) => ({
          group: 'Files',
          label: f.path,
          meta: f.repo.split('/')[1],
          count: `${num(f.change_count)}×`,
          href: `/repos/${f.repo_id}/files/${f.path}`,
        })),
      ];
      sel = -1;
      paint();
    } catch (err) {
      console.error(err);
    }
  }, 220);

  input.addEventListener('input', search);
  input.addEventListener('focus', () => input.value.trim().length >= 2 && paint());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') return (input.blur(), close());
    if (!items.length || panel.hidden) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); paint(); }
    else if (e.key === 'Enter' && sel >= 0) { e.preventDefault(); close(); input.value = ''; go(items[sel].href); }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.omnibox')) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== input && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
      e.preventDefault();
      input.focus();
    }
  });
}

/* ---------------------------------------------------------------- boot -- */

function paintFooter(ov) {
  if (!ov) return;
  $('#footer-stats').textContent =
    `${num(ov.repos)} repositories · ${num(ov.commits)} commits · ${num(ov.file_changes)} atomic facts · ${num(ov.file_pairs)} coupling pairs`;
}

function wireTheme() {
  const saved = localStorage.getItem('git-synapse.theme');
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('git-synapse.theme', next);
  });
}

// Intercept in-app links so navigation stays client-side. Anything marked
// data-nav is an internal route; everything else (API docs, GitHub) is left to
// the browser.
document.addEventListener('click', (e) => {
  const link = e.target.closest('a[data-nav]');
  if (!link) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  const href = link.getAttribute('href');
  if (!href) return;
  e.preventDefault();
  go(href);
});

async function boot() {
  wireTheme();
  wireOmnibox();
  try {
    const cat = await api('/api/measures');
    state.measures = cat.measures;
    state.byKey = new Map(cat.measures.map((m) => [m.key, m]));
    if (!state.byKey.has(state.measure)) state.measure = cat.default;
    paintMeasureBar();
  } catch (err) {
    toast(`Could not load measure catalogue: ${err.message}`, true);
  }
  api('/api/overview').then(paintFooter).catch(() => {});
  window.addEventListener('popstate', route);
  route();
}

boot();

/* ========================================================================
   Cross-repository: impact, chains, evidence tiers
   ======================================================================== */

/**
 * Render the evidence tier for a cross-repo edge.
 *
 * This is the most important piece of presentation in the app. Declared and
 * bump-backed edges were measured at AUC 0.88 in sample against real dependency
 * propagation; discovery edges are statistical only and unvalidated, and skew
 * toward merely busy repositories. Showing them identically would be
 * misleading, so tier is always rendered, never inferred from the score.
 */
const tierBadge = (row) => {
  if (row.is_declared)
    return h('span', { class: 'tier tier-declared', title: 'Declared dependency — validated tier (AUC 0.88 in sample, 0.69 held out)' },
      h('i', { class: 'dot' }), 'declared');
  if (row.has_bump_history)
    return h('span', { class: 'tier tier-bump', title: 'Observed manifest bumps — ground truth' },
      h('i', { class: 'dot' }), 'bump-backed');
  return h('span', { class: 'tier tier-discovery', title: 'Statistical only — unvalidated, verify before acting' },
    h('i', { class: 'dot' }), 'discovery');
};

const adoptedAfter = (d) => (d === null || d === undefined ? '—' : `${Number(d).toFixed(1)}d`);

/** Render a chain as clickable nodes joined by weighted arrows. */
function chainNode(repos, hops, repoIds, reverse = false) {
  const wrap = h('div', { class: 'chain' });
  repos.forEach((name, i) => {
    wrap.appendChild(
      h('span', {
        class: `node${i === 0 ? ' first' : ''}`,
        onclick: () => repoIds && repoIds[i] && go(`/repos/${repoIds[i]}`),
      }, name),
    );
    if (i < hops.length) {
      wrap.appendChild(
        h('span', { class: 'hop' },
          h('span', { class: 'arrow' }, reverse ? '←' : '→'),
          h('span', { class: 'w' }, hops[i].toFixed(2))),
      );
    }
  });
  return wrap;
}

on('/insights/impact', async (_args, params) => {
  const repoId = Number(params.repo || 0) || null;
  const direction = params.dir === 'downstream' ? 'downstream' : 'upstream';
  const wrap = await insightsShell('impact', repoId);
  const repos = { repos: wrap.repos };
  const mining = await api('/api/mining/overview');

  wrap.append(
    h('div', { class: 'grid grid-stats' },
      statTile('Impact edges', num(mining.impact_edges),
               `${num(mining.declared_edges)} declared · ${num(mining.bump_edges)} bump-backed`),
      statTile('Manifest bumps', num(mining.dep_bumps), 'observed propagation events'),
      statTile('Declared deps', num(mining.declared_deps), 'internal module edges'),
      statTile('Map', 'open', 'the same edges, drawn', () => go('/insights/graph?mode=repos')),
    ),
  );

  wrap.append(
    explainer('How to read this',
      'Within a repository, coupling means “same commit”. Two repositories never share one, ',
      'so nothing here is inferred from co-change: every edge is read from a ',
      h('strong', {}, 'declared dependency'),
      ' in a manifest, and is stronger still when an actual version ',
      h('strong', {}, 'bump'),
      ' was observed and resolved to the upstream commit it consumed. ',
      'Grouping commits by ticket key or by author session was tried and removed: it scored ',
      'AUC 0.80 while managing 0.63 on which way the arrow points, and a baseline that ',
      'ignored coupling entirely matched it — the measure was ranking “both repositories ',
      'are busy”. Restricting to declared dependencies lifts the base rate from 0.23% to 82% ',
      'before any measure is evaluated.'),
  );

  const dirBtn = (key, label, title) =>
    h('button', { class: `btn${direction === key ? ' primary' : ''}`, title,
      onclick: () => go(`/insights/impact?repo=${repoId || ''}&dir=${key}`) }, label);

  wrap.append(
    h('div', { class: 'toolbar' },
      h('div', { class: 'field' },
        dirBtn('upstream', 'Upstream', 'Repos whose changes precede this one — where a fix may belong'),
        dirBtn('downstream', 'Downstream', 'Repos a change here forces to update')),
    ),
  );

  if (!repoId) {
    wrap.append(card('Pick a repository',
      h('div', { class: 'empty' },
        'Cross-repository impact is per repository: choose one in Scope above to see what '
        + 'it reaches and what reaches it. The org-wide table that used to sit here '
        + 'ranked repositories by co-change across change sets, which is not how '
        + 'this graph is built any more -- it is read from declared dependencies '
        + 'and observed version bumps.')));
    return wrap;
  }

  const [impact, chains, deps] = await Promise.all([
    api(`/api/repos/${repoId}/impact`, { direction, limit: 60 }),
    api(`/api/repos/${repoId}/impact-chains`, { direction, limit: 25, min_score: 0.3 }),
    api(`/api/repos/${repoId}/dependencies`),
  ]);
  const repo = repos.repos.find((r) => r.id === repoId) || { name: repoId };

  wrap.append(h('div', { class: 'section-title' },
    direction === 'upstream'
      ? `Changes in these repositories precede changes in ${repo.name}`
      : `A change in ${repo.name} historically forces these to update`));

  wrap.append(card(
    direction === 'upstream' ? 'Upstream — a fix may belong here' : 'Downstream impact',
    dataTable(impact.edges, [
      { key: 'name', label: 'Repository', render: (r) => h('span', { class: 'mono', style: 'font-weight:550' }, r.name) },
      { key: 'evidence', label: 'Evidence', sortable: false, render: (r) => tierBadge(r) },
      { key: 'bump_count', label: 'Bumps', num: true, title: 'Observed manifest version bumps — ground truth' },
      { key: 'median_adoption_days', label: 'Adopted after', num: true, title: 'Median days from the upstream commit to the bump that took it', render: (r) => adoptedAfter(r.median_adoption_days) },
      { key: 'score', label: 'Score', num: true, render: (r) => h('div', { class: 'confbar', style: 'justify-content:flex-end' }, h('span', {}, fx(r.score, 3)), bar(r.score)) },
      { key: 'primary_language', label: 'Lang', render: (r) => (r.primary_language ? h('span', { class: 'badge muted' }, r.primary_language) : '—') },
    ], { initialSort: 'score', onRow: (r) => go(`/insights/impact/${direction === 'upstream' ? r.source_repo_id : repoId}/${direction === 'upstream' ? repoId : r.target_repo_id}`), empty: 'No cross-repo edges recorded for this repository.' }),
    'Click a row for the full evidence, including the exact commits'));

  if (chains.chains.length) {
    wrap.append(h('div', { class: 'section-title' }, `Transitive chains (${direction})`));
    const body = h('div', { class: 'card-body' });
    body.append(h('div', { class: 'metric-note' },
      'Path confidence is the product of the per-hop scores, so a weak hop can only weaken a chain. Only declared or bump-backed hops are traversed.'));
    for (const c of chains.chains) {
      body.append(h('div', { style: 'display:flex;align-items:center;gap:14px;margin-top:10px;flex-wrap:wrap' },
        chainNode(c.repos, c.hops, c.repo_ids, direction === 'upstream'),
        h('span', { style: 'font-family:var(--mono);font-size:12px;color:var(--accent)' }, `${(c.path_score * 100).toFixed(1)}%`),
        h('span', { style: 'font-size:11px;color:var(--text-faint)' }, `${c.depth} hops`)));
    }
    wrap.append(h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', { class: 'card-title' }, `${chains.chains.length} chains`)), body));
  }

  wrap.append(h('div', { class: 'section-title' }, 'Declared dependencies and observed bumps'));
  wrap.append(h('div', { class: 'grid grid-2' },
    card(`Declared in manifests (${deps.declared.length})`,
      dataTable(deps.declared, [
        { key: 'dep_name', label: 'Module', render: (d) => h('span', { class: 'mono' }, d.dep_name) },
        { key: 'dep_repo', label: 'Tracked repo', render: (d) => (d.dep_repo ? h('a', { href: `/repos/${d.dep_repo_id}`, 'data-nav': true, class: 'mono' }, d.dep_repo) : h('span', { class: 'badge muted' }, 'external')) },
        { key: 'manifest', label: 'Manifest', render: (d) => h('span', { class: 'badge muted' }, d.manifest) },
        { key: 'dep_version', label: 'Version', render: (d) => h('span', { class: 'mono', style: 'font-size:11px' }, (d.dep_version || '').slice(0, 34)) },
      ], { empty: 'No manifest dependencies found.' }),
      'Structural: the candidate set that lifts the base rate ~350×'),
    card(`Observed bumps (${deps.bumps.length})`,
      dataTable(deps.bumps, [
        { key: 'dep_repo', label: 'Upstream', render: (b) => h('a', { href: `/repos/${b.dep_repo_id}`, 'data-nav': true, class: 'mono' }, b.dep_repo) },
        { key: 'bumps', label: 'Bumps', num: true },
        { key: 'median_adoption_days', label: 'Adopted after', num: true, render: (b) => adoptedAfter(b.median_adoption_days) },
        { key: 'last_bump', label: 'Last', render: (b) => when(b.last_bump) },
      ], { initialSort: 'bumps', empty: 'No manifest bumps observed.' }),
      'Ground truth: a pseudo-version names the exact upstream commit')));

  return wrap;
});

/* --------------------------------------------------- repo pair detail -- */

on('/insights/impact/:a/:b', async ({ a, b }) => {
  const [depsA] = await Promise.all([
    api(`/api/repos/${b}/dependencies`),
  ]);
  const [repoA, repoB] = await Promise.all([api(`/api/repos/${a}`), api(`/api/repos/${b}`)]);
  const partners = await api(`/api/repos/${b}/impact`, { direction: 'upstream', limit: 200 });
  const edge = partners.edges.find((e) => String(e.source_repo_id) === String(a));

  const wrap = h('div');
  wrap.append(crumbs(...(await repoTrail(repoB)), [`depends on ${repoA.name}`]));
  wrap.append(pageHead(
    h('span', { class: 'mono' }, `${repoA.name} → ${repoB.name}`),
    'Everything known about this repository relationship',
    [h('a', { class: 'btn', href: `/repos/${a}`, 'data-nav': true }, repoA.name),
     h('a', { class: 'btn', href: `/repos/${b}`, 'data-nav': true }, repoB.name)]));

  if (edge) {
    wrap.append(h('div', { class: 'grid grid-stats' },
      statTile('Impact score', fx(edge.score, 3), 'rank-averaged ensemble'),
      statTile('Evidence', edge.is_declared ? 'declared' : (edge.has_bump_history ? 'bump-backed' : 'discovery'), edge.is_declared ? 'validated tier' : 'see note'),
      statTile('Manifest bumps', num(edge.bump_count), 'observed propagation'),
      statTile('Adopted after', adoptedAfter(edge.median_adoption_days), 'median, upstream commit to bump'),
      statTile('Rank', `#${edge.rank_in_source}`, `within ${repoA.name}`)));
  }

  // Lag profile: two directional curves. If A precedes B, the forward curve
  // sits above the reverse one — that visual gap IS the directional evidence.
  const bump = depsA.bumps.find((x) => String(x.dep_repo_id) === String(a));
  if (bump) {
    wrap.append(h('div', { class: 'help', style: 'margin-top:14px' },
      `${repoB.name} has bumped ${repoA.name} `, h('strong', {}, String(bump.bumps)),
      ` times, with a median propagation lag of `, h('strong', {}, adoptedAfter(bump.median_adoption_days)),
      `. Every one of them is below, with the version it moved to and the upstream `,
      `commit it consumed where that could be resolved.`));
  }

  // The tiles above summarise; this is the thing they summarise. A count and a
  // median say a relationship exists without ever saying when anything happened.
  const evidence = await api(`/api/repos/${b}/bumps/${a}`, { limit: 200 })
    .catch((e) => ({ bumps: [], __failed: String(e.message || e) }));

  wrap.append(h('div', { class: 'section-title' }, 'Every bump, newest first'));
  if (evidence.__failed) {
    wrap.append(card('Could not load the bumps', h('div', { class: 'empty' },
      `This section failed to load, so it is not evidence of absence: ${evidence.__failed}`)));
  } else if (!evidence.bumps.length) {
    wrap.append(card('No recorded bumps', h('div', { class: 'empty' },
      `${repoB.name} declares ${repoA.name}, but no version change has been observed `
      + `in a manifest yet. The edge is real and undated.`)));
  } else {
    wrap.append(card(`${evidence.bumps.length} version changes`,
      dataTable(evidence.bumps, [
        { key: 'bumped_at', label: 'When', render: (r) => when(r.bumped_at) },
        { key: 'dep_version', label: 'Moved to',
          render: (r) => h('span', { class: 'mono' }, r.dep_version) },
        { key: 'manifest', label: 'Manifest',
          render: (r) => h('span', { class: 'mono' }, r.manifest) },
        { key: 'resolution', label: 'Evidence',
          title: 'sha: the manifest named the commit. tag: an exact version matched a '
               + 'tag. floor: a range\u2019s declared lower bound. ceiling: the newest '
               + 'release below an upper bound.',
          render: (r) => (r.resolution
            ? h('span', { class: `badge ${r.resolution === 'sha' ? 'ok' : 'info'}` }, r.resolution)
            : h('span', { class: 'badge muted' }, 'unresolved')) },
        { key: 'adoption_days', label: 'Adopted after', num: true,
          title: 'Days from the upstream commit to the bump that took it',
          render: (r) => (r.adoption_days == null ? '\u2014' : `${r.adoption_days}d`) },
        { key: 'upstream_subject', label: 'Upstream commit',
          render: (r) => (r.upstream_sha
            ? h('span', { title: r.upstream_sha },
                h('span', { class: 'mono' }, String(r.upstream_sha).slice(0, 8)), ' ',
                String(r.upstream_subject || '').slice(0, 60))
            : h('span', { class: 'muted' }, 'not resolved to a commit')) },
      ], { initialSort: 'bumped_at' }),
      'A row with no upstream commit is a bump that happened; only the version it '
      + 'named could not be tied to one.'));
  }
  return wrap;
});

/* Insights is the analysis half of the application: everything derived from
   history, as opposed to the things history is about, which live under
   Repositories. Sections are path segments rather than a ?tab= parameter,
   because cross-repo impact drills further -- to one edge, and to the graph --
   and a query parameter cannot express where you are inside that. */
const INSIGHT_SECTIONS = [
  ['graph', 'Map'],
  ['impact', 'Cross-repo impact'],
  ['risk', 'Risk & bus factor'],
  ['drift', 'Coupling drift'],
  ['modules', 'De-facto modules'],
];

/**
 * The frame every Insights section shares: trail, headline figures, the
 * section tabs and the scope selector. Scoped to a repository it is a rung of
 * the drill-down, so it breadcrumbs under that repository.
 */
async function insightsShell(section, repoId, trail = []) {
  const [ov, repos] = await Promise.all([
    api('/api/mining/overview'),
    api('/api/repos', { limit: 1000, order_by: 'commit_count' }),
  ]);
  const wrapOv = ov;

  const wrap = h('div');
  if (repoId) {
    wrap.append(crumbs(...(await repoTrail({ id: repoId })),
                       ['Insights', `/insights/${section}?repo=${repoId}`], ...trail));
  } else if (trail.length) {
    wrap.append(crumbs(['Insights', `/insights/${section}`], ...trail));
  }
  wrap.append(pageHead('Insights', 'What history says about the code.'));

  const repoSel = h('select', { class: 'input', style: 'min-width:230px' },
    h('option', { value: '' }, 'All repositories'),
    ...repos.repos.map((r) => h('option', { value: r.id, selected: r.id === repoId }, r.name)));
  repoSel.addEventListener('change', () =>
    go(`/insights/${section}${repoSel.value ? `?repo=${repoSel.value}` : ''}`));

  wrap.append(h('div', { class: 'tabs' }, ...INSIGHT_SECTIONS.map(([k, l]) =>
    h('button', { class: `tab${section === k ? ' active' : ''}`,
                  onclick: () => go(`/insights/${k}${repoId ? `?repo=${repoId}` : ''}`) }, l))));
  wrap.append(h('div', { class: 'toolbar' },
    h('div', { class: 'field' }, h('label', {}, 'Scope'), repoSel)));
  wrap.repos = repos.repos;
  wrap.mining = wrapOv;
  return wrap;
}

/* Long explanations earn their place, but not above the thing they explain.
   Collapsed by default: the reader who wants it opens it once. */
const explainer = (summary, ...body) =>
  h('details', { class: 'explainer' }, h('summary', {}, summary),
    h('div', { class: 'help', style: 'margin:9px 0 0' }, ...body));

// The map leads: a picture of the whole corpus is a better first answer than
// a table that has to be configured before it says anything.
on('/insights', (_args, params) => {
  const qs = params.repo ? `?repo=${params.repo}` : '';
  go(`/insights/graph${qs}`);
  return h('div');
});

/* One route for the three sections that are simply a table, so the tab bar can
   build its links from INSIGHT_SECTIONS rather than each name being wired
   twice. Cross-repo impact is registered above, and matches first. */
on('/insights/:section', async ({ section }, params) => {
  if (!['risk', 'drift', 'modules'].includes(section)) {
    return notFound(`No insight called ${section}`);
  }
  const repoId = params.repo ? Number(params.repo) : null;
  const wrap = await insightsShell(section, repoId);
  const ov = wrap.mining;
  if (section === 'risk') {
    const data = await api('/api/risk', { repo_id: repoId || undefined, limit: 120 });
    wrap.append(h('div', { class: 'grid grid-stats' },
      statTile('Files profiled', num(ov.risk_scored), 'scored for churn, coupling and ownership')));
    wrap.append(explainer('How risk is computed',
      h('strong', {}, 'Effective authors'), ' is 1 / HHI, the Herfindahl concentration of each author’s share of a file’s commits. ',
      'Three authors splitting commits 98/1/1 has an effective count near 1, not 3 — only a concentration measure sees that. ',
      'Risk multiplies churn by coupling, lifts by concentration, then shrinks by n/(n+25) so a five-commit README cannot outrank a real hotspot.'));
    wrap.append(card('Highest-risk files',
      dataTable(data.files, [
        { key: 'repo', label: 'Repository', render: (r) => h('span', { class: 'mono', style: 'font-size:11px' }, r.repo) },
        { key: 'path', label: 'File', render: (r) => pathNode(r.path) },
        { key: 'change_count', label: 'Churn', num: true, render: (r) => num(r.change_count) },
        { key: 'partner_count', label: 'Partners', num: true, render: (r) => num(r.partner_count) },
        { key: 'author_count', label: 'Authors', num: true },
        { key: 'effective_authors', label: 'Effective', num: true, title: '1 / HHI — the bus factor that matters', render: (r) => h('span', { style: `color:${(r.effective_authors || 9) < 1.5 ? 'var(--danger)' : 'inherit'}` }, fx(r.effective_authors, 1)) },
        { key: 'ownership_hhi', label: 'HHI', num: true, render: (r) => fx(r.ownership_hhi, 2) },
        { key: 'top_author', label: 'Top author' },
        { key: 'days_since_change', label: 'Idle', num: true, render: (r) => (r.days_since_change === null ? '—' : `${r.days_since_change}d`) },
        { key: 'risk_score', label: 'Risk', num: true, render: (r) => h('div', { class: 'confbar', style: 'justify-content:flex-end' }, h('span', {}, fx(r.risk_score, 3)), bar((r.risk_score || 0) / 2)) },
      ], { initialSort: 'risk_score', onRow: (r) => go(`/repos/${r.repo_id}/files/${r.path}`) })));
  } else if (section === 'drift') {
    const trend = params.trend === 'decaying' ? 'decaying' : 'emerging';
    const data = await api('/api/drift', { trend, repo_id: repoId || undefined, limit: 120 });
    wrap.append(h('div', { class: 'grid grid-stats' },
      statTile('Emerging', num(ov.emerging), 'coupling strengthening lately'),
      statTile('Decaying', num(ov.decaying), 'finished refactors'),
      statTile('Stable', num(ov.stable), 'unchanged over time')));
    wrap.append(h('div', { class: 'toolbar' },
      h('button', { class: `btn${trend === 'emerging' ? ' primary' : ''}`, onclick: () => go(`/insights/drift?trend=emerging&repo=${repoId || ''}`) }, 'Emerging'),
      h('button', { class: `btn${trend === 'decaying' ? ' primary' : ''}`, onclick: () => go(`/insights/drift?trend=decaying&repo=${repoId || ''}`) }, 'Decaying')));
    wrap.append(explainer('What drift means',
      trend === 'emerging'
        ? 'Coupling that has strengthened in the last year relative to the preceding history — relationships forming now.'
        : 'Coupling that has weakened. These are usually completed refactors. Reporting them as current coupling is one of the easier ways to mislead an agent, which is why they are separated out.',
      ' NPMI is recomputed inside each window from that window’s own marginals, not rescaled from the lifetime figure.'));
    wrap.append(card(`${data.pairs.length} ${trend} pairs`,
      dataTable(data.pairs, [
        { key: 'repo', label: 'Repository', render: (r) => h('span', { class: 'mono', style: 'font-size:11px' }, r.repo) },
        { key: 'path_a', label: 'File A', render: (r) => pathNode(r.path_a) },
        { key: 'path_b', label: 'File B', render: (r) => pathNode(r.path_b) },
        { key: 'n_ab_historic', label: 'Then', num: true },
        { key: 'n_ab_recent', label: 'Now', num: true },
        { key: 'npmi_historic', label: 'NPMI then', num: true, render: (r) => fx(r.npmi_historic, 3) },
        { key: 'npmi_recent', label: 'NPMI now', num: true, render: (r) => fx(r.npmi_recent, 3) },
        { key: 'delta', label: 'Δ', num: true, render: (r) => h('span', { style: `color:${r.delta > 0 ? 'var(--ok)' : 'var(--danger)'};font-family:var(--mono)` }, (r.delta > 0 ? '+' : '') + fx(r.delta, 2)) },
      ], { initialSort: 'delta', desc: trend === 'emerging', onRow: (r) => go(`/repos/${r.repo_id}/pairs/${r.file_a_id}/${r.file_b_id}`) })));
  } else {
    if (!repoId) {
      wrap.append(h('div', { class: 'help' }, 'Pick a repository above to see its de-facto modules.'));
      return wrap;
    }
    const data = await api(`/api/repos/${repoId}/modules`, { limit: 40 });
    wrap.append(h('div', { class: 'grid grid-stats' },
      statTile('De-facto modules', num(ov.modules), `${num(ov.clustered_files)} files clustered`),
      statTile('Cross-directory', num(ov.cross_dir_modules), 'modules that cut across folders')));
    wrap.append(explainer('How modules are found',
      'Clusters found by label propagation over the file-coupling graph, weighted by NPMI. ',
      h('strong', {}, 'The interesting output is the disagreement with the directory tree: '),
      'a cluster spanning several folders is a module the codebase grew without declaring. ',
      'Cohesion is the share of a file’s coupling weight that stays inside its own cluster.'));
    wrap.append(h('div', { class: 'grid grid-3' },
      ...data.modules.map((m) => h('div', { class: 'module-card' },
        h('div', { style: 'display:flex;justify-content:space-between;align-items:baseline' },
          h('strong', {}, `${m.cluster_size} files`),
          h('span', { class: 'badge warn' }, `${m.dirs_spanned} directories`)),
        h('div', { class: 'dirs' }, ...(m.directories || []).filter(Boolean).slice(0, 8).map((d) => h('span', { class: 'badge muted' }, d || '<root>'))),
        h('div', { class: 'files' }, ...(m.sample_files || []).slice(0, 5).map((f) => h('div', { title: f }, f))),
        h('div', { style: 'font-size:11px;color:var(--text-faint);margin-top:7px' }, `cohesion ${m.avg_cohesion}`)))));
  }
  return wrap;
});

/* ------------------------------------------------------------ accounts -- */

/** A labelled form control with an optional hint underneath. */
const field = (label, control, hint) =>
  h('div', { class: 'field-block' },
    h('label', { class: 'field-label' }, label),
    control,
    hint ? h('div', { class: 'field-hint' }, hint) : null);

/** A checkbox with its label, returned with the input exposed for reading. */
function toggle(label, checked, hint) {
  const input = h('input', { type: 'checkbox', id: `t-${label.replace(/\W/g, '')}` });
  input.checked = checked;
  const node = h('label', { class: 'toggle' }, input,
    h('span', {}, h('span', { class: 'toggle-label' }, label),
      hint ? h('span', { class: 'toggle-hint' }, hint) : null));
  node.input = input;
  return node;
}

on('/accounts', async () => {
  const data = await api('/api/accounts');
  const rows = data.accounts || [];
  const wrap = h('div');

  wrap.append(pageHead('Accounts',
    'The organisations and users whose repositories get discovered and scanned. Changes take effect on the next discovery run — nothing needs redeploying.'));

  const enabled = rows.filter((r) => r.enabled);
  const discovered = rows.reduce((n, r) => n + Number(r.live_repo_count || 0), 0);
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Accounts', num(rows.length), `${num(enabled.length)} enabled`),
    statTile('Repositories', num(discovered), 'across every account', () => go('/repos')),
    statTile('Never scanned', num(rows.filter((r) => !r.last_discovered_at).length), 'awaiting first discovery'),
    statTile('Failing', num(rows.filter((r) => r.last_discover_error).length), 'last discovery errored')));

  // ---- add form -----------------------------------------------------------
  const login = h('input', { class: 'input', placeholder: 'e.g. kubernetes', autocomplete: 'off', spellcheck: 'false' });
  const kind = h('select', { class: 'input' },
    ...(data.kinds || ['org', 'user']).map((k) => h('option', { value: k }, k === 'org' ? 'Organisation' : 'User account')));
  const only = h('input', { class: 'input', placeholder: 'blank = every repository', autocomplete: 'off' });
  const skip = h('input', { class: 'input', placeholder: 'comma-separated names to ignore', autocomplete: 'off' });
  const tForks = toggle('Include forks', true);
  const tArchived = toggle('Include archived', true);
  const tPrivate = toggle('Include private', true, 'needs a token with repo scope');
  const submit = h('button', { class: 'btn primary' }, 'Add account');

  const add = async () => {
    const value = login.value.trim();
    if (!value) { toast('Enter a login first', true); login.focus(); return; }
    submit.disabled = true;
    try {
      await apiSend('POST', '/api/accounts', {
        login: value,
        kind: kind.value,
        only_repos: only.value.split(',').map((x) => x.trim()).filter(Boolean),
        skip_repos: skip.value.split(',').map((x) => x.trim()).filter(Boolean),
        include_forks: tForks.input.checked,
        include_archived: tArchived.input.checked,
        include_private: tPrivate.input.checked,
      });
      toast(`${value} added — run a discovery to pick up its repositories`);
      route();
    } catch (err) {
      toast(String(err.message || err), true);
    } finally {
      submit.disabled = false;
    }
  };
  submit.onclick = add;
  login.onkeydown = (e) => { if (e.key === 'Enter') add(); };

  wrap.append(card('Add an account',
    h('div', { class: 'form' },
      h('div', { class: 'form-row two' },
        field('Organisation or user', login, 'The login exactly as GitHub spells it.'),
        field('Kind', kind, 'Organisations list via /orgs, users via /users.')),
      h('div', { class: 'form-row two' },
        field('Only these repositories', only, 'An allowlist. When set, it overrides every filter below.'),
        field('Skip these repositories', skip, 'Applied after the include filters.')),
      h('div', { class: 'form-row toggles' }, tForks, tArchived, tPrivate),
      h('div', { class: 'form-actions' }, submit)),
    'Discovery reads this list, so onboarding is a write rather than a redeploy'));

  // ---- existing accounts --------------------------------------------------
  const setEnabled = async (row, value) => {
    try {
      await apiSend('PATCH', `/api/accounts/${row.id}`, { enabled: value });
      toast(`${row.login} ${value ? 'enabled' : 'disabled'}`);
      route();
    } catch (err) { toast(String(err.message || err), true); }
  };

  const remove = async (row) => {
    if (!window.confirm(
      `Stop scanning ${row.login}?\n\nIts ${num(row.live_repo_count)} repositories and everything mined from them are kept — they simply stop being refreshed.`
    )) return;
    try {
      await apiSend('DELETE', `/api/accounts/${row.id}`);
      toast(`${row.login} removed`);
      route();
    } catch (err) { toast(String(err.message || err), true); }
  };

  const filterCell = (r) => {
    if (r.only_repos && r.only_repos.length) {
      return h('span', { class: 'badge info', title: r.only_repos.join(', ') }, `only ${r.only_repos.length}`);
    }
    const off = [
      !r.include_forks ? 'no forks' : null,
      !r.include_archived ? 'no archived' : null,
      !r.include_private ? 'no private' : null,
      r.skip_repos && r.skip_repos.length ? `skip ${r.skip_repos.length}` : null,
    ].filter(Boolean);
    if (!off.length) return h('span', { class: 'muted-cell' }, 'everything');
    return h('span', {}, ...off.map((t) => h('span', { class: 'badge muted' }, t)));
  };

  wrap.append(card(`${rows.length} configured`,
    dataTable(rows, [
      { key: 'login', label: 'Account', render: (r) => h('span', {},
          h('strong', {}, r.login),
          h('span', { class: 'badge muted', style: 'margin-left:6px' }, r.kind)) },
      { key: 'live_repo_count', label: 'Repos', num: true,
        title: 'Repositories currently attributed to this account. Click to see them.',
        render: (r) => (Number(r.live_repo_count)
          ? h('a', { class: 'mono', href: `/accounts/${r.id}`, 'data-nav': true,
                     onclick: (e) => e.stopPropagation() }, num(r.live_repo_count))
          : h('span', { class: 'muted-cell' }, '0')) },
      { key: 'filters', label: 'Filters', sortable: false, render: filterCell },
      { key: 'last_discovered_at', label: 'Last discovery', render: (r) => (
          r.last_discover_error
            ? h('span', { class: 'badge danger', title: r.last_discover_error }, 'failed')
            : when(r.last_discovered_at) || h('span', { class: 'muted-cell' }, 'never')) },
      { key: 'enabled', label: 'Scanning', render: (r) => h('button', {
          class: `badge ${r.enabled ? 'ok' : 'muted'} clickable`,
          onclick: (e) => { e.stopPropagation(); setEnabled(r, !r.enabled); },
          title: r.enabled ? 'Click to pause scanning' : 'Click to resume scanning',
        }, r.enabled ? 'on' : 'paused') },
      { key: 'actions', label: '', sortable: false, render: (r) => h('button', {
          class: 'btn sm danger-btn',
          onclick: (e) => { e.stopPropagation(); remove(r); },
        }, 'Remove') },
    ], { initialSort: 'live_repo_count',
         onRow: (r) => (Number(r.live_repo_count) ? go(`/accounts/${r.id}`) : null),
         empty: 'No accounts yet. Add one above and run a discovery.' }),
    'An account owns repositories: open one to see just those. '
    + 'Removing an account keeps them and everything mined from them.'));

  return wrap;
});

on('/feedback', async (_args, params) => {
  const status = params.status || 'open';
  const data = await api('/api/feedback', { status, limit: 300 });
  const s = data.summary || {};

  const wrap = h('div');
  wrap.append(pageHead('Feedback',
    'Defects in Git Synapse itself, reported by the sessions that use it.'));

  wrap.append(h('div', { class: 'help' },
    h('strong', {}, 'This log feeds nothing. '),
    'No measure, score or ranking reads from it. That boundary is deliberate: letting sessions write into the coupling data would close a confirmation loop — Git Synapse suggests a pair, the agent edits both files, the commit strengthens the pair — and the statistic would drift from measuring the codebase to measuring its own past advice. ',
    'Agent-authored commits are already ~16% of the last week of history, so that is a live risk rather than a hypothetical one.'));

  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Open', num(s.open), `${num(s.open_high)} high severity`, () => go('/feedback?status=open')),
    statTile('Fixed', num(s.fixed), 'resolved', () => go('/feedback?status=fixed')),
    statTile('Total reports', num(s.total), `${num(s.total_hits)} hits`, () => go('/feedback?status=all')),
    statTile('Seen today', num(s.seen_today), 'last 24h')));

  wrap.append(h('div', { class: 'toolbar' },
    ...['open', 'investigating', 'fixed', 'wontfix', 'all'].map((st) =>
      h('button', { class: `chip${status === st ? ' active' : ''}`, onclick: () => go(`/feedback?status=${st}`) }, st))));

  const sevBadge = (sev) => h('span', {
    class: `badge ${sev === 'high' ? 'danger' : sev === 'medium' ? 'warn' : 'muted'}`,
  }, sev);

  wrap.append(card(`${data.reports.length} reports`,
    dataTable(data.reports, [
      { key: 'occurrences', label: 'Hits', num: true, title: 'How many sessions hit this. The priority signal.' },
      { key: 'severity', label: 'Sev', render: (r) => sevBadge(r.severity) },
      { key: 'kind', label: 'Kind', render: (r) => h('span', { class: 'badge muted' }, r.kind) },
      { key: 'tool', label: 'Tool', mono: true },
      { key: 'repo', label: 'Where', render: (r) => h('span', { class: 'mono', style: 'font-size:11px' }, [r.repo, r.path].filter(Boolean).join('/') || '—') },
      { key: 'detail', label: 'Detail', render: (r) => h('span', { style: 'display:block;max-width:420px' }, r.detail || '—') },
      { key: 'expected', label: 'Expected vs observed', render: (r) => h('div', { style: 'font-size:11px;max-width:300px' },
          r.expected ? h('div', { style: 'color:var(--ok)' }, `want: ${r.expected}`) : null,
          r.observed ? h('div', { style: 'color:var(--danger)' }, `got:  ${r.observed}`) : null) },
      { key: 'status', label: 'Status', render: (r) => h('span', { class: `badge ${r.status === 'fixed' ? 'ok' : r.status === 'open' ? 'info' : 'muted'}` }, r.status) },
      { key: 'last_seen_at', label: 'Last seen', render: (r) => when(r.last_seen_at) },
    ], { initialSort: 'occurrences', empty: status === 'open' ? 'Nothing open. Sessions have not reported an unresolved defect.' : 'No reports.' }),
    'Reported via the report_gap MCP tool'));

  const resolved = data.reports.filter((r) => r.resolution);
  if (resolved.length) {
    wrap.append(h('div', { class: 'section-title' }, 'Resolutions'));
    const body = h('div', { class: 'card-body' });
    for (const r of resolved) {
      body.append(h('div', { style: 'margin-bottom:9px;font-size:12.5px' },
        h('span', { class: 'badge muted' }, `#${r.id}`), ' ',
        h('span', { style: 'color:var(--text-dim)' }, r.resolution)));
    }
    wrap.append(h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', { class: 'card-title' }, 'What was done')), body));
  }
  return wrap;
});

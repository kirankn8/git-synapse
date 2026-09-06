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

/** A deliberate do-nothing, so an empty arrow never reads as an oversight. */
const noop = () => undefined;

export const h = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Number.POSITIVE_INFINITY)) {
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

/** `num` without the decimal, for a label that has to fit a narrow column. */
export const numTight = (n) => {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (Math.abs(v) >= 1e9) return Math.round(v / 1e9) + 'B';
  if (Math.abs(v) >= 1e6) return Math.round(v / 1e6) + 'M';
  if (Math.abs(v) >= 1e3) return Math.round(v / 1e3) + 'k';
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
  if (res.status === 401) throw signedOut();
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
  if (res.status === 401) throw signedOut();
  if (!res.ok) throw await failure(res);
  return res.status === 204 ? null : res.json();
}

/* A session can end between two clicks -- it expired, an administrator
   deactivated the account, someone signed out in another tab. Marking the
   error lets the router put up the sign-in form instead of "could not load
   this view", which is true but useless. */
function signedOut() {
  const err = new Error('Your session has ended. Sign in to continue.');
  err.signedOut = true;
  return err;
}

/* -------------------------------------------------------------- state -- */

/* Reading localStorage throws outright in a browser told to block site data,
   and this is read at module scope -- so the whole application failed to load,
   blank, with the reason only in the console. A remembered measure and a
   remembered theme are conveniences; neither is worth the page. */
const store = {
  get(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* Nothing to do and nothing to report: the setting simply will not
         survive the tab, which is what the browser was asked to enforce. */
    }
  },
};

export const state = {
  //: The signed-in user, and what this deployment asks of a visitor.
  me: null,
  needsSetup: false,
  authRequired: false,
  measure: store.get('git-synapse.measure') || 'npmi',
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
  store.set('git-synapse.measure', key);
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

  const host = $('#measure-select');
  if (!host.firstChild) {
    // Thirty-one measures grouped into families: worth searching, same as the
    // repository list.
    host.appendChild(searchSelect(
      state.measures.map((m) => ({ value: m.key, label: m.label, group: m.family })),
      { selected: state.measure, placeholder: 'Search measures\u2026',
        onChange: (key) => key && setMeasure(key) },
    ));
  }
  // The chips carry the eight quick measures; the picker shows whichever is
  // active so the two never disagree about what is selected.
  host.firstChild.value = state.measure;

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
  // Overview lost its ranked pairs table when it became an operator page, and
  // the bar stayed behind ordering nothing.
  if (path === '/') return false;
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

  // The door, if this deployment has one. Rendered in place of any view, so a
  // deep link is remembered: signing in continues to where you were going.
  if (state.needsSetup || (state.authRequired && !state.me)) {
    document.body.classList.add('gated');
    settled();
    progress(false);
    view.replaceChildren(gate({ setup: state.needsSetup, minted: state.setupTokenMinted }));
    return;
  }
  document.body.classList.remove('gated');

  for (const { rx, keys, handler } of routes) {
    const match = path.match(rx);
    if (!match) continue;
    const args = Object.fromEntries(keys.map((k, i) => [k, decodeURIComponent(match[i + 1])]));
    // A view that arrives within a blink should not flash a spinner at the
    // reader; one that takes longer should say something. The bar waits.
    const slow = setTimeout(() => token === navToken && progress(true), 140);
    try {
      const node = await handler(args, params);
      clearTimeout(slow);
      if (token !== navToken) return; // a newer navigation won
      progress(false);
      settled();
      view.replaceChildren(node);
      view.classList.remove('view-enter');
      void view.offsetWidth;            // restart the animation on every view
      view.classList.add('view-enter');
      window.scrollTo(0, 0);
    } catch (err) {
      clearTimeout(slow);
      progress(false);
      settled();
      if (token !== navToken) return;
      if (err.signedOut) {
        // Expired, revoked, or signed out in another tab. Re-read the state and
        // show the door rather than "could not load this view", which is true
        // and useless.
        await loadMe();
        paintProfile();
        return route();
      }
      console.error(err);
      view.replaceChildren(
        h('div', { class: 'empty' }, h('strong', {}, 'Could not load this view'), String(err.message || err)),
      );
      toast(String(err.message || err), true);
    }
    return;
  }

  progress(false);
  settled();
  view.replaceChildren(notFound(`No view for ${path}`));
}

/* The splash covers the first paint, when the script has not run and the first
   query has not returned. It is removed by the first view that renders --
   success or failure -- so a broken deployment shows its error rather than an
   animation that never ends. */
function settled() {
  const splash = document.getElementById('splash');
  if (!splash || splash.classList.contains('gone')) return;
  splash.classList.add('gone');
  // Removed rather than left hidden: it is fixed and full-screen, and a stray
  // overlay that stops swallowing clicks only because of a class is fragile.
  setTimeout(() => splash.remove(), 400);
}

/** A thin bar for a navigation slow enough to notice. */
function progress(on) {
  let el = document.getElementById('progress');
  if (!el) {
    el = h('div', { id: 'progress' });
    document.body.appendChild(el);
  }
  if (on) {
    el.classList.add('busy');
    el.style.width = '18%';
    requestAnimationFrame(() => { el.style.width = '72%'; });
  } else {
    el.style.width = '100%';
    el.classList.remove('busy');
    setTimeout(() => { el.style.width = '0'; }, 260);
  }
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

async function repoTrail(input) {
  const trail = [];
  // A caller with only an id gets the rest looked up, so every page can build
  // the same trail without carrying repository fields it does not otherwise need.
  const repo = input && input.id && !input.name
    ? { ...((await repoById(input.id)) || {}), ...input }
    : input;
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
      trail.push(['Sources', '/sources'], [account.login, `/sources/${account.id}`]);
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
    { class: `stat${onclick ? ' is-link' : ''}`, onclick: onclick || noop,
      role: onclick ? 'button' : null, tabindex: onclick ? '0' : null },
    h('div', { class: 'stat-label' }, label),
    h('div', { class: 'stat-value' }, value),
    meta ? h('div', { class: 'stat-meta' }, meta) : null,
  );

/**
 * A titled panel.
 *
 * `to` makes the whole card a way in: a summary always has a fuller view
 * behind it, and a reader who wants it should not have to find a separate
 * link. Clicking anything inside that already navigates -- a table row, a bar
 * -- wins, so the card's own target is the fallback rather than an override.
 */
export const card = (title, body, sub, headExtra, to) =>
  h(
    'div',
    {
      class: `card${to ? ' is-link' : ''}`,
      role: to ? 'link' : null,
      tabindex: to ? '0' : null,
      onclick: to
        ? (e) => {
            // The card is itself `.is-link`, so the match has to be something
            // strictly inside it.
            const inner = e.target.closest('a,button,.is-link');
            if (!inner || inner === e.currentTarget) go(to);
          }
        : null,
      onkeydown: to ? (e) => { if (e.key === 'Enter') go(to); } : null,
    },
    h(
      'div',
      { class: 'card-head' },
      h('div', {}, h('h3', { class: 'card-title' }, title), sub ? h('div', { class: 'card-sub' }, sub) : null),
      headExtra || (to ? h('span', { class: 'card-go' }, '\u2192') : null),
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
        // Only rows that actually go somewhere are marked as links. Every row
        // used to carry a handler and a pointer cursor whether or not one was
        // given, so a table that led nowhere looked exactly like one that did.
        const tr = h('tr', opts.onRow ? {
          class: 'is-link',
          onclick: (ev) => {
            if (ev.target.closest('a,button')) return;
            opts.onRow(row);
          },
        } : {});
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

/* Overview answers one question: is this deployment healthy, and is anything
   using it? Everything that was a worse copy of another tab is gone -- the
   repository list belongs to Repositories, the mining figures to Insights, the
   run history to Jobs, and the shortcut buttons duplicated the nav one line
   above. What is left is corpus scale, ingest health, and activity. */
/* Every distribution defined once: how to shape the rows, how to scale them,
   what the chart is saying, and where a bucket leads when it maps to something
   the reader can open. Overview draws these small; /insights/shape/:metric
   draws the same definition at full size with a table of every bucket. Two
   copies of this would drift, and the small one would start lying. */
const SHAPE = {
  commits_by_year: {
    title: 'Commits per year',
    unit: 'commits',
    axis: 'Year',
    rows: (d) => (d.commits_by_year || []).map((r, i) => ({
      label: String(r.year), value: Number(r.n), series: (i % 6) + 1,
    })),
    says: (rows) => (rows.length
      ? `${rows.length} years of history; ${num(rows[rows.length - 1].value)} commits in ${rows[rows.length - 1].label}`
      : 'No dated commits'),
    means: 'How far back the corpus reaches, and whether it is still moving. '
      + 'A coupling drawn from history that stopped years ago describes code as it was, not as it is.',
    more: '/repos?order_by=commit_count',
  },
  pair_support: {
    title: 'Evidence behind a coupling',
    unit: 'pairs', scale: 'log',
    axis: 'Times the pair changed together',
    rows: (d) => (d.pair_support || []).map((r, i) => ({
      label: Number(r.support) >= 10 ? '10+' : String(r.support),
      value: Number(r.n), series: (i % 6) + 1,
    })),
    says: (rows) => {
      const total = rows.reduce((n, r) => n + r.value, 0) || 1;
      const thin = (rows.find((r) => r.label === '2') || { value: 0 }).value;
      return `${pct(thin / total)} of pairs rest on just two co-changes`;
    },
    means: 'A score computed from two shared commits is arithmetic, not evidence. '
      + 'This is why a minimum support exists, and why every measure is reported beside its support count.',
    more: '/measures',
  },
  repo_sizes: {
    title: 'Repositories by size',
    unit: 'repositories', kind: 'hbar',
    axis: 'Commits in the repository',
    rows: (d) => (d.repo_sizes || []).map((r) => ({
      label: r.bucket, value: Number(r.n), commits: Number(r.commits),
      to: '/repos?order_by=commit_count',
    })),
    says: (rows) => {
      const all = rows.reduce((n, r) => n + r.commits, 0) || 1;
      const big = rows[rows.length - 1];
      return big ? `${big.value} repositories hold ${pct(big.commits / all)} of all commits`
                 : 'No repositories ingested';
    },
    means: 'A few very large repositories can dominate any corpus-wide ranking, '
      + 'which is why most views are scoped to one repository by default.',
    more: '/repos?order_by=commit_count',
  },
  languages: {
    title: 'Languages',
    unit: 'repositories', kind: 'hbar', limit: 5,
    axis: 'Primary language',
    rows: (d) => (d.languages || []).map((l) => ({
      label: l.language, value: Number(l.n),
      to: `/repos?lang=${encodeURIComponent(l.language)}`,
    })),
    says: (rows) => `${rows.length} languages across ${num(rows.reduce((n, r) => n + r.value, 0))} repositories`,
    means: 'Coupling is computed from commit co-occurrence, so it is language-agnostic. '
      + 'The mix matters for manifest parsing, which is per-ecosystem.',
    more: '/repos',
  },
  commit_width: {
    title: 'Files per commit',
    unit: 'commits', scale: 'log',
    axis: 'Files touched',
    rows: (d) => (d.commit_width || []).map((r, i) => ({
      label: Number(r.files) >= 12 ? '12+' : String(r.files),
      value: Number(r.n), series: (i % 6) + 1,
    })),
    says: (rows) => {
      const total = rows.reduce((n, r) => n + r.value, 0) || 1;
      const w = (rows.find((r) => r.label === '12+') || { value: 0 }).value;
      return `${pct(w / total)} of commits touch 12 files or more`;
    },
    means: 'A commit touching n files pairs every one of them with every other, '
      + 'so a sweeping change contributes n\u00b2 pairs of pure noise. That is what the fan-out cap exists to exclude.',
    more: '/jobs?tab=settings',
  },
  authors_per_file: {
    title: 'Authors per file',
    unit: 'files', scale: 'log',
    axis: 'Distinct authors',
    rows: (d) => (d.authors_per_file || []).map((r, i) => ({
      label: Number(r.authors) >= 8 ? '8+' : String(r.authors),
      value: Number(r.n), series: (i % 6) + 1,
    })),
    says: (rows) => {
      const total = rows.reduce((n, r) => n + r.value, 0) || 1;
      const one = (rows.find((r) => r.label === '1') || { value: 0 }).value;
      return `${pct(one / total)} of files have been touched by one author only`;
    },
    means: 'The bus-factor shape of the corpus before any scoring. '
      + 'Risk sharpens this with ownership concentration, which sees 98/1/1 as close to one author, not three.',
    more: '/insights/risk',
  },
  adoption_days: {
    title: 'How fast a bump is adopted',
    unit: 'bumps', kind: 'hbar',
    axis: 'Time from the upstream commit to the bump',
    rows: (d) => {
      const B = ['<2mo', '2-4mo', '4-6mo', '6-8mo', '8-10mo', '10-12mo', '1yr+'];
      return (d.adoption_days || []).map((r) => ({
        label: B[Math.max(0, Number(r.bucket) - 1)] || '1yr+', value: Number(r.n),
        to: '/insights/impact',
      }));
    },
    says: (rows) => {
      const total = rows.reduce((n, r) => n + r.value, 0) || 1;
      const fast = (rows.find((r) => r.label === '<2mo') || { value: 0 }).value;
      return total > 1 ? `${pct(fast / total)} of bumps landed within two months`
                       : 'No resolved bumps yet';
    },
    means: 'Ground truth, not inference: each of these is a manifest version change '
      + 'resolved to the upstream commit it consumed. The spread is how long a fix actually takes to travel.',
    more: '/insights/impact',
  },
  repo_recency: {
    title: 'When repositories last changed',
    unit: 'repositories', kind: 'hbar',
    axis: 'Last commit',
    rows: (d) => (d.repo_recency || []).map((r) => ({
      label: r.bucket, value: Number(r.n), to: '/repos?order_by=last_commit_at',
    })),
    says: (rows) => {
      const total = rows.reduce((n, r) => n + r.value, 0) || 1;
      const cold = rows.filter((r) => r.label === 'over a year' || r.label === 'never')
                       .reduce((n, r) => n + r.value, 0);
      return `${cold} of ${num(total)} untouched in a year`;
    },
    means: 'A dormant repository still contributes history, and that history no longer '
      + 'describes code anyone is changing. Worth knowing before reading a corpus-wide ranking.',
    more: '/repos?order_by=last_commit_at',
  },
};

/** One distribution, drawn at whatever size the caller has room for. */
/**
 * `values` says whether there is room for a number above each column, which is
 * a question about width and therefore about the caller: a card gives 32px a
 * column, a half-width panel 31 to 55, and the full view 66. Guessing it from
 * the row count alone drew 21 numbers into 31px and clipped every one.
 */
/**
 * One distribution, drawn.
 *
 * `drill` decides who owns a click on a bar. Inside a card that opens the full
 * distribution the answer is the card: a preview is a picture of a whole, and
 * clicking part of it should show the whole rather than jump sideways to a
 * filtered repository list. The languages card sat on Overview with its arrow
 * going to the distribution and its bars going to `/repos?lang=…`, so the same
 * card had two destinations depending on where in it you clicked.
 *
 * At `/insights/shape/:metric` the whole is already on screen, so there a bar
 * drills into what it counts.
 */
function shapeChart(spec, rows, { small = true, values, drill = false } = {}) {
  // Five horizontal rows is what a card holds: each is about 21px and the body
  // is 124px less its padding. Six were drawn and the last was sliced in half
  // by the card's own clipping, which is worse than aggregating it away. The
  // full view has room for every one.
  const cap = spec.kind === 'hbar' ? 4 : spec.limit;
  const shown = small && cap && rows.length > cap
    ? [...rows.slice(0, cap), {
        label: `${rows.length - cap} more`,
        value: rows.slice(cap).reduce((n, r) => n + r.value, 0),
        to: rows[0] && rows[0].to,
      }]
    : rows;
  const bars = drill ? shown : shown.map(({ to, ...r }) => r);
  return spec.kind === 'hbar'
    ? hbars(bars, { suffix: '' })
    : barChart(bars, { label: spec.unit, scale: spec.scale || 'linear',
                        height: small ? 66 : 190,
                        values: values === undefined ? (small ? null : true) : values });
}

on('/', async () => {
  const [ov, runs, activity, timeline, shape, mining] = await Promise.all([
    api('/api/overview'),
    api('/api/runs', { limit: 12 }),
    api('/api/calls/summary', { hours: 24 }).catch(() => ({ summary: {}, by_name: [] })),
    api('/api/calls/timeline', { hours: 24 }).catch(() => ({ buckets: [] })),
    api('/api/overview/shape').catch(() => ({})),
    api('/api/mining/overview').catch(() => ({})),
  ]);
  const wrap = h('div');
  const c = activity.summary || {};

  wrap.append(pageHead('Overview',
    'The state of the corpus and of the deployment serving it.'));

  // Every figure opens the thing it counts, or the distribution behind it.
  // A number with no way in is a dead end, and these are the first eight a
  // reader sees.
  wrap.append(h('div', { class: 'section-title' }, 'Data'));
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Repositories', num(ov.repos), `${num(ov.repos_ready)} ready · ${num(ov.repos_failed)} failed`,
             () => go('/repos')),
    statTile('Commits', num(ov.commits), 'analysed',
             () => go('/insights/shape/commits_by_year')),
    statTile('Files tracked', num(ov.files), `${num(ov.directories)} directories`,
             () => go('/repos?order_by=file_count')),
    statTile('Coupling pairs', num(ov.file_pairs), `${num(ov.dir_pairs)} directory pairs`,
             () => go('/insights/shape/pair_support')),
    statTile('Contributors', num(ov.authors), 'distinct authors',
             () => go('/insights/shape/authors_per_file')),
    statTile('Mirror size', bytes(ov.mirror_kb), 'bare git mirrors',
             () => go('/repos?order_by=commit_count')),
    statTile('History span', ov.first_commit_at ? `${dateStr(ov.first_commit_at).slice(0, 4)}\u2192` : '\u2014',
             `to ${dateStr(ov.last_commit_at)}`,
             () => go('/insights/shape/repo_recency')),
    statTile('File changes', num(ov.file_changes), 'atomic (commit \u00d7 file) facts',
             () => go('/insights/shape/commit_width'))));

  // ---- ingest health ------------------------------------------------------
  const rows = runs.runs || [];
  const last = rows[0];
  const failing = rows.filter((r) => r.status === 'failed').length;
  const stale = last && last.started_at
    ? (Date.now() - new Date(last.started_at).getTime()) / 3600000 : null;

  wrap.append(h('div', { class: 'section-title' }, 'Ingest'));
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Last run', last ? when(last.started_at) : 'never',
             last ? `${last.status} in ${Math.round(last.duration_s || 0)}s` : 'no runs recorded',
             () => go('/jobs')),
    statTile('Failed lately', num(failing), `of the last ${rows.length} runs`, () => go('/jobs')),
    statTile('Repositories failing', num(ov.repos_failed), 'last ingest errored', () => go('/repos')),
    statTile('Freshness', stale === null ? '\u2014' : `${stale.toFixed(1)}h`,
             'since the last run started', () => go('/jobs?tab=settings'))));

  if (last && last.status === 'failed') {
    wrap.append(h('div', { class: 'help', style: 'border-left-color:var(--danger)' },
      h('strong', {}, 'The last ingest failed. '), last.error || 'See Jobs for the detail.'));
  }

  // ---- who is calling -----------------------------------------------------
  // ---- the shape behind the headline numbers ------------------------------
  // Eight questions an operator has before trusting anything derived from this.
  // Each card states its own answer and opens the full chart, where every
  // bucket is listed with its count rather than squeezed into a card.
  wrap.append(h('div', { class: 'section-title' }, 'Shape of the data',
    h('a', { class: 'section-more', href: '/insights/shape', 'data-nav': true }, 'all eight in full \u2192')));

  wrap.append(h('div', { class: 'grid grid-charts' },
    ...Object.entries(SHAPE).map(([key, spec]) => {
      const rows = spec.rows(shape);
      return card(spec.title,
        h('div', { class: 'card-body chart-body' }, shapeChart(spec, rows)),
        rows.length ? spec.says(rows) : 'No data yet',
        null, `/insights/shape/${key}`);
    })));

  wrap.append(card('What history has produced',
    h('div', { class: 'card-body' },
      h('div', { class: 'grid grid-4' },
        miniStat('Coupled pairs', num(ov.file_pairs), 'file pairs that change together'),
        miniStat('Impact edges', num(mining.impact_edges || 0), 'repository to repository'),
        miniStat('De-facto modules', num(mining.modules || 0), `${num(mining.cross_dir_modules || 0)} cross-directory`),
        miniStat('Manifest bumps', num(mining.dep_bumps || 0), 'observed version changes'))),
    'The derived layers, in full under Insights', null, '/insights'));

  wrap.append(h('div', { class: 'section-title' }, 'Activity, last 24 hours'));

  const buckets = (timeline.buckets || []).map((b) => ({
    label: new Date(b.hour).toISOString().slice(11, 16),
    value: Number(b.calls) || 0,
    alert: Number(b.errors) > 0,
  }));
  wrap.append(card('Calls per hour',
    h('div', { class: 'card-body' }, barChart(buckets, { label: 'calls' })),
    'Bars turn red in an hour that contained a failed call', null, '/activity'));
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('MCP calls', num(c.mcp_calls || 0), 'tools invoked by agents', () => go('/activity?surface=mcp')),
    statTile('HTTP calls', num(c.http_calls || 0), 'API requests', () => go('/activity?surface=http')),
    statTile('Errors', num(c.errors || 0), `of ${num(c.calls || 0)} calls`, () => go('/activity?status=error')),
    statTile('Median', c.p50_ms == null ? '\u2014' : `${c.p50_ms}ms`,
             c.p95_ms == null ? 'no calls recorded' : `p95 ${c.p95_ms}ms`, () => go('/activity'))));

  // Capped: the full ranking runs to fifty rows and 1800 pixels, which is a
  // page of its own, not a summary. Activity holds it.
  const ranked = (activity.by_name || []).slice()
    .sort((a, b) => (b.calls || 0) - (a.calls || 0)).slice(0, 8);
  wrap.append(card('Most-called', dataTable(ranked, [
    { key: 'surface', label: 'Surface', render: (r) => h('span', { class: `badge ${r.surface === 'mcp' ? 'ok' : 'muted'}` }, r.surface) },
    { key: 'name', label: 'Tool or route', render: (r) => h('span', { class: 'mono' }, r.name) },
    { key: 'calls', label: 'Calls', num: true, render: (r) => num(r.calls) },
    { key: 'errors', label: 'Errors', num: true, render: (r) => h('span', { style: `color:${r.errors ? 'var(--danger)' : 'inherit'}` }, num(r.errors)) },
    { key: 'avg_ms', label: 'Avg', num: true, render: (r) => `${fx(r.avg_ms, 0)}ms` },
    { key: 'avg_rows', label: 'Avg rows', num: true, render: (r) => (r.avg_rows == null ? '\u2014' : fx(r.avg_rows, 0)) },
    { key: 'last_call', label: 'Last', render: (r) => (r.last_call ? when(r.last_call)
        : h('span', { class: 'badge muted' }, 'never called')) },
  ], {
    initialSort: 'calls',
    onRow: (r) => (r.calls ? go(`/activity?surface=${r.surface}&name=${encodeURIComponent(r.name)}`) : null),
    empty: 'Nothing has called this deployment in the last 24 hours.',
  }), h('span', {}, `Top 8 of ${(activity.by_name || []).length}. `,
        h('a', { href: '/activity', 'data-nav': true }, 'All activity'),
        `, including the ${activity.mcp_tools || 0} MCP tools and which have never been called.`)));

  wrap.append(h('div', { class: 'help' },
    'The corpus itself is under ', h('a', { href: '/repos', 'data-nav': true }, 'Repositories'),
    '; what history says about it is under ', h('a', { href: '/insights', 'data-nav': true }, 'Insights'), '.'));
  return wrap;
});

/** Columns of the repository table a link may ask to sort by. */
const SORTABLE = new Set(['commit_count', 'file_count', 'pair_count', 'author_count',
                          'last_commit_at', 'primary_language', 'name']);

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
    wrap.append(crumbs(['Sources', '/sources'], [account.login]));
    wrap.append(pageHead(`${account.login} repositories`,
      `${repos.count} of the ${account.kind === 'org' ? 'organisation' : 'user'}'s repositories are scanned`));
  } else {
    wrap.append(pageHead('Repositories', `${repos.count} repositories in the corpus`));
  }

  // The filter runs against `full_name`, which is `owner/name` -- so typing a
  // source name finds everything under it, and the placeholder says so rather
  // than leaving the reader to discover it.
  const search = h('input', {
    class: 'input',
    type: 'search',
    placeholder: 'Search repositories or sources…',
    value: params.q || '',
    style: 'width:260px',
  });
  search.addEventListener('input', debounce(() => applyFilters(), 280));

  const langSel = searchSelect(
    langs.languages.map((l) => ({ value: l.language, label: `${l.language} (${l.n})` })),
    { selected: params.lang || '', emptyLabel: 'All languages',
      placeholder: 'Search languages\u2026', onChange: () => applyFilters() },
  );
  const statusSel = h(
    'select',
    { class: 'input' },
    ...['', 'ready', 'failed', 'ingesting', 'pending'].map((s) =>
      h('option', { value: s, selected: params.status === s }, s || 'Any status'),
    ),
  );
  statusSel.addEventListener('change', () => applyFilters());

  const applyFilters = () => {
    const p = new URLSearchParams();
    if (search.value) p.set('q', search.value);
    if (langSel.value) p.set('lang', langSel.value);
    if (statusSel.value) p.set('status', statusSel.value);
    const base = accountId ? `/sources/${accountId}` : '/repos';
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

  /* Grouped by source, never one flat list. A repository is *in* an account --
     that is the rule the whole information architecture runs on -- and 240
     undifferentiated rows make the corpus look like a bag of names. Two owners
     can also share a name across hosts, so the group is (owner, host).

     Collapsed by default because most owners hold one or two repositories:
     sixty-six open cards would be worse than the flat list, while sixty-six
     summary lines are an index you can read. Filtering opens what matches, so
     a search never hides its own results behind a disclosure triangle. */
  const cols = [
    { key: 'name', label: 'Repository', render: (r) => h('div', {},
        h('span', { class: 'mono', style: 'font-weight:550' }, r.name),
        r.description ? h('div', { style: 'font-size:11px;color:var(--text-faint);max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, r.description) : null) },
    { key: 'primary_language', label: 'Lang', render: (r) => (r.primary_language ? h('span', { class: 'badge muted' }, r.primary_language) : '\u2014') },
    { key: 'is_private', label: 'Vis', render: (r) => h('span', { class: `badge ${r.is_private ? 'violet' : 'info'}` }, r.is_private ? 'private' : 'public') },
    { key: 'commit_count', label: 'Commits', num: true, render: (r) => num(r.commit_count) },
    { key: 'file_count', label: 'Files', num: true, render: (r) => num(r.file_count) },
    { key: 'pair_count', label: 'Pairs', num: true, render: (r) => num(r.pair_count) },
    { key: 'author_count', label: 'Authors', num: true, render: (r) => num(r.author_count) },
    { key: 'clone_mode', label: 'Mirror', render: (r) => h('span', { class: `badge ${r.has_churn ? 'muted' : 'warn'}`, title: r.has_churn ? 'Full clone: line-level churn available' : 'Blobless mirror: coupling is complete, but no line counts' }, r.clone_mode || '\u2014') },
    { key: 'last_commit_at', label: 'Last commit', render: (r) => when(r.last_commit_at) },
    { key: 'ingest_status', label: 'Status', render: (r) => (r.ingest_error ? h('span', { class: 'badge danger', title: r.ingest_error }, 'failed') : statusBadge(r.ingest_status)) },
  ];
  const tableOpts = {
    initialSort: SORTABLE.has(params.order_by) ? params.order_by : 'commit_count',
    onRow: (r) => go(`/repos/${r.id}`),
    empty: 'No repositories match those filters.',
  };

  if (account) {
    // Already inside one source: the grouping is the page.
    wrap.append(card(`${repos.count} repositories`,
                     dataTable(repos.repos, cols, tableOpts)));
    return wrap;
  }

  const groups = new Map();
  for (const r of repos.repos) {
    const key = `${r.owner}\u0000${r.host || 'github.com'}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  /* Groups are ordered by whatever the reader is sorting on, not always by
     commits: Overview's "Files tracked" tile opens `?order_by=file_count`
     meaning *show me the biggest*, and a group order fixed to commits answers
     a question nobody asked. Counts sum across the group; dates take the most
     recent, since a group is as fresh as its freshest repository. */
  const SUMMED = new Set(['commit_count', 'file_count', 'pair_count', 'author_count',
                          'stargazers', 'disk_usage_kb', 'total_insertions']);
  const LATEST = new Set(['last_commit_at', 'github_pushed_at', 'last_ingest_at']);
  const sortKey = tableOpts.initialSort;
  const groupRank = (rows) => {
    if (LATEST.has(sortKey)) {
      return Math.max(...rows.map((r) => (r[sortKey] ? Date.parse(r[sortKey]) : 0)));
    }
    return rows.reduce((n, r) => n + Number(r[SUMMED.has(sortKey) ? sortKey : 'commit_count'] || 0), 0);
  };
  const ordered = [...groups.entries()]
    .map(([key, rows]) => {
      const [owner, host] = key.split('\u0000');
      return { owner, host, rows,
               commits: rows.reduce((n, r) => n + Number(r.commit_count || 0), 0),
               rank: groupRank(rows),
               accountId: rows[0].account_id };
    })
    .sort((a, b) => (sortKey === 'name'
      ? a.owner.localeCompare(b.owner)
      : b.rank - a.rank || b.rows.length - a.rows.length));

  // A filter is a search: what it finds should be open, not hidden one click
  // further in.
  const filtering = Boolean(params.q || params.lang || params.status);
  // Arriving on an explicit ranking -- from a tile that counted something --
  // the leader is the answer, so it is open on arrival.
  const ranking = Boolean(params.order_by);

  wrap.append(h('div', { class: 'section-title' },
    `${num(repos.count)} repositories across ${num(ordered.length)} sources`));

  // One list, not sixty-six stacked blocks. The page rhythm is 16px between
  // blocks; items *within* a list are tighter, and that is a real distinction
  // rather than an exception -- so the list is one block and its own spacing is
  // its own business.
  const list = h('div', { class: 'repo-groups' });
  wrap.append(list);

  for (const g of ordered) {
    const body = h('div', { class: 'card-body flush' });
    let built = false;
    const panel = h('details',
      { class: 'repo-group',
        open: filtering || ordered.length <= 3 || (ranking && g === ordered[0]) },
      h('summary', { class: 'repo-group-head' },
        h('span', { class: 'repo-group-owner mono' }, g.owner),
        h('span', { class: 'repo-group-host' }, g.host),
        h('span', { class: 'repo-group-counts' },
          `${num(g.rows.length)} ${g.rows.length === 1 ? 'repository' : 'repositories'}`,
          h('span', { class: 'repo-group-dot' }, '\u00b7'),
          `${num(g.commits)} commits`),
        g.accountId
          ? h('a', { class: 'repo-group-open', href: `/sources/${g.accountId}`,
                     'data-nav': true,
                     onclick: (e) => e.stopPropagation() }, 'open source \u2192')
          : null),
      body);
    // Built on first open: sixty-six tables rendered up front is a lot of DOM
    // for a page where most stay shut.
    const build = () => {
      if (built) return;
      built = true;
      body.append(dataTable(g.rows, cols, tableOpts));
    };
    if (panel.open) build();
    panel.addEventListener('toggle', build);
    list.append(panel);
  }

  return wrap;
};

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
      statTile('Commits', num(repo.commit_count), `${num(repo.pair_population)} pair-eligible`,
               () => go(`/repos/${id}?tab=overview`)),
      statTile('Files', num(repo.file_count), `${num(repo.pair_count)} coupling pairs`,
               () => go(`/repos/${id}?tab=files`)),
      statTile('Authors', num(repo.author_count), 'distinct contributors',
               () => go(`/insights/risk?repo=${id}`)),
      statTile('Churn', repo.has_churn ? `+${num(repo.total_insertions)}` : 'n/a',
               repo.has_churn ? `−${num(repo.total_deletions)} lines` : 'blobless mirror',
               () => go(`/repos/${id}?tab=files`)),
      statTile('Stars', num(repo.stargazers), `${num(repo.forks_count)} forks`,
               () => go(`/repos/${id}?tab=meta`)),
      statTile('Mirror', bytes(repo.mirror_size_kb), repo.clone_mode || '—',
               () => go(`/repos/${id}?tab=meta`)),
      statTile('First commit', dateStr(repo.first_commit_at), `last ${when(repo.last_commit_at)}`,
               () => go(`/repos/${id}?tab=overview`)),
      statTile('Last ingest', when(repo.last_ingest_at), repo.ingest_status,
               () => go('/jobs')),
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

on('/repos', reposView);
on('/sources/:id', reposView);
on('/accounts/:id', ({ id }) => { go(`/sources/${id}`); return h('div'); });

/** Repository-level impact graph: nodes are repos, edges are validated impact. */
async function repoImpactGraphView(params) {
  const minScore = Number(params.min || 0.4);
  const data = await api('/api/impact/graph', { min_score: minScore, limit: 600 });

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
  scoreInput.addEventListener('input', () => { scoreLabel.textContent = `min ${Number(scoreInput.value).toFixed(2)}`; });
  scoreInput.addEventListener('change', () => go(`/insights/graph?mode=repos&min=${scoreInput.value}`));

  wrap.append(h('div', { class: 'toolbar' },
    h('div', { class: 'field' }, h('label', {}, 'Min score'), scoreInput, scoreLabel),
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
    'Every edge is backed by a dependency declared in a manifest or by an observed ',
    'version bump — there is no inferred tier to filter out.'));
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
  // Facts about the repository, several of which are ways out: the fork's
  // parent, the language, and the repository itself on GitHub.
  const rows = [
    ['Full name', repo.html_url
      ? h('a', { href: repo.html_url, target: '_blank', rel: 'noopener' }, repo.full_name)
      : repo.full_name],
    ['GitHub id', repo.github_id],
    ['Default branch', repo.default_branch],
    ['Visibility', repo.visibility],
    ['License', repo.license_spdx],
    ['Topics', (repo.topics || []).length
      ? h('span', {}, ...(repo.topics || []).map((t) => h('span', {
          class: 'badge muted is-link', style: 'margin-right:4px',
          title: `Search repositories for ${t}`,
          onclick: () => go(`/repos?q=${encodeURIComponent(t)}`),
        }, t)))
      : '—'],
    ['Language', repo.primary_language
      ? h('a', { href: `/repos?lang=${encodeURIComponent(repo.primary_language)}`,
                 'data-nav': true }, repo.primary_language)
      : '—'],
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
    ['Ingest history', h('a', { href: '/jobs', 'data-nav': true }, 'every run')],
  ];
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of rows) {
    // Elements pass through: several of these values are links, and String()
    // turned them into "[object HTMLAnchorElement]".
    const value = v === null || v === undefined ? '—'
      : (v instanceof window.Node ? v : String(v));
    dl.append(h('dt', {}, k), h('dd', {}, value));
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
/* Every distribution at full size. The card on Overview is a thumbnail of the
   same definition; this is where there is room to show each bucket with its
   count, and to say what the chart is actually claiming. */
on('/insights/shape', async () => {
  const shape = await api('/api/overview/shape');
  const wrap = await insightsShell('shape', null, [], null, { scoped: false });
  wrap.append(h('div', { class: 'grid grid-2' },
    ...Object.entries(SHAPE).map(([key, spec]) => {
      const rows = spec.rows(shape);
      return card(spec.title,
        h('div', { class: 'card-body' },
          shapeChart(spec, rows, { small: false, values: rows.length <= 12 })),
        rows.length ? spec.says(rows) : 'No data yet',
        null, `/insights/shape/${key}`);
    })));
  return wrap;
});

on('/insights/shape/:metric', async ({ metric }) => {
  const spec = SHAPE[metric];
  if (!spec) return notFound(`No distribution called ${metric}`);
  const shape = await api('/api/overview/shape');
  const rows = spec.rows(shape);
  const total = rows.reduce((n, r) => n + r.value, 0) || 1;

  const wrap = await insightsShell('shape', null, [[spec.title]],
    pageHead(spec.title, spec.says(rows),
      spec.more ? [h('a', { class: 'btn primary', href: spec.more, 'data-nav': true },
                      'Open the full analysis')] : []),
    { scoped: false });

  // No stat strip here: buckets, total, largest and scale are all already on
  // the chart or in the table below it, and a figure that opens nothing is a
  // dead end. Restating them would be decoration standing between the reader
  // and the diagram they came for.

  wrap.append(card(spec.title,
    h('div', { class: 'card-body' }, shapeChart(spec, rows, { small: false, drill: true })),
    spec.means, null, spec.more || '/insights/shape'));

  // Every bucket, with its share -- the part a card has no room for.
  wrap.append(card('Every bucket', dataTable(
    rows.map((r) => ({ ...r, share: r.value / total })), [
      { key: 'label', label: spec.axis, render: (r) => h('span', { class: 'mono' }, r.label) },
      { key: 'value', label: spec.unit.replace(/^./, (c) => c.toUpperCase()),
        num: true, render: (r) => num(r.value) },
      { key: 'share', label: 'Share', num: true,
        render: (r) => h('div', { class: 'confbar', style: 'justify-content:flex-end' },
          h('span', {}, pct(r.share)), bar(r.share)) },
    ], {
      initialSort: 'value',
      onRow: rows.some((r) => r.to) ? (r) => r.to && go(r.to) : undefined,
      empty: 'Nothing recorded yet.',
    }),
    rows.some((r) => r.to) ? 'Click a bucket to open what it counts'
                           : 'Counted across the whole corpus',
    null, spec.more || '/insights/shape'));
  return wrap;
});

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
  edgeCount.addEventListener('input', () => { edgeLabel.textContent = `${edgeCount.value} edges`; });
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

/* Activity: every call this deployment served, and what it returned.
   The drill-down the rest of the app has -- a ranking, then a list, then the
   thing itself -- applied to traffic: Overview ranks tools, this lists their
   calls, and one call opens the arguments it was given and the reply that went
   back. */
on('/activity', async (_args, params) => {
  const surface = params.surface || '';
  const status = params.status || '';
  const name = params.name || '';
  const hours = Number(params.hours || 24);

  const [summary, list] = await Promise.all([
    api('/api/calls/summary', {
      hours, surface: surface || undefined, status: status || undefined,
    }),
    api('/api/calls', {
      surface: surface || undefined,
      status: status || undefined,
      name: name || undefined,
      hours,
      limit: 200,
    }),
  ]);
  const c = summary.summary || {};

  const wrap = h('div');
  wrap.append(crumbs(['Overview', '/'], ['Activity'], ...(name ? [[name]] : [])));
  wrap.append(pageHead('Activity',
    'Every call served, what it was asked, and what went back. Kept for 30 days.'));

  // Each figure is a filter of the list below it, so a number leads to the
  // calls it counts rather than being read and left.
  const filtered = (over) => () => {
    const q = new URLSearchParams(params);
    for (const [k, v] of Object.entries(over)) { if (v) q.set(k, v); else q.delete(k); }
    go(`/activity${q.toString() ? '?' + q : ''}`);
  };
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Calls', num(c.calls || 0), `in the last ${c.hours || hours}h`,
             filtered({ surface: '', status: '' })),
    statTile('MCP', num(c.mcp_calls || 0), 'agent tool calls', filtered({ surface: 'mcp' })),
    statTile('HTTP', num(c.http_calls || 0), 'API requests', filtered({ surface: 'http' })),
    statTile('Errors', num(c.errors || 0), 'failed calls', filtered({ status: 'error' })),
    statTile('Median', c.p50_ms == null ? '\u2014' : `${c.p50_ms}ms`,
             c.p95_ms == null ? '' : `p95 ${c.p95_ms}ms`, filtered({ status: '' })),
    statTile('Distinct clients', num(c.clients || 0), 'by user-agent',
             filtered({ surface: '', status: '' }))));

  if (c.dropped) {
    wrap.append(h('div', { class: 'help', style: 'border-left-color:var(--warn)' },
      `${num(c.dropped)} call(s) went unrecorded because the write queue was full. `
      + 'Recording is dropped rather than allowed to slow a reply, so the counts '
      + 'above are a floor, not a total.'));
  }

  const chip = (label, key, value) => h('button', {
    class: `btn sm${(params[key] || '') === value ? ' primary' : ''}`,
    onclick: () => {
      const p = new URLSearchParams(params);
      if (value) p.set(key, value); else p.delete(key);
      // A tool name belongs to one surface, so keeping it while switching
      // surface asks for MCP calls to an HTTP route and returns nothing --
      // which reads as the filter being broken rather than empty.
      if (key === 'surface') p.delete('name');
      go(`/activity${p.toString() ? '?' + p : ''}`);
    },
  }, label);

  wrap.append(h('div', { class: 'toolbar' },
    h('div', { class: 'field' }, h('label', {}, 'Surface'),
      chip('All', 'surface', ''), chip('MCP', 'surface', 'mcp'), chip('HTTP', 'surface', 'http')),
    h('div', { class: 'field' }, h('label', {}, 'Status'),
      chip('Any', 'status', ''), chip('Errors only', 'status', 'error')),
    h('div', { class: 'field' }, h('label', {}, 'Window'),
      chip('1h', 'hours', '1'), chip('24h', 'hours', ''), chip('7d', 'hours', '168')),
    name ? h('button', { class: 'btn sm', onclick: () => {
      const p = new URLSearchParams(params); p.delete('name');
      go(`/activity${p.toString() ? '?' + p : ''}`);
    } }, `clear "${name}"`) : null));

  wrap.append(card('By tool or route', dataTable(summary.by_name || [], [
    { key: 'surface', label: 'Surface', render: (r) => h('span', { class: `badge ${r.surface === 'mcp' ? 'ok' : 'muted'}` }, r.surface) },
    { key: 'name', label: 'Name', render: (r) => h('span', { class: 'mono' }, r.name) },
    { key: 'calls', label: 'Calls', num: true, render: (r) => num(r.calls) },
    { key: 'errors', label: 'Errors', num: true },
    { key: 'avg_ms', label: 'Avg', num: true, render: (r) => `${fx(r.avg_ms, 0)}ms` },
    { key: 'max_ms', label: 'Slowest', num: true, render: (r) => `${num(r.max_ms)}ms` },
    { key: 'avg_rows', label: 'Avg rows', num: true, render: (r) => (r.avg_rows == null ? '\u2014' : fx(r.avg_rows, 0)) },
    { key: 'last_call', label: 'Last used', render: (r) => (r.last_call ? when(r.last_call)
        : h('span', { class: 'badge muted' }, 'never called')) },
  ], {
    initialSort: 'calls',
    onRow: (r) => {
      if (!r.calls) return;
      const p = new URLSearchParams(params);
      p.set('surface', r.surface); p.set('name', r.name);
      go(`/activity?${p}`);
    },
    empty: 'No calls in this window.',
  }),
    `Every MCP tool is listed, called or not \u2014 ${summary.mcp_tools || 0} are registered, `
    + 'and a tool nobody uses is the row worth seeing'));

  wrap.append(h('div', { class: 'section-title' }, `${list.count} most recent`));
  wrap.append(card('Calls', dataTable(list.calls || [], [
    { key: 'at', label: 'When', render: (r) => when(r.at) },
    { key: 'surface', label: 'Surface', render: (r) => h('span', { class: `badge ${r.surface === 'mcp' ? 'ok' : 'muted'}` }, r.surface) },
    { key: 'name', label: 'Tool or route', render: (r) => h('span', { class: 'mono' }, r.name) },
    { key: 'status', label: 'Status', render: (r) => h('span', { class: `badge ${r.status === 'ok' ? 'ok' : 'danger'}` }, r.status) },
    { key: 'duration_ms', label: 'Took', num: true, render: (r) => `${num(r.duration_ms)}ms` },
    { key: 'result_rows', label: 'Rows', num: true, render: (r) => (r.result_rows == null ? '\u2014' : num(r.result_rows)) },
    { key: 'result_bytes', label: 'Size', num: true, render: (r) => (r.result_bytes == null ? '\u2014' : bytes(r.result_bytes / 1024)) },
    { key: 'client', label: 'Client', render: (r) => h('span', { class: 'card-sub' }, (r.client || '\u2014').slice(0, 42)) },
  ], {
    initialSort: 'at',
    onRow: (r) => go(`/activity/${r.id}`),
    empty: 'No calls match those filters.',
  }), 'Click a call to see exactly what it asked for and what it got back'));
  return wrap;
});

on('/activity/:id', async ({ id }) => {
  const call = await api(`/api/calls/${id}`);
  const wrap = h('div');
  wrap.append(crumbs(['Overview', '/'],
                     ['Activity', `/activity?surface=${call.surface}`],
                     [call.name]));
  wrap.append(pageHead(h('span', { class: 'mono' }, call.name),
    `${call.surface.toUpperCase()}${call.method ? ' ' + call.method : ''} · ${when(call.at)}`,
    [h('a', { class: 'btn', href: `/activity?surface=${call.surface}&name=${encodeURIComponent(call.name)}`, 'data-nav': true },
       'Other calls to this')]));

  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Status', call.status, call.error || 'no error'),
    statTile('Took', `${num(call.duration_ms)}ms`, 'server-side'),
    statTile('Rows', call.result_rows == null ? '\u2014' : num(call.result_rows), 'in the reply'),
    statTile('Size', call.result_bytes == null ? '\u2014' : bytes(call.result_bytes / 1024), 'of response body')));

  if (call.error) {
    wrap.append(h('div', { class: 'help', style: 'border-left-color:var(--danger)' },
      h('strong', {}, 'Error: '), call.error));
  }

  const pre = (value) => h('pre', { class: 'payload' },
    typeof value === 'string' ? value : JSON.stringify(value, null, 2));

  wrap.append(card('What was asked',
    h('div', { class: 'card-body' },
      call.arguments ? pre(call.arguments)
                     : h('div', { class: 'empty' }, 'No arguments.')),
    call.surface === 'mcp' ? 'The tool arguments the agent supplied'
                           : 'The query string the caller sent'));

  const body = call.result_preview;
  const truncated = body && typeof body === 'object' && body.truncated;
  wrap.append(card('What came back',
    h('div', { class: 'card-body' },
      body == null ? h('div', { class: 'empty' }, 'No body recorded.')
        : pre(truncated ? body.head : body)),
    truncated
      ? `Truncated: the reply was ${bytes(body.bytes / 1024)}, and the log keeps the first part`
      : 'The reply exactly as it was returned'));
  wrap.append(h('div', { class: 'card-sub' }, `Client: ${call.client || 'unknown'}`));
  return wrap;
});

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

on('/jobs', async (_args, params) => {
  const tab = params.tab || 'runs';
  const [runs, cfg] = await Promise.all([api('/api/runs', { limit: 60 }), api('/api/config')]);
  const wrap = h('div');
  wrap.append(
    pageHead('Ingest jobs', `Refreshing on cron "${cfg.refresh_cron}" (${cfg.scheduler_timezone})`, [
      h('button', { class: 'btn primary', onclick: triggerRefresh }, 'Run now'),
    ]),
  );
  wrap.append(h('div', { class: 'tabs' },
    ...[['runs', 'Run history'], ['settings', 'Schedule & settings']].map(([k, l]) =>
      h('button', { class: `tab${tab === k ? ' active' : ''}`,
                    onclick: () => go(`/jobs?tab=${k}`) }, l))));
  if (tab === 'settings') {
    wrap.append(await settingsPanel(cfg));
    return wrap;
  }
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

/* Operational settings, editable here rather than only in .env, because a
   deployment that has to be restarted to be slowed down will not be slowed
   down. Anything that changes what the numbers *mean* -- pair support, rename
   similarity, the measure set -- stays in the environment on purpose: it is
   versioned with the deployment, and must not differ between two readings of
   the same table. Those are shown, clearly, as read-only. */
async function settingsPanel(cfg) {
  const box = h('div');
  const { settings } = await api('/api/settings');

  const PRESETS = [
    ['0 * * * *', 'Hourly'],
    ['*/30 * * * *', 'Every 30 minutes'],
    ['*/15 * * * *', 'Every 15 minutes'],
    ['0 */6 * * *', 'Every 6 hours'],
    ['0 3 * * *', 'Daily at 03:00'],
  ];

  const rows = settings.map((setting) => {
    const input = h('input', { class: 'input', value: setting.value, spellcheck: 'false',
                               autocomplete: 'off', style: 'width:170px;font-family:var(--mono)' });
    const save = h('button', { class: 'btn primary' }, 'Save');
    const reset = h('button', { class: 'btn', title: `Back to the deployment's value, ${setting.from_env}` }, 'Use .env');
    const status = h('span', { class: 'card-sub' },
      setting.overridden ? 'set here' : `from .env (${setting.from_env})`);

    const put = async (value) => {
      save.disabled = reset.disabled = true;
      try {
        const out = await apiSend('PUT', `/api/settings/${setting.name}`, { value });
        input.value = out.value;
        status.textContent = out.overridden ? 'set here' : `from .env (${setting.from_env})`;
        toast(`${setting.name} is now "${out.value}" — applies within a minute`);
      } catch (err) {
        toast(String(err.message || err), true);
      } finally {
        save.disabled = reset.disabled = false;
      }
    };
    save.addEventListener('click', () => put(input.value.trim()));
    reset.addEventListener('click', () => put(''));

    return h('div', { class: 'setting-row' },
      h('div', {},
        h('strong', {}, setting.name === 'refresh_cron' ? 'Refresh schedule' : 'Discovery schedule'),
        h('div', { class: 'card-sub' }, setting.name === 'refresh_cron'
          ? 'Re-fetch known repositories and rebuild whatever moved. No GitHub API calls.'
          : 'Additionally re-list each account through the GitHub API to find new repositories.')),
      h('div', { class: 'toolbar', style: 'margin:0' },
        input, save, reset, status),
      h('div', { class: 'pillrow' }, ...PRESETS.map(([expr, label]) =>
        h('button', { class: `btn sm${setting.value === expr ? ' primary' : ''}`,
                      onclick: () => put(expr) }, label))));
  });

  box.append(card('Schedules', h('div', { class: 'card-body' }, ...rows),
    'Stored in the database and picked up within a minute — no restart, no redeploy.'));

  // --- the token ---------------------------------------------------------
  const tok = cfg.github_token || {};
  box.append(card('GitHub token',
    h('div', { class: 'card-body' },
      h('div', { class: 'toolbar', style: 'margin:0' },
        h('span', { class: `badge ${tok.present ? 'ok' : 'warn'}` },
          tok.present ? 'configured' : 'not set'),
        h('span', { class: 'card-sub' }, `source: ${tok.source}`)),
      h('div', { class: 'help', style: 'margin:12px 0 0' },
        tok.present
          ? 'A token is in place. Its value is never returned by the API, so it cannot be read back here.'
          : 'No token. Every account in this corpus is public, so ingest runs unauthenticated — '
            + 'GitHub allows 60 API calls an hour that way, which only limits discovery, not fetching.',
        ' To set or rotate one, put it in ',
        h('code', {}, '.env'),
        ' as ',
        h('code', {}, 'GITHUB_TOKEN'),
        ' (or the file at ',
        h('code', {}, 'GITHUB_TOKEN_FILE'),
        '), then ',
        h('code', {}, 'docker compose up -d'),
        '. It is deliberately not editable here: a credential posted through this ',
        'unauthenticated local API would be readable by anything that can reach the ',
        'database, and would silently diverge from the value the host rotates.')),
    'Read from the environment or a host-managed file'));

  // --- who may read this deployment --------------------------------------
  const admin = state.me && state.me.role === 'admin';
  const access = (await api('/api/settings').catch(() => ({}))).access || {};
  const modeRow = (surface, title, note) => {
    const on = access[surface] !== 'open';
    const set = async (value) => {
      try {
        await apiSend('PUT', `/api/settings/${surface}_auth`, { value });
        toast(value === 'open'
          ? `${title}: sign-in no longer required`
          : `${title}: sign-in required`);
        route();
      } catch (err) { toast(err.message || String(err), true); }
    };
    return h('div', { class: 'setting-row' },
      h('div', {}, h('strong', {}, title), h('div', { class: 'card-sub' }, note)),
      h('div', { class: 'toolbar', style: 'margin:0' },
        h('button', { class: `btn${on ? ' primary' : ''}`, disabled: !admin,
                      onclick: () => set('required') }, 'Sign-in required'),
        h('button', { class: `btn${on ? '' : ' primary'}`, disabled: !admin,
                      onclick: () => set('open') }, 'Open to anyone'),
        h('span', { class: 'card-sub' }, admin ? '' : 'administrators only')));
  };

  box.append(card('Who may read this',
    h('div', { class: 'card-body' },
      modeRow('dashboard', 'Dashboard and API',
              'With sign-in off, anyone who can reach this address can read everything. '
              + 'Managing people always needs an account, whichever way this is set.'),
      modeRow('mcp', 'MCP server',
              'Whether an agent must present a token. Off suits a laptop; on suits anything shared.')),
    'Applies to the next request — nothing restarts',
    h('a', { class: 'btn', href: '/people', 'data-nav': true }, 'People')));

  // --- what is deliberately not editable ---------------------------------
  const fixed = [
    ['Max files per commit', cfg.max_files_per_commit, 'a commit touching more is not paired'],
    ['Min pair support', cfg.min_pair_support, 'co-changes before a pair is stored'],
    ['Rename similarity', `${cfg.rename_similarity}%`, 'threshold git uses to follow a rename'],
    ['Recency half-life', `${cfg.recency_half_life_days}d`, 'weighting applied to older commits'],
    ['Blobless above', bytes(cfg.blobless_threshold_kb), 'mirror metadata only past this size'],
    ['Scheduler timezone', cfg.scheduler_timezone, 'the crons above are read in it'],
  ];
  box.append(card('Fixed for this deployment',
    dataTable(fixed.map(([name, value, note]) => ({ name, value: String(value), note })), [
      { key: 'name', label: 'Setting' },
      { key: 'value', label: 'Value', render: (r) => h('span', { class: 'mono' }, r.value) },
      { key: 'note', label: 'What it does' },
    ], { sortable: false }),
    'Changing these changes what every number means, so they are versioned with the deployment in .env rather than editable at runtime'));
  return box;
}

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
    if (e.key === 'Escape') { input.blur(); close(); return; }
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
  const saved = store.get('git-synapse.theme');
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    store.set('git-synapse.theme', next);
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

/* Everyone may see who has access -- it is a shared dashboard, and knowing who
   else reads it is part of using it. Only an administrator may change it. */
on('/people', async () => {
  const [data, cfg] = await Promise.all([
    api('/api/users'),
    api('/api/settings').catch(() => ({ access: {} })),
  ]);
  const admin = state.me && state.me.role === 'admin';
  const wrap = h('div');

  wrap.append(pageHead('People',
    admin ? 'Everyone who can read this deployment. You may add and remove them.'
          : 'Everyone who can read this deployment. Ask an administrator to change it.'));

  const users = data.users || [];
  const admins = users.filter((u) => u.role === 'admin' && u.is_active).length;
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('People', num(users.length), `${num(users.filter((u) => u.is_active).length)} active`),
    statTile('Administrators', num(admins), 'may add and remove people'),
    statTile('Signed in now', num(users.reduce((n, u) => n + Number(u.active_sessions || 0), 0)),
             'live sessions', () => go('/activity')),
    statTile('Sign-in', (cfg.access || {}).dashboard === 'open' ? 'not required' : 'required',
             admin ? 'change under Jobs → settings' : 'set by an administrator',
             admin ? () => go('/jobs?tab=settings') : null)));

  if (admin) wrap.append(addPersonCard());

  wrap.append(card(`${users.length} with access`, dataTable(users, [
    { key: 'name', label: 'Name', render: (u) => h('div', {},
        h('div', {}, u.name),
        h('div', { class: 'card-sub' }, u.email)) },
    { key: 'role', label: 'Role', render: (u) => h('span',
        { class: `badge ${u.role === 'admin' ? 'ok' : 'muted'}` }, u.role) },
    { key: 'is_active', label: 'Status', render: (u) => h('span',
        { class: `badge ${u.is_active ? 'info' : 'warn'}` }, u.is_active ? 'active' : 'deactivated') },
    { key: 'active_sessions', label: 'Sessions', num: true },
    { key: 'last_login_at', label: 'Last signed in', render: (u) => (u.last_login_at ? when(u.last_login_at) : 'never') },
    { key: 'created_by_email', label: 'Added by', render: (u) => h('span', { class: 'card-sub' }, u.created_by_email || 'first account') },
    ...(admin ? [{ key: 'act', label: '', sortable: false, render: (u) => peopleActions(u) }] : []),
  ], { initialSort: 'created_at', empty: 'Nobody yet.' }),
    admin ? 'A person may read everything; only an administrator may change who can.'
          : 'Read-only: you are not an administrator.'));
  return wrap;
});

/** The add-a-person form. Admin-only, and the API enforces that too. */
function addPersonCard() {
  const email = h('input', { class: 'input', type: 'email', placeholder: 'them@example.com' });
  const name = h('input', { class: 'input', placeholder: 'Their name' });
  const password = h('input', { class: 'input', type: 'password',
                                placeholder: 'at least 12 characters' });
  const role = h('select', { class: 'input' },
    h('option', { value: 'member' }, 'Member — reads everything'),
    h('option', { value: 'admin' }, 'Administrator — may also add people'));
  const submit = h('button', { class: 'btn primary' }, 'Add person');

  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      await apiSend('POST', '/api/users', {
        email: email.value.trim(), name: name.value.trim(),
        password: password.value, role: role.value,
      });
      toast(`${email.value.trim()} can now sign in`);
      route();
    } catch (err) {
      toast(err.message || String(err), true);
    } finally {
      submit.disabled = false;
    }
  });

  return card('Add a person',
    h('div', { class: 'card-body' },
      h('div', { class: 'grid grid-2' },
        field('Email', email),
        field('Name', name),
        field('Temporary password', password,
              'They keep it until they change it; there is no email from this deployment.'),
        field('Role', role))),
    'They will be able to read everything, immediately',
    submit);
}

/** Per-person controls. Each refuses server-side too, not only here. */
function peopleActions(u) {
  const act = async (label, fn) => {
    try { await fn(); toast(label); route(); }
    catch (err) { toast(err.message || String(err), true); }
  };
  const me = state.me && state.me.id === u.id;
  return h('div', { class: 'pillrow' },
    h('button', { class: 'btn sm', title: u.role === 'admin'
        ? 'Make a member: they keep read access but cannot add people'
        : 'Make an administrator: they may add and remove people',
      onclick: (e) => { e.stopPropagation(); act(`${u.email} is now a ${u.role === 'admin' ? 'member' : 'administrator'}`,
        () => apiSend('PATCH', `/api/users/${u.id}`, { role: u.role === 'admin' ? 'member' : 'admin' })); },
    }, u.role === 'admin' ? 'Make member' : 'Make admin'),
    h('button', { class: 'btn sm', title: u.is_active
        ? 'Deactivate: their sessions end immediately and they cannot sign in'
        : 'Reactivate',
      onclick: (e) => { e.stopPropagation(); act(`${u.email} ${u.is_active ? 'deactivated' : 'reactivated'}`,
        () => apiSend('PATCH', `/api/users/${u.id}`, { is_active: !u.is_active })); },
    }, u.is_active ? 'Deactivate' : 'Reactivate'),
    me ? null : h('button', { class: 'btn sm danger',
      onclick: (e) => {
        e.stopPropagation();
        if (!window.confirm(`Remove ${u.email}? Their sessions and tokens go with them.`)) return;
        act(`${u.email} removed`, () => apiSend('DELETE', `/api/users/${u.id}`));
      },
    }, 'Remove'));
}

/* A token is a person's key: it carries their identity and role, does exactly
   what they can do, and stops working when their account does. Anyone may make
   one -- an agent calling the API is the person who set it up. */
on('/tokens', async () => {
  const data = await api('/api/auth/tokens');
  const wrap = h('div');
  wrap.append(pageHead('API tokens',
    'For agents and scripts calling this deployment. A token acts as you.'));

  const name = h('input', { class: 'input', placeholder: 'e.g. laptop agent' });
  const days = h('select', { class: 'input' },
    h('option', { value: '90' }, 'Expires in 90 days'),
    h('option', { value: '365' }, 'Expires in a year'),
    h('option', { value: '' }, 'Never expires'));
  const secret = h('div', { class: 'token-reveal', hidden: true });
  const mint = h('button', { class: 'btn primary' }, 'Create token');

  mint.addEventListener('click', async () => {
    mint.disabled = true;
    try {
      const out = await apiSend('POST', '/api/auth/tokens', {
        name: name.value.trim(), days: days.value ? Number(days.value) : null,
      });
      secret.replaceChildren(
        h('div', { class: 'token-note' }, out.note),
        h('code', { class: 'token-value' }, out.token),
        h('button', { class: 'btn sm', onclick: () => {
          navigator.clipboard?.writeText(out.token);
          toast('Token copied');
        } }, 'Copy'));
      secret.hidden = false;
      name.value = '';
      const table = document.getElementById('token-table');
      if (table) table.replaceChildren(tokenTable((await api('/api/auth/tokens')).tokens));
    } catch (err) {
      toast(err.message || String(err), true);
    } finally {
      mint.disabled = false;
    }
  });

  wrap.append(card('Create a token',
    h('div', { class: 'card-body' },
      h('div', { class: 'grid grid-2' }, field('Name', name), field('Lifetime', days)),
      secret),
    'Shown once. It is stored as a hash, so it cannot be shown again.', mint));

  wrap.append(h('div', { class: 'help' },
    'Send it as ', h('code', {}, 'Authorization: Bearer ' + (data.prefix || 'gss_') + '…'),
    '. It carries your identity and your role, so it can do what you can do and no more.'));

  const host = h('div', { id: 'token-table' }, tokenTable(data.tokens));
  wrap.append(card(`${(data.tokens || []).length} tokens`, host,
    'Revoking one takes effect immediately'));
  return wrap;
});

const tokenTable = (tokens) => dataTable(tokens || [], [
  { key: 'name', label: 'Name' },
  { key: 'prefix', label: 'Starts with', render: (t) => h('code', { class: 'mono' }, `${t.prefix}…`) },
  { key: 'created_at', label: 'Created', render: (t) => when(t.created_at) },
  { key: 'last_used_at', label: 'Last used', render: (t) => (t.last_used_at ? when(t.last_used_at) : 'never') },
  { key: 'expires_at', label: 'Expires', render: (t) => (t.expires_at ? when(t.expires_at)
      : h('span', { class: 'badge warn', title: 'A token that never expires is a key left in a door' }, 'never')) },
  { key: 'act', label: '', sortable: false, render: (t) => h('button', {
      class: 'btn sm danger',
      onclick: async (e) => {
        e.stopPropagation();
        if (!window.confirm(`Revoke "${t.name}"? Anything using it stops working.`)) return;
        await apiSend('DELETE', `/api/auth/tokens/${t.id}`).catch((err) => toast(err.message, true));
        route();
      },
    }, 'Revoke') },
], { initialSort: 'created_at', empty: 'No tokens yet.' });

/* ==========================================================================
   Who is signed in

   Everything here is derived from public repositories, but the deployment is
   not: it says which repositories an organisation tracks, where its coupling
   is weakest, and which files one person alone understands. So there is a door
   -- and an administrator may leave it open, because a laptop demo and a
   shared internal dashboard are different things.
   ========================================================================== */

/** The mark, at whatever size the caller wants. Shared by splash and sign-in. */
const brandMark = (px) => {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 32 32');
  svg.setAttribute('class', 'gate-mark');
  svg.style.width = `${px}px`;
  svg.style.height = `${px}px`;
  svg.innerHTML =
    '<g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round">'
    + '<path d="M8 24 C 8 18.5, 9.5 15.5, 12 13.6"/>'
    + '<path d="M20 18.4 C 22.5 16.5, 24 13.5, 24 8"/></g>'
    + '<circle cx="14.6" cy="15.1" r="1.15" fill="currentColor" opacity=".55"/>'
    + '<circle cx="17.4" cy="16.9" r="1.15" fill="currentColor" opacity=".8"/>'
    + '<circle cx="8" cy="24" r="2.6" fill="currentColor"/>'
    + '<circle cx="24" cy="8" r="2.6" fill="currentColor"/>';
  return svg;
};

/** A field with its label, returned with the input exposed. */
function gateField(label, attrs, hint) {
  const input = h('input', { class: 'input', ...attrs });
  const node = h('label', { class: 'gate-field' },
    h('span', {}, label), input,
    hint ? h('span', { class: 'gate-hint' }, hint) : null);
  node.input = input;
  return node;
}

/**
 * The sign-in screen, and the first-run screen that creates the first
 * administrator.
 *
 * One component for both because they differ by two fields and a verb, and two
 * near-identical forms drift.
 */
function gate({ setup = false, minted = true } = {}) {
  const email = gateField('Email', { type: 'email', autocomplete: 'username',
                                     placeholder: 'you@example.com', required: true });
  const name = gateField('Name', { autocomplete: 'name', placeholder: 'Your name' });
  const password = gateField('Password',
    { type: 'password', autocomplete: setup ? 'new-password' : 'current-password',
      placeholder: setup ? `at least ${12} characters` : '', required: true },
    setup ? 'You will be the administrator: only you can add other people.' : null);
  // Nothing is signed in yet, so this screen is by necessity reachable by
  // anyone who reaches the port. The token is what makes reaching it first
  // insufficient.
  const token = gateField('Setup token',
    { autocomplete: 'off', spellcheck: 'false', class: 'input mono',
      placeholder: 'paste it here', required: true },
    minted ? 'Printed in the API log when it started: docker compose logs api'
           : 'The ADMIN_SETUP_TOKEN this deployment was configured with.');

  const error = h('div', { class: 'gate-error', hidden: true });
  const submit = h('button', { class: 'btn primary gate-submit', type: 'submit' },
    setup ? 'Create administrator' : 'Sign in');

  const form = h('form', { class: 'gate-form' },
    email, setup ? name : null, password, setup ? token : null, error, submit);

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    error.hidden = true;
    submit.disabled = true;
    submit.textContent = setup ? 'Creating…' : 'Signing in…';
    try {
      const body = setup
        ? { email: email.input.value, name: name.input.value,
            password: password.input.value, setup_token: token.input.value.trim() }
        : { email: email.input.value, password: password.input.value };
      await apiSend('POST', setup ? '/api/auth/setup' : '/api/auth/login', body);
      // Re-read rather than trusting the reply: this is the same call every
      // later page makes, so if it disagrees the problem shows up here.
      await loadMe();
      paintProfile();
      route();
    } catch (err) {
      error.textContent = err.message || String(err);
      error.hidden = false;
      password.input.value = '';
      password.input.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = setup ? 'Create administrator' : 'Sign in';
    }
  });

  setTimeout(() => email.input.focus(), 40);

  // Two halves: what this is, and the way in. The left side is the only place
  // in the app with room to say what the product does, and a person signing in
  // to someone else's deployment has usually never been told.
  return h('div', { class: 'gate' },
    h('div', { class: 'gate-panel' },
      h('div', { class: 'gate-aside' },
        h('div', { class: 'gate-aside-inner' },
          brandMark(40),
          h('h1', { class: 'gate-h1' }, 'Git Synapse'),
          h('p', { class: 'gate-lede' },
            'If I change this file, what else has to change? Answered from the '
            + 'history that is already in your repositories.'),
          h('ul', { class: 'gate-points' },
            h('li', {}, h('b', {}, 'Coupling'), ' — files that move together, ranked by 29 measures'),
            h('li', {}, h('b', {}, 'Across repositories'), ' — every edge backed by a manifest or a bump'),
            h('li', {}, h('b', {}, 'Measured'), ' — every prediction scored against the commits before it')),
          // Facts about the product, not about this corpus: the figures for
          // this deployment need a session to read, and a hard-coded "163
          // repositories" would be a false claim on anyone else's.
          h('div', { class: 'gate-figure' },
            h('span', {}, '29'), ' association measures · ',
            h('span', {}, 'prequential'), ' backtesting · ',
            h('span', {}, 'no'), ' inference without evidence'))),

      h('div', { class: 'gate-main' },
        h('div', { class: 'gate-head' },
          h('h2', { class: 'gate-title' }, setup ? 'Create the first account' : 'Sign in'),
          h('div', { class: 'gate-sub' }, setup
            ? 'Nobody has an account on this deployment yet. Yours will be the administrator.'
            : 'This deployment is private to the people who have been added to it.')),
        form)));
}

/** The signed-in user, or null. Read once at boot and after every change. */
async function loadMe() {
  try {
    const me = await fetch('/api/auth/me', { headers: { Accept: 'application/json' } })
      .then((r) => r.json());
    state.me = me.user;
    state.needsSetup = me.needs_setup;
    state.setupTokenMinted = me.setup_token_minted !== false;
    state.authRequired = me.auth_required;
  } catch {
    // The API is unreachable. Let the view report that rather than showing a
    // sign-in form for a server that cannot check one.
    state.me = null;
    state.needsSetup = false;
    state.authRequired = false;
  }
  return state.me;
}

/* Registered once, at module scope. Doing it inside paintProfile added a
   listener per call -- and that runs on boot, on sign-in, on sign-out and on
   every 401 -- each closure holding a menu element that had already been
   replaced. */
document.addEventListener('click', () => {
  const menu = document.querySelector('.profile-menu');
  if (menu) menu.hidden = true;
});

/** The avatar and menu, top right. Absent entirely when nobody is signed in. */
function paintProfile() {
  const host = document.getElementById('profile');
  if (!host) return;
  host.replaceChildren();
  if (!state.me) { host.hidden = true; return; }
  host.hidden = false;

  const initials = (state.me.name || state.me.email).trim().split(/\s+/)
    .slice(0, 2).map((w) => w[0]).join('').toUpperCase();

  const menu = h('div', { class: 'profile-menu', hidden: true },
    h('div', { class: 'profile-head' },
      h('div', { class: 'profile-name' }, state.me.name),
      h('div', { class: 'profile-email' }, state.me.email),
      h('span', { class: `badge ${state.me.role === 'admin' ? 'ok' : 'muted'}` }, state.me.role)),
    h('a', { class: 'profile-item', href: '/people', 'data-nav': true },
      state.me.role === 'admin' ? 'People and access' : 'People'),
    h('a', { class: 'profile-item', href: '/tokens', 'data-nav': true }, 'API tokens'),
    h('button', { class: 'profile-item danger', onclick: async () => {
      await apiSend('POST', '/api/auth/logout').catch(noop);
      await loadMe();
      paintProfile();
      go('/');
      route();
    } }, 'Sign out'));

  const button = h('button', {
    class: 'avatar', title: `${state.me.name} (${state.me.role})`,
    'aria-haspopup': 'menu',
    onclick: (ev) => { ev.stopPropagation(); menu.hidden = !menu.hidden; },
  }, initials);

  host.append(button, menu);
}

async function boot() {
  wireTheme();
  wireOmnibox();
  // Before the catalogue, before the footer: every other call needs to know
  // whether there is a door, and a 401 storm at boot is not a diagnosis.
  await loadMe();
  paintProfile();
  if (state.needsSetup || (state.authRequired && !state.me)) {
    window.addEventListener('popstate', route);
    return route();
  }
  try {
    const cat = await api('/api/measures');
    state.measures = cat.measures;
    state.byKey = new Map(cat.measures.map((m) => [m.key, m]));
    if (!state.byKey.has(state.measure)) state.measure = cat.default;
    paintMeasureBar();
  } catch (err) {
    toast(`Could not load measure catalogue: ${err.message}`, true);
  }
  api('/api/overview').then(paintFooter).catch(noop);
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
 * Both tiers are evidence: a dependency declared in a manifest, or a version
 * bump observed and resolved to the upstream commit it consumed. They were
 * measured at AUC 0.88 in sample against real dependency propagation. Tier is
 * always rendered rather than inferred from the score, because the score mixes
 * both and a reader cannot recover which from a number.
 */
const tierBadge = (row) => {
  if (row.is_declared)
    return h('span', { class: 'tier tier-declared', title: 'Declared dependency — validated tier (AUC 0.88 in sample, 0.69 held out)' },
      h('i', { class: 'dot' }), 'declared');
  if (row.has_bump_history)
    return h('span', { class: 'tier tier-bump', title: 'Observed manifest bumps — ground truth' },
      h('i', { class: 'dot' }), 'bump-backed');
  // Unreachable: every edge is written from a declared dependency or a bump.
  // Rendered rather than thrown so a stale row is visible, not silently blank.
  return h('span', { class: 'tier', title: 'No evidence recorded — this should not occur' },
    h('i', { class: 'dot' }), 'no evidence');
};

const adoptedAfter = (d) => (d === null || d === undefined ? '—' : `${Number(d).toFixed(1)}d`);

/** Render a chain as clickable nodes joined by weighted arrows. */
function chainNode(repos, hops, repoIds, reverse = false) {
  const wrap = h('div', { class: 'chain' });
  repos.forEach((name, i) => {
    wrap.appendChild(
      h('span', {
        class: `node${i === 0 ? ' first' : ''}${repoIds && repoIds[i] ? ' is-link' : ''}`,
        title: repoIds && repoIds[i] ? `Open ${name}` : null,
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
      'Inferring it from co-change instead — grouping commits by ticket key, or by ',
      'author session — reaches AUC 0.80 overall but only 0.63 on which way the arrow ',
      'points, and a baseline ignoring coupling entirely matches that: it ranks ',
      '“both repositories are busy”. Restricting to declared dependencies lifts the ',
      'base rate from 0.23% to 82% before any measure is evaluated.'),
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
    // There is no org-wide ranking here on purpose: an edge is a fact about
    // one pair of repositories, read from a manifest or a bump, and averaging
    // those into a league table says nothing a reader can act on.
    wrap.append(card('Pick a repository',
      h('div', { class: 'empty' },
        h('strong', {}, 'Choose a repository in Scope above.'),
        'Impact is a fact about one repository: what its changes reach, and what '
        + 'reaches it. Every edge is read from a dependency declared in a manifest '
        + 'or from a version bump that was actually observed.')));
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
    wrap.append(card(`${chains.chains.length} chains`, body,
      'Each hop is a declared dependency or an observed bump \u2014 click a repository to open it',
      null, '/insights/graph?mode=repos'));
  }

  wrap.append(h('div', { class: 'section-title' }, 'Declared dependencies and observed bumps'));
  wrap.append(h('div', { class: 'grid grid-2' },
    card(`Declared in manifests (${deps.declared.length})`,
      dataTable(deps.declared, [
        { key: 'dep_name', label: 'Module', render: (d) => h('span', { class: 'mono' }, d.dep_name) },
        { key: 'dep_repo', label: 'Tracked repo', render: (d) => (d.dep_repo ? h('a', { href: `/repos/${d.dep_repo_id}`, 'data-nav': true, class: 'mono' }, d.dep_repo) : h('span', { class: 'badge muted' }, 'external')) },
        { key: 'manifest', label: 'Manifest', render: (d) => h('span', { class: 'badge muted' }, d.manifest) },
        { key: 'dep_version', label: 'Version', render: (d) => h('span', { class: 'mono', style: 'font-size:11px' }, (d.dep_version || '').slice(0, 34)) },
      ], {
        // Only a tracked dependency has somewhere to go; an external module is
        // a name in a manifest and nothing more.
        onRow: (d) => d.dep_repo_id && go(`/repos/${d.dep_repo_id}`),
        empty: 'No manifest dependencies found.',
      }),
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
      statTile('Evidence', edge.is_declared ? 'declared' : 'bump-backed',
               edge.is_declared ? 'from a manifest' : 'from an observed bump'),
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
  ['shape', 'Distributions'],
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
async function insightsShell(section, repoId, trail = [], head = null,
                             { scoped = true } = {}) {
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
  // A section that is about one specific thing titles itself; the generic
  // heading would otherwise sit above it and the page would carry two.
  wrap.append(head || pageHead('Insights', 'What history says about the code.'));

  const repoSel = repoSelect(repos.repos, {
    selected: repoId,
    onChange: (value) => go(`/insights/${section}${value ? `?repo=${value}` : ''}`),
  });

  wrap.append(h('div', { class: 'tabs' }, ...INSIGHT_SECTIONS.map(([k, l]) =>
    h('button', { class: `tab${section === k ? ' active' : ''}`,
                  onclick: () => go(`/insights/${k}${repoId ? `?repo=${repoId}` : ''}`) }, l))));
  // A section that reads corpus-wide aggregates has no scope to offer. Showing
  // the selector anyway meant picking a repository navigated to ?repo=N, the
  // section ignored it, and the control snapped back to "All repositories" --
  // which reads as the page refusing the choice.
  wrap.append(scoped
    ? h('div', { class: 'toolbar' },
        h('div', { class: 'field' }, h('label', {}, 'Scope'), repoSel))
    : h('div', { class: 'toolbar' },
        h('span', { class: 'card-sub' },
          'Counted across every repository \u2014 these describe the corpus as a whole.')));
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

/** Horizontal bars with the value in line. `rows` need `label` and `value`. */
/* Position along the ramp, in degrees of hue. Teal through blue to violet: one
   family, so a chart reads as a single object, but far enough apart that
   neighbouring bars never blur together. */
const ramp = (i, n) => 172 + (n > 1 ? (i / (n - 1)) * 96 : 0);

function hbars(rows, { max = null, suffix = '', colour = true } = {}) {
  const top = max || Math.max(1, ...rows.map((r) => r.value));
  return h('div', { class: 'hbars' }, ...rows.map((r, i) => h('div', {
    class: `hbar${r.to ? ' is-link' : ''}`,
    onclick: r.to ? (e) => { e.stopPropagation(); go(r.to); } : null,
    title: r.to ? `${r.label} \u2014 click to open` : null,
  },
    h('span', { class: 'hbar-label', title: r.label }, r.label),
    h('span', { class: 'hbar-track' },
      h('span', {
        class: 'hbar-fill',
        style: `width:${Math.max(1.5, (r.value / top) * 100)}%;`
             + `--hue:${colour ? ramp(i, rows.length) : 172};--i:${i}`,
      })),
    h('span', { class: 'hbar-value' }, `${num(r.value)}${suffix}`))));
}

/** A small labelled figure for use inside a card. */
const miniStat = (label, value, note) =>
  h('div', { class: 'ministat' },
    h('div', { class: 'ministat-value' }, value),
    h('div', { class: 'ministat-label' }, label),
    note ? h('div', { class: 'ministat-note' }, note) : null);

/* Charts, drawn as inline SVG. No library: the UI has no build step, and a
   bar chart and a stacked bar are a few dozen lines each. Both scale to their
   container so they survive a narrow window without a resize observer. */

/** Hourly bars. `rows` need `label`, `value`, and optionally `alert`. */
/**
 * A column chart, built from elements rather than SVG.
 *
 * The SVG version stretched a 100-unit viewBox to the card width with
 * `preserveAspectRatio="none"`, which scales text non-uniformly: every axis
 * label was drawn horizontally squashed. Elements avoid that entirely, and let
 * each column carry its own value where there is room for one.
 *
 * `scale: 'log'` for a heavy tail. These distributions are power laws -- half
 * the coupling pairs sit in the first bucket -- so on a linear axis one bar
 * fills the card and the rest are two pixels tall and indistinguishable. A log
 * axis shows the shape, and says so: read as linear, it makes the tail look far
 * bigger than it is.
 */
function barChart(rows, { label = 'calls', scale = 'linear', height = 66,
                         values = null } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  const norm = scale === 'log'
    ? (v) => (v > 0 ? Math.log10(v + 1) / Math.log10(max + 1) : 0)
    : (v) => v / max;
  // Room, measured rather than assumed: a value like "149.8k" needs about
  // 42px, and at twelve columns in a card there are 23. Past that the numbers
  // were drawn and then clipped, which is worse than not drawing them -- the
  // full chart, one click away, has the room to show every one.
  // Room, measured rather than assumed: "149.8k" needs about 42px, and twelve
  // columns in a card leave 23. A caller with room says so rather than being
  // held to the card's limit -- the full view has 200px a column.
  const wide = values === null ? rows.length <= 8 : values;
  const dense = rows.length > 12;
  const fmt = values === true && rows.length <= 9 ? num : numTight;

  const cols = rows.map((r, i) => h('div', {
    class: `cbar${r.to ? ' is-link' : ''}`,
    title: r.to ? `${r.label}: ${num(r.value)} ${label} \u2014 click to open`
                : `${r.label}: ${num(r.value)} ${label}`,
    onclick: r.to ? (e) => { e.stopPropagation(); go(r.to); } : null,
    style: `--i:${i}`,
  },
    // Precision costs width: "142.9k" needs 38px and "143k" needs 26. Nine
    // columns or fewer have the room even at half width; more do not, at any
    // size this app draws.
    wide ? h('span', { class: 'cbar-value' }, fmt(r.value)) : null,
    h('span', { class: 'cbar-track' },
      h('span', {
        class: `cbar-fill${r.alert ? ' alert' : ''}`,
        style: `height:${Math.max(r.value ? 3 : 0, norm(r.value) * 100)}%;`
             + `--hue:${ramp(i, rows.length)}`,
      })),
    dense ? null : h('span', { class: 'cbar-label' }, r.label)));

  return h('div', { class: 'chart-wrap' },
    h('div', { class: 'chart-scale' },
      h('span', {}, `peak ${num(max)}`),
      scale === 'log' ? h('span', { class: 'badge muted', title:
        'Heights follow the logarithm of the count, so the small buckets stay '
        + 'visible. Do not read one bar as a multiple of another.' }, 'log') : null),
    h('div', { class: 'cbars', style: `--bar-h:${height}px` }, ...cols),
    // The ends of a dense chart belong to the axis, not to a 11px column that
    // clips them: "2026" was rendering as "026".
    dense ? h('div', { class: 'cbars-axis' },
      h('span', {}, rows[0].label),
      h('span', {}, rows[rows.length - 1].label)) : null);
}

/**
 * A dropdown you can type into.
 *
 * A native `select` cannot be searched, and the repository list is 164 long --
 * finding one meant scrolling past six accounts. This keeps the shape of a
 * select (a trigger showing the current value, a list below) and adds the one
 * thing missing.
 *
 * `items` are `{ value, label, group }`. It is a combobox in the ARIA sense,
 * so a keyboard reaches everything a mouse does: type to filter, arrows to
 * move, Enter to choose, Escape to close.
 */
function searchSelect(items, { selected = null, placeholder = 'Search…',
                               emptyLabel = null, onChange = null } = {}) {
  const all = emptyLabel
    ? [{ value: '', label: emptyLabel, group: null }, ...items]
    : items;
  let value = selected == null ? (emptyLabel ? '' : (all[0] && all[0].value)) : selected;
  const labelFor = (v) => (all.find((i) => String(i.value) === String(v)) || {}).label || '';

  const trigger = h('button', {
    class: 'picker-trigger', type: 'button',
    role: 'combobox', 'aria-expanded': 'false', 'aria-haspopup': 'listbox',
  }, h('span', { class: 'picker-value' }, labelFor(value)),
     h('span', { class: 'picker-caret' }, '\u25be'));

  const search = h('input', {
    class: 'picker-search input', type: 'search', placeholder,
    autocomplete: 'off', spellcheck: 'false',
  });
  const list = h('div', { class: 'picker-list', role: 'listbox' });
  const panel = h('div', { class: 'picker-panel', hidden: true }, search, list);
  const root = h('div', { class: 'picker' }, trigger, panel);

  let active = -1;          // index into the currently visible options
  let visible = [];

  const paint = () => {
    const term = search.value.trim().toLowerCase();
    // Match the group too: typing an account name should find its
    // repositories, which is how someone looks for one they half-remember.
    visible = all.filter((i) => !term
      || i.label.toLowerCase().includes(term)
      || (i.group || '').toLowerCase().includes(term));
    list.replaceChildren();
    if (!visible.length) {
      list.append(h('div', { class: 'picker-empty' }, 'Nothing matches that.'));
      return;
    }
    let group = Symbol('none');
    visible.forEach((item, i) => {
      if (item.group !== group) {
        group = item.group;
        if (group) list.append(h('div', { class: 'picker-group' }, group));
      }
      list.append(h('div', {
        class: `picker-option${i === active ? ' active' : ''}`
             + (String(item.value) === String(value) ? ' chosen' : ''),
        role: 'option', 'aria-selected': String(item.value) === String(value) ? 'true' : 'false',
        onclick: () => choose(item.value),
      }, item.label));
    });
  };

  const open = () => {
    panel.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    search.value = '';
    active = -1;
    paint();
    search.focus();
  };
  const close = () => {
    panel.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
  };
  function choose(next) {
    value = next;
    trigger.firstChild.textContent = labelFor(next);
    close();
    if (onChange) onChange(String(next));
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    if (panel.hidden) open(); else close();
  });
  search.addEventListener('input', () => { active = -1; paint(); });
  search.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = Math.max(0, Math.min(visible.length - 1,
                                    active + (e.key === 'ArrowDown' ? 1 : -1)));
      paint();
      list.querySelector('.picker-option.active')?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (visible[active]) choose(visible[active].value);
      else if (visible.length === 1) choose(visible[0].value);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
      trigger.focus();
    }
  });
  root.addEventListener('click', (e) => e.stopPropagation());
  document.addEventListener('click', close);

  // The call sites read `.value`, as they did from the select this replaces.
  Object.defineProperty(root, 'value', {
    get: () => value,
    set: (v) => { value = v; trigger.firstChild.textContent = labelFor(v); },
  });
  return root;
}

/**
 * A repository picker, grouped by the account that owns it.
 *
 * A repository name is unique only inside its account: two organisations can
 * each have a `guava`, and a flat list renders both as "guava" with no way to
 * tell which is which. The account is the group label, so the option carries
 * only the part that varies -- which is also how the rest of the application
 * addresses them, account then repository.
 */
function repoSelect(repos, { selected = null, allLabel = 'All repositories',
                             onChange = null } = {}) {
  const byAccount = new Map();
  for (const r of repos) {
    const owner = r.owner || (r.full_name || '').split('/')[0] || 'unknown';
    if (!byAccount.has(owner)) byAccount.set(owner, []);
    byAccount.get(owner).push(r);
  }
  // Accounts alphabetically, so a reader can find one; repositories within an
  // account in the order given, which is busiest first.
  const groups = [...byAccount.entries()].sort((a, b) => a[0].localeCompare(b[0]));

  return searchSelect(
    groups.flatMap(([owner, rows]) =>
      rows.map((r) => ({ value: r.id, label: r.name, group: owner }))),
    { selected, emptyLabel: allLabel, placeholder: 'Search repositories…', onChange },
  );
}

/** A labelled form control with an optional hint underneath. */
const field = (label, control, hint) =>
  h('div', { class: 'field-block' },
    h('label', { class: 'field-label' }, label),
    control,
    hint ? h('div', { class: 'field-hint' }, hint) : null);

/* "Account" reads as a login you sign in with, which is not what this is: it
   is a place repositories come from, and one of them may be a self-hosted git
   server with no accounts at all. The old path still resolves, because links
   to it exist. */
on('/accounts', () => { go('/sources'); return h('div'); });

on('/sources', async () => {
  const data = await api('/api/accounts');
  const rows = data.accounts || [];
  const wrap = h('div');

  wrap.append(pageHead('Sources',
    'Where repositories come from. Paste a URL from any git host \u2014 changes take effect on the next discovery run, so nothing needs redeploying.'));

  const enabled = rows.filter((r) => r.enabled);
  const discovered = rows.reduce((n, r) => n + Number(r.live_repo_count || 0), 0);
  wrap.append(h('div', { class: 'grid grid-stats' },
    statTile('Sources', num(rows.length), `${num(enabled.length)} enabled`),
    statTile('Repositories', num(discovered), 'across every source', () => go('/repos')),
    statTile('Never scanned', num(rows.filter((r) => !r.last_discovered_at).length), 'awaiting first discovery'),
    statTile('Failing', num(rows.filter((r) => r.last_discover_error).length), 'last discovery errored')));

  // ---- add by URL ---------------------------------------------------------
  /* One field, because a person adding something has the URL in their
     clipboard already. Decomposing it into a login, a kind, an allowlist and
     three toggles is asking them to do work the string contains. */
  const url = h('input', {
    class: 'input mono', id: 'source-url', autocomplete: 'off', spellcheck: 'false',
    placeholder: 'https://github.com/microsoft/vscode',
  });
  // Distinct from the confirm button the panel renders below it: both are
  // primary buttons inside the same form, and a selector that cannot tell
  // them apart is one a test -- or a keyboard user -- will get wrong.
  const submit = h('button', { class: 'btn primary', id: 'source-lookup' }, 'Look up');
  const panel = h('div', { class: 'resolve-panel', hidden: true });

  /* Most sources are public, so the token field is hidden until it is the
     answer to something: the reader asks for it, or a lookup came back
     not-found, which is what a private repository looks like from outside. */
  const token = h('input', {
    class: 'input mono', id: 'source-token', type: 'password',
    autocomplete: 'off', spellcheck: 'false',
    placeholder: 'ghp_… / glpat-… — only if the repository is private',
  });
  const tokenBox = h('div', { class: 'token-box', hidden: true },
    h('label', { class: 'field-label', for: 'source-token' }, 'Access token'),
    token,
    h('div', { class: 'field-hint' },
      'Kept for this source only, encrypted, and never shown again \u2014 just '
      + 'the first and last few characters so you can recognise it. Leave it '
      + 'empty to use whatever credential the deployment is configured with.'));
  const tokenToggle = h('button', { class: 'linkish', type: 'button' },
    'Private repository?');
  tokenToggle.onclick = () => {
    tokenBox.hidden = !tokenBox.hidden;
    if (!tokenBox.hidden) token.focus();
  };

  /* The picker. An owner is shown as a list to tick rather than added whole,
     because "add microsoft" means 8,296 repositories and almost never means
     what the person wanted. Forks and archived repositories are listed but
     start unticked: a fork's history is its parent's, so tracking both files
     every commit twice. */
  function picker(found) {
    // With no list there is nothing to tick, so the only offer left is the
    // whole owner -- which needs no list. Forcing the toggle on is the honest
    // rendering of that: the choice really has collapsed to one.
    const blind = !found.repos.length;
    // Every host returns a hundred at a time, so the rest arrive behind the
    // reader while they are already looking at the first hundred. `rows` is
    // the accumulator; `found.repos` is only ever page one.
    const rows = [...found.repos];
    let more = !!found.has_more;
    let loading = more;
    // Keyed on `key`, the path under the owner, not on `name`: GitLab groups
    // nest, so one owner can hold two projects called the same thing.
    const chosen = new Set(found.repos.filter((r) => r.suggested).map((r) => r.key));
    const everything = h('input', { type: 'checkbox', checked: blind, disabled: blind });
    const rowsBox = h('div', { class: 'pick-list' });
    const search = h('input', {
      class: 'input', placeholder: 'Filter\u2026', autocomplete: 'off',
    });
    const count = h('span', { class: 'card-sub' });
    const go = h('button', { class: 'btn primary' });

    const label = () => {
      const n = everything.checked ? found.total : chosen.size;
      count.textContent = everything.checked
        ? (found.total == null
            ? `every repository under ${found.owner}, now and in future`
            : `every repository under ${found.owner} \u2014 ${num(found.total)} now, and any added later`)
        : `${num(chosen.size)} of ${found.total == null ? num(rows.length) : num(found.total)}`
          + ` selected${loading ? ` \u00b7 loading ${num(rows.length)}\u2026` : ''}`;
      go.textContent = everything.checked
        ? `Track all of ${found.owner}`
        : `Track ${num(n)} ${n === 1 ? 'repository' : 'repositories'}`;
      go.disabled = !everything.checked && chosen.size === 0;
      rowsBox.classList.toggle('is-muted', everything.checked);
    };

    const paint = () => {
      const q = search.value.trim().toLowerCase();
      const shown = rows.filter((r) => !q || r.key.toLowerCase().includes(q));
      rowsBox.replaceChildren(...shown.map((r) => {
        const box = h('input', {
          type: 'checkbox', checked: chosen.has(r.key), disabled: r.already_tracked,
          onchange: () => {
            if (box.checked) chosen.add(r.key); else chosen.delete(r.key);
            label();
          },
        });
        return h('label', { class: `pick-row${r.already_tracked ? ' is-done' : ''}` },
          box,
          // The key, not the name: two projects called `gitlab-runner` in
          // different subgroups are indistinguishable by name alone.
          h('span', { class: 'pick-name mono', title: r.full_name }, r.key),
          h('span', { class: 'pick-meta' },
            r.language ? h('span', { class: 'badge muted' }, r.language) : null,
            r.stars ? h('span', {}, `\u2605 ${num(r.stars)}`) : null,
            r.is_fork ? h('span', { class: 'badge warn' }, 'fork') : null,
            r.is_archived ? h('span', { class: 'badge muted' }, 'archived') : null,
            r.is_private ? h('span', { class: 'badge info' }, 'private') : null,
            r.already_tracked ? h('span', { class: 'badge ok' }, 'tracked') : null));
      }));
      if (!shown.length) {
        rowsBox.append(h('div', { class: 'empty' },
          loading ? 'Still loading\u2026' : 'Nothing matches'));
      }
    };

    const note = h('div', { class: 'help', hidden: true });

    /* Keep asking for the next page until the host says there is no more.
       Sequential rather than parallel: the budget is per hour, and eighty-three
       requests fired at once is how a host decides you are a robot. A page that
       fails stops the walk and keeps what arrived -- a partial list somebody
       can act on beats an error where a list was. */
    const loadRest = async () => {
      let page = found.page || 1;
      while (more && !cancelled) {
        page += 1;
        let next;
        try {
          next = await apiSend('POST', '/api/accounts/resolve',
                               { url: found.url, token: lastToken, page });
        } catch (err) {
          more = false;
          loading = false;
          note.textContent = `Stopped after ${num(rows.length)} of `
            + `${found.total == null ? 'an unknown number' : num(found.total)}: `
            + String(err.message || err);
          note.hidden = false;
          paint();
          label();
          return;
        }
        for (const r of next.repos) {
          if (!rows.some((x) => x.key === r.key)) {
            rows.push(r);
            if (r.suggested) chosen.add(r.key);
          }
        }
        more = !!next.has_more;
        paint();
        label();
      }
      loading = false;
      label();
    };

    everything.onchange = label;
    search.oninput = paint;
    go.onclick = async () => {
      go.disabled = true;
      try {
        await apiSend('POST', '/api/accounts/from-url', {
          url: found.url,
          repos: everything.checked ? [] : [...chosen],
          token: lastToken,
        });
        toast(`${found.owner} added \u2014 run a discovery to pick up its history`);
        route();
      } catch (err) {
        toast(String(err.message || err), true);
        go.disabled = false;
      }
    };
    paint();
    label();
    if (more) loadRest();

    return h('div', {},
      h('div', { class: 'resolve-head' },
        h('div', {},
          h('div', { class: 'resolve-title mono' }, found.owner),
          h('div', { class: 'card-sub' },
            `${found.provider === 'git' ? 'git' : found.provider} \u00b7 ${found.host}`
            + (found.truncated
                ? ` \u00b7 the ${found.repos.length} most starred \u2014 there are more`
                : ''))),
        h('label', { class: `pick-all${blind ? ' is-only' : ''}` }, everything,
          h('span', {}, 'Track everything under this owner'))),
      found.listing_error
        ? h('div', { class: 'help' },
            h('strong', {}, `${found.listing_error} `),
            'You can still track the whole owner \u2014 that needs no listing. '
            + 'To take just one repository, paste its own URL instead: that costs '
            + 'a single request.')
        : null,
      note,
      found.repos.length > 8 ? h('div', { class: 'toolbar' }, search) : null,
      blind ? null : rowsBox,
      h('div', { class: 'form-actions' }, count, go));
  }

  /* A single repository needs no list: it is one row and one button. */
  function confirmRepo(found) {
    const r = found.repos[0];
    const go = h('button', { class: 'btn primary' },
      r.already_tracked ? 'Already tracked' : 'Track this repository');
    go.disabled = !!r.already_tracked;
    go.onclick = async () => {
      go.disabled = true;
      try {
        await apiSend('POST', '/api/accounts/from-url',
                      { url: found.url, token: lastToken });
        toast(`${r.full_name} added \u2014 run a discovery to pick up its history`);
        route();
      } catch (err) { toast(String(err.message || err), true); go.disabled = false; }
    };
    return h('div', {},
      h('div', { class: 'resolve-head' },
        h('div', {},
          h('div', { class: 'resolve-title mono' }, r.full_name),
          h('div', { class: 'card-sub' },
            r.description || `${found.provider} \u00b7 ${found.host}`)),
        h('div', { class: 'pick-meta' },
          r.language ? h('span', { class: 'badge muted' }, r.language) : null,
          r.stars ? h('span', {}, `\u2605 ${num(r.stars)}`) : null,
          r.is_fork ? h('span', { class: 'badge warn' }, 'fork') : null,
          r.is_archived ? h('span', { class: 'badge muted' }, 'archived') : null)),
      !found.has_api
        ? h('div', { class: 'help' },
            'No API client for this host, so there is no description or language '
            + 'to show. It clones, parses and scores exactly the same.')
        : null,
      h('div', { class: 'form-actions' }, go));
  }

  // The token that produced the panel now on screen, so confirming stores the
  // one that actually worked rather than whatever the field holds afterwards.
  let lastToken = '';

  /* A background page-walk outlives the panel that started it. Without this a
     second lookup leaves the first still appending rows into a list nobody is
     looking at, and still spending request budget on it. */
  let cancelled = false;

  const look = async () => {
    const value = url.value.trim();
    if (!value) { toast('Paste a repository or organisation URL', true); url.focus(); return; }
    submit.disabled = true;
    submit.textContent = 'Looking\u2026';
    panel.hidden = true;
    cancelled = true;             // stop whatever the last lookup started
    await Promise.resolve();
    cancelled = false;
    try {
      const found = await apiSend('POST', '/api/accounts/resolve',
                                  { url: value, token: token.value.trim() });
      lastToken = token.value.trim();
      panel.replaceChildren(
        found.tracks_everything
          ? h('div', { class: 'help' },
              `${found.owner} is already tracked in full, so everything under it `
              + 'is in scope already.')
          : found.kind === 'repo' ? confirmRepo(found) : picker(found));
      panel.hidden = false;
    } catch (err) {
      const msg = String(err.message || err);
      // "Nothing there" is what a private repository looks like from outside,
      // so this is the moment the token field stops being clutter.
      if (/is private|nothing at/i.test(msg) && tokenBox.hidden) {
        tokenBox.hidden = false;
        token.focus();
      }
      panel.replaceChildren(h('div', { class: 'help' }, msg));
      panel.hidden = false;
    } finally {
      submit.disabled = false;
      submit.textContent = 'Look up';
    }
  };
  submit.onclick = look;
  url.onkeydown = (e) => { if (e.key === 'Enter') look(); };

  /* The button shares the input's line, so the hint has to sit outside the
     field block: inside it, the block's bottom edge is the bottom of the hint
     text, and anything aligned to that edge lands below the input rather than
     beside it. */
  wrap.append(card('Add a source',
    h('div', { class: 'form' },
      h('div', { class: 'url-add' },
        h('label', { class: 'field-label', for: 'source-url' },
          'Repository or organisation URL'),
        h('div', { class: 'url-add-row' }, url, submit),
        h('div', { class: 'field-hint' },
          'GitHub, GitLab, Bitbucket, or any git host \u2014 paste the page you '
          + 'were looking at. A repository adds just that one; an organisation '
          + 'lets you pick. ', tokenToggle)),
      tokenBox,
      panel),
    'Nothing is added until you choose'));

  // ---- existing accounts --------------------------------------------------
  const setEnabled = async (row, value) => {
    try {
      await apiSend('PATCH', `/api/accounts/${row.id}`, { enabled: value });
      toast(`${row.login} ${value ? 'enabled' : 'disabled'}`);
      route();
    } catch (err) { toast(String(err.message || err), true); }
  };

  const clearToken = async (row) => {
    if (!window.confirm(
      `Remove the stored access token for ${row.login}?\n\n`
      + 'Its private repositories will stop updating unless the deployment-wide '
      + 'credential can see them.')) return;
    try {
      await apiSend('PUT', `/api/accounts/${row.id}/credential`, { token: '' });
      toast(`token removed from ${row.login}`);
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
      return h('span', { class: 'badge info', title: r.only_repos.join(', ') },
               `${r.only_repos.length} chosen`);
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
      { key: 'login', label: 'Source', render: (r) => h('span', {},
          h('strong', {}, r.login),
          h('span', { class: 'badge muted', style: 'margin-left:6px' }, r.kind)) },
      // The host earns a column now that a login is only unique within one:
      // an internal GitLab group commonly carries the company's GitHub name.
      { key: 'host', label: 'Host', render: (r) => h('span', { class: 'mono', style: 'font-size:11px' },
          r.host || 'github.com') },
      { key: 'live_repo_count', label: 'Repos', num: true,
        title: 'Repositories currently attributed to this account. Click to see them.',
        render: (r) => (Number(r.live_repo_count)
          ? h('a', { class: 'mono', href: `/sources/${r.id}`, 'data-nav': true,
                     onclick: (e) => e.stopPropagation() }, num(r.live_repo_count))
          : h('span', { class: 'muted-cell' }, '0')) },
      { key: 'filters', label: 'Filters', sortable: false, render: filterCell },
      { key: 'credential_hint', label: 'Token', sortable: false,
        title: 'A token stored for this source alone, used instead of the '
             + 'deployment-wide credential. Click to remove it.',
        render: (r) => (r.has_credential
          ? h('button', { class: 'badge ok clickable', title: 'Click to remove',
                          onclick: (e) => { e.stopPropagation(); clearToken(r); } },
              r.credential_hint || 'set')
          : h('span', { class: 'muted-cell' }, '\u2014')) },
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
         onRow: (r) => (Number(r.live_repo_count) ? go(`/sources/${r.id}`) : null),
         empty: 'Nothing tracked yet. Paste a URL above.' }),
    'A source owns repositories: open one to see just those. '
    + 'Removing a source keeps them and everything mined from them.'));

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

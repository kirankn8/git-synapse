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
  ['/insights/risk', 'Risk'],
  ['/insights/drift', 'Drift'],
  ['/accounts', 'Accounts'],
  ['/jobs', 'Jobs'],
  ['/measures', 'Measures'],
];

// Where the ranking measure orders something on screen, and where it does not.
const MEASURE_BAR = [
  ['/', true], ['/repos/4?tab=pairs', true],
  ['/repos', false], ['/insights/risk', false], ['/jobs', false],
];

const problems = [];
const ok = (label, detail) => console.log(`  ok   ${label.padEnd(36)} ${detail}`);
const bad = (label, detail) => { console.log(`  FAIL ${label.padEnd(36)} ${detail}`); problems.push(`${label}: ${detail}`); };

const browser = await puppeteer.launch({
  executablePath: '/usr/bin/chromium-browser',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1000 });

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

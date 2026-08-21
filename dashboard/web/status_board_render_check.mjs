// Prove dashboard/status-board.html draws, and that the substrate tile group
// (Task 14, RL-069) is really there with every chip in it reading a state from
// the board's own vocabulary -- not just that some page loaded. Fails on any
// console error.
//   dashboard/web/verify_status_board_renders.sh
import { chromium } from 'playwright';
const [page_url, shot] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1200 } });
const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', e => errors.push(String(e)));
await page.goto(page_url, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(500);

const must = async (sel, what) => {
  const n = await page.locator(sel).count();
  if (!n) throw new Error('missing: ' + what);
  return n;
};

// The board in general: some probed tiles exist at all.
const tileCount = await must('.tile', 'probed-state tiles');

// The substrate section specifically -- this is what Task 14 added, and what a
// verifier that only checked "a page loaded" would never notice was missing.
const headings = await page.locator('h2').allTextContents();
const substrateHeading = headings.find(h => h.includes('Part runtime substrate'));
if (!substrateHeading) throw new Error('missing: substrate section heading (off-diagram, RL-069)');

const substrateSection = page.locator('section', { has: page.locator('h2', { hasText: 'Part runtime substrate' }) });
const substrateTiles = substrateSection.locator('.tile');
const substrateTileCount = await substrateTiles.count();
if (substrateTileCount !== 6) {
  throw new Error(`expected 6 substrate tiles, found ${substrateTileCount}`);
}

// Every chip in the substrate group must carry a state from the known
// vocabulary -- a verifier that only checked presence would pass against a
// tile rendering an empty or made-up state string just as happily as a real one.
const VALID_STATES = new Set(['OK', 'NOT BUILT', 'FAILING', 'NOT MEASURED']);
const chips = await substrateTiles.evaluateAll(nodes => nodes.map(n => ({
  label: n.querySelector('.tile-label')?.textContent || null,
  state: n.querySelector('.tile-state')?.textContent || null,
  value: n.querySelector('.tile-value')?.textContent || null,
  proof: n.querySelector('.tile-proof')?.textContent || null,
})));
for (const chip of chips) {
  if (!chip.label || !chip.label.trim()) throw new Error(`substrate tile with no label: ${JSON.stringify(chip)}`);
  if (!VALID_STATES.has(chip.state)) throw new Error(`substrate tile "${chip.label}" has an invalid state: ${chip.state}`);
  if (!chip.proof || !chip.proof.trim()) throw new Error(`substrate tile "${chip.label}" carries no proof`);
}

await substrateSection.scrollIntoViewIfNeeded();
await substrateSection.screenshot({ path: shot, timeout: 15000 });

const textVisible = await page.evaluate(() => getComputedStyle(document.querySelector('h1')).fontFamily);
console.log(JSON.stringify({ tileCount, substrateHeading, substrateTileCount, chips, font: textVisible, errors }, null, 2));
await browser.close();
if (errors.length) { console.error('console errors:', errors); process.exit(1); }

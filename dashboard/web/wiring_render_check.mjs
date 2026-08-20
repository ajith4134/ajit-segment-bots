// Prove wiring-explorer.html draws and its three views work: matrix cell click,
// part focus ego graph, type view, search. Fails on any console error.
//   dashboard/web/verify_wiring_renders.sh
import { chromium } from 'playwright';
const [page_url, shot] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', e => errors.push(String(e)));
await page.goto(page_url); await page.waitForTimeout(800);
const must = async (sel, what) => { const n = await page.locator(sel).count(); if (!n) throw new Error('missing: ' + what); return n; };
const cells = await must('table.matrix td', 'matrix cells');
const verdict = await page.locator('#verdict').textContent();
// click an off-diagonal filled cell
await page.locator('table.matrix td:not(.diag)').filter({ hasText: /\d/ }).first().click();
await must('#matrix-detail .wire', 'cell detail wires');
// peer cells empty
const peerBreach = await page.locator('td[title*="BREACH"]').count();
// jump to a part
await page.locator('#matrix-detail [data-p]').first().click();
await must('#v-focus.on svg.ego .node.centre', 'focus centre node');
const wires = await page.locator('svg.ego path.w').count();
const centre = await page.locator('svg.ego .node.centre text').first().textContent();
// jump to a type
await page.locator('#focus .typebadge').first().click();
await must('#v-type.on .cols .part', 'type view parts');
// search
await page.fill('#q', 'bull-'); await page.waitForTimeout(100);
const hits = await page.locator('#results .part').count();
await page.locator('#results .part').first().click();
await page.screenshot({ path: shot, fullPage: false });
const textVisible = await page.evaluate(() => getComputedStyle(document.querySelector('h1')).fontFamily);
console.log(JSON.stringify({ cells, verdict, peerBreach, wires, centre, hits, font: textVisible, errors }));
await browser.close();
if (errors.length) { console.error('console errors:', errors); process.exit(1); }

// Verify the LIVE board renders and actually moves.
//
// Separate from render_check.mjs because the two pages differ in the one way that
// matters to a browser check: the live board polls forever, so `networkidle` never
// fires and waiting for it times out at 30s every time. A frozen snapshot is idle by
// construction; a live page is never idle, and that is the point of it.
//
// It also checks something the snapshot check cannot: that a rate appears. A rate
// needs two observed heartbeat tables, so a board that renders on the first poll and
// never updates would pass every DOM assertion while being useless. This waits for
// the second poll and asserts the live column filled in.
import { chromium } from 'playwright'

const url = process.argv[2]
const shot = process.argv[3]
const errors = []
const failed = []

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 1200 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('requestfailed', (r) => failed.push(`${r.url().slice(0, 90)} :: ${r.failure()?.errorText}`))

await page.goto(url, { waitUntil: 'domcontentloaded' })
// Waited for as long as the shape route can actually take, not as long as it
// takes when warm. `/api/board` measures 327 rungs off the filesystem, and on a
// cold cache with every part running it was timed at 70s on 2026-08-26 -- the
// same order as the trades route below, and the reason that one already waits
// 60s. At 20s this failed against a board that was merely still measuring, which
// reads exactly like a board that is broken.
await page.waitForSelector('.live-grid', { timeout: 120000 })

// The first poll can only ever say NOT MEASURED -- one table is not a rate. Wait for
// a second one so the thing being verified is the live column, not the empty state.
await page.waitForFunction(
  () => [...document.querySelectorAll('.rate-live')].length > 0,
  null,
  { timeout: 30000 },
).catch(() => {})

const seen = await page.evaluate(() => ({
  title: document.querySelector('h1')?.textContent || null,
  mode: document.querySelector('.badge')?.textContent?.trim() || null,
  views: [...document.querySelectorAll('.view')].map((v) => v.textContent.trim()),
  blockCards: document.querySelectorAll('.live-card').length,
  cardsWorking: document.querySelectorAll('.live-card-working').length,
  cardsOff: document.querySelectorAll('.live-card-off').length,
  liveRates: document.querySelectorAll('.rate-live').length,
  unmeasured: document.querySelectorAll('.unmeasured').length,
  stats: [...document.querySelectorAll('.live-stat')].map((s) =>
    `${s.querySelector('.live-stat-lab').textContent}=${s.querySelector('.live-stat-val').textContent}`),
  proof: document.querySelector('.live-proof')?.textContent?.slice(0, 120) || null,
  rootChildren: document.getElementById('root')?.children.length ?? 0,
}))

// Opening a block must reveal its parts, and opening a part must reveal its counters.
// That chain is the whole contract of this view: block -> part -> the number it keeps.
await page.locator('.live-head').first().click()
await page.waitForTimeout(250)
const partsShown = await page.locator('.part-row').count()
let countersShown = 0
if (await page.locator('.part-row-open').count()) {
  await page.locator('.part-row-open').first().click()
  await page.waitForTimeout(250)
  countersShown = await page.locator('.counter').count()
}

// Every view must draw. The live view is one of several, not a replacement for
// the others, and a tab that renders nothing is the failure this check exists to
// catch.
//
// Views are found by their label, never by position. They used to be clicked by
// index, and when a Settings view was added between Server load and How far
// built every later index shifted by one: the check went on clicking nth(3),
// landed on Settings, found no `.panel` there and reported "the build view drew
// nothing" -- against a build view that was drawing perfectly. A check that has
// silently stopped checking the thing it names is worse than no check, which is
// the whole reason this file exists.
const openView = async (label) => {
  const button = page.locator('.view', { hasText: label })
  if (await button.count() !== 1) {
    console.error(`RENDER FAILED: expected exactly one view button labelled ${JSON.stringify(label)}, found ${await button.count()}`)
    process.exit(1)
  }
  await button.click()
}

await openView('Trading')
// Waited for, not slept through. The trades payload reads the tape per held
// position and can take seconds on a cold cache; a fixed sleep caught the loading
// state and reported a render failure for a page that was working.
await page.waitForSelector('.trade-panel', { timeout: 60000 }).catch(() => {})
await page.waitForTimeout(500)
const tradePanels = await page.locator('.trade-panel').count()
const tradeRows = await page.locator('.trade-table tbody tr').count()
const tradeEmpty = await page.locator('.trade-empty').count()

await openView('Server load')
await page.waitForTimeout(4000)
const gauges = await page.locator('.gauge').count()
const cores = await page.locator('.core').count()
const costRows = await page.locator('.cost-row').count()
const gaugeHeadlines = await page.evaluate(() =>
  [...document.querySelectorAll('.gauge')].map((g) =>
    `${g.querySelector('.gauge-label').textContent}=${g.querySelector('.gauge-headline').textContent}`))

await openView('How far built')
await page.waitForTimeout(300)
const buildPanels = await page.locator('.panel').count()

await page.locator('.view').first().click()
await page.waitForTimeout(300)
await page.screenshot({ path: shot, fullPage: false })
await browser.close()

const result = {
  ...seen, partsShown, countersShown, buildPanels,
  tradePanels, tradeRows, tradeEmpty, gauges, cores, costRows, gaugeHeadlines,
  consoleErrors: errors, failedRequests: failed,
}
console.log(JSON.stringify(result, null, 2))

if (seen.rootChildren === 0) { console.error('RENDER FAILED: #root is empty'); process.exit(1) }
if (seen.blockCards === 0) { console.error('RENDER FAILED: no block cards drawn'); process.exit(1) }
if (partsShown === 0) { console.error('RENDER FAILED: opening a block showed no parts'); process.exit(1) }
if (countersShown === 0) { console.error('RENDER FAILED: opening a part showed no counters'); process.exit(1) }
if (buildPanels === 0) { console.error('RENDER FAILED: the build view drew nothing'); process.exit(1) }
if (tradePanels < 2) { console.error('RENDER FAILED: the trading view drew fewer than two panels'); process.exit(1) }
// Rows OR an explicit empty state -- a table with neither is a view that silently
// shows nothing, which is exactly what NOTHING YET exists to prevent.
if (tradeRows === 0 && tradeEmpty === 0) { console.error('RENDER FAILED: trading drew no rows and no empty state'); process.exit(1) }
if (gauges < 4) { console.error('RENDER FAILED: server load drew fewer than four gauges'); process.exit(1) }
if (cores === 0) { console.error('RENDER FAILED: server load drew no per-core bars'); process.exit(1) }
if (costRows === 0) { console.error('RENDER FAILED: server load drew no per-part costs'); process.exit(1) }
if (errors.length) { console.error('CONSOLE ERRORS PRESENT'); process.exit(1) }
if (seen.liveRates === 0) {
  // Not fatal on its own: a board watched while the spine is stopped correctly shows
  // no rates. It is reported loudly because it is also what a broken poll looks like.
  console.error('WARNING: no live rate rendered — either nothing is running, or /api/activity is not updating')
}

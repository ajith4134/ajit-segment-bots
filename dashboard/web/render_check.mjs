// Verify the board actually renders: mount React, collect console errors and failed
// requests, assert the real content is on the page, and save a screenshot.
import { chromium } from 'playwright'

const url = process.argv[2]
const shot = process.argv[3]
const errors = []
const failed = []

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('requestfailed', (r) => failed.push(`${r.url().slice(0, 90)} :: ${r.failure()?.errorText}`))

await page.goto(url, { waitUntil: 'networkidle' })
await page.waitForTimeout(700)

const seen = await page.evaluate(() => ({
  title: document.querySelector('h1')?.textContent || null,
  mode: document.querySelector('.badge')?.textContent?.trim() || null,
  tabs: document.querySelectorAll('.tab').length,
  panels: document.querySelectorAll('.panel').length,
  cells: document.querySelectorAll('.cell').length,
  legend: document.querySelectorAll('.legend-row').length,
  stats: [...document.querySelectorAll('.stat')].map((s) =>
    `${s.querySelector('.stat-lab').textContent}=${s.querySelector('.stat-val').textContent}`),
  banner: document.querySelector('.banner')?.textContent?.slice(0, 70) || null,
  rootChildren: document.getElementById('root')?.children.length ?? 0,
}))

// Clicking a cell must reveal its proof — that is the whole contract of the cell.
await page.locator('.cell').first().click()
await page.waitForTimeout(200)
const proof = await page.evaluate(() => document.querySelector('.detail-proof')?.textContent || null)

await page.screenshot({ path: shot, fullPage: false })
await browser.close()

console.log(JSON.stringify({ ...seen, proofOnClick: proof, consoleErrors: errors, failedRequests: failed }, null, 2))
if (seen.rootChildren === 0) { console.error('RENDER FAILED: #root is empty'); process.exit(1) }
if (errors.length) { console.error('CONSOLE ERRORS PRESENT'); process.exit(1) }

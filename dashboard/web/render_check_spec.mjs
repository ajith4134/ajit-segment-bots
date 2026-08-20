// Verify a rendered spec page actually draws, rather than assuming it did because
// the generator exited 0.
//
// The failure this exists to catch is specific and has bitten this project before:
// when fonts are missing, every glyph paints invisible while every DOM assertion
// still passes. So this measures painted pixels, not just the DOM.
import { chromium } from 'playwright'

const url = process.argv[2]
const shot = process.argv[3]
const errors = []
const failed = []

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('requestfailed', (r) => failed.push(`${r.url().slice(0, 80)} :: ${r.failure()?.errorText}`))

await page.goto(url, { waitUntil: 'load' })
await page.waitForTimeout(900)

const seen = await page.evaluate(() => {
  const h1 = document.querySelector('h1')
  const body = document.body
  const styles = getComputedStyle(body)
  const h1Box = h1?.getBoundingClientRect()

  // Tables must have real rows, not just a <table> tag.
  const tables = [...document.querySelectorAll('table')]

  return {
    title: document.title,
    h1: h1?.textContent?.trim() || null,
    h1Height: h1Box ? Math.round(h1Box.height) : 0,
    h1FontFamily: h1 ? getComputedStyle(h1).fontFamily.split(',')[0].replace(/"/g, '') : null,
    bodyColor: styles.color,
    bodyBackground: styles.backgroundColor,
    contentsLinks: document.querySelectorAll('.contents a').length,
    sections: document.querySelectorAll('h2[id]').length,
    tables: tables.length,
    tableRows: tables.reduce((n, t) => n + t.querySelectorAll('tbody tr').length, 0),
    tablesOverflowGuarded: document.querySelectorAll('.table-scroll').length,
    badges: document.querySelectorAll('.verdict').length,
    blockquotes: document.querySelectorAll('blockquote').length,
    codeBlocks: document.querySelectorAll('pre').length,
    stampFields: document.querySelectorAll('.stamp span').length,
    // A page that scrolls sideways is a layout bug.
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    documentHeight: Math.round(document.documentElement.scrollHeight),
    fontsLoaded: document.fonts.status,
    archivoLoaded: document.fonts.check('700 2rem Archivo'),
    serifLoaded: document.fonts.check('400 1rem "Source Serif 4"'),
    monoLoaded: document.fonts.check('400 1rem "IBM Plex Mono"'),
  }
})

// Measure that text is actually painted: crop the headline area and count how many
// pixels differ from the page background. Invisible text scores near zero here
// while passing every assertion above.
const strip = await page.screenshot({ clip: { x: 0, y: 0, width: 1400, height: 420 } })
const inkRatio = await page.evaluate(async (dataUrl) => {
  const img = new Image()
  img.src = dataUrl
  await img.decode()
  const canvas = document.createElement('canvas')
  canvas.width = img.width
  canvas.height = img.height
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  ctx.drawImage(img, 0, 0)
  const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
  const bg = [data[0], data[1], data[2]]
  let differing = 0
  for (let i = 0; i < data.length; i += 4) {
    if (Math.abs(data[i] - bg[0]) + Math.abs(data[i + 1] - bg[1]) + Math.abs(data[i + 2] - bg[2]) > 30) differing++
  }
  return +(differing / (data.length / 4)).toFixed(4)
}, `data:image/png;base64,${strip.toString('base64')}`)

// Both themes must be legible, not just the one the host happens to use.
const themes = {}
for (const theme of ['light', 'dark']) {
  await page.emulateMedia({ colorScheme: theme })
  await page.waitForTimeout(150)
  themes[theme] = await page.evaluate(() => ({
    color: getComputedStyle(document.body).color,
    background: getComputedStyle(document.body).backgroundColor,
  }))
}
await page.emulateMedia({ colorScheme: 'light' })

await page.screenshot({ path: shot, fullPage: false })
await browser.close()

const report = { ...seen, inkRatio, themes, consoleErrors: errors, failedRequests: failed }
console.log(JSON.stringify(report, null, 2))

const problems = []
if (!seen.h1 || seen.h1Height === 0) problems.push('headline missing or has no height')
if (seen.contentsLinks === 0) problems.push('contents list is empty')
if (seen.sections === 0) problems.push('no anchored sections')
if (seen.tables === 0 || seen.tableRows === 0) problems.push('tables did not render rows')
if (seen.tables !== seen.tablesOverflowGuarded) problems.push('a table is not inside an overflow guard')
if (seen.horizontalOverflow) problems.push('page scrolls horizontally')
if (seen.bodyColor === seen.bodyBackground) problems.push('body text colour equals its background')
if (inkRatio < 0.01) problems.push(`headline area is blank (ink ratio ${inkRatio}) - fonts probably invisible`)
for (const [theme, v] of Object.entries(themes)) {
  if (v.color === v.background) problems.push(`${theme} theme: text colour equals background`)
}
if (errors.length) problems.push(`console errors: ${errors.length}`)

if (problems.length) {
  console.error('RENDER CHECK FAILED:')
  for (const p of problems) console.error(`  - ${p}`)
  process.exit(1)
}
console.error('render check passed')

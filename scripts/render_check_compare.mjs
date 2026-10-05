#!/usr/bin/env node
/**
 * 「双通道对照」视图的本地渲染核验（一次性探针，不进验收包）。
 *
 * 只验证三件事，且全部是**前端静态渲染**，不需要后端：
 *   1. 对照数据能取到（demo/vision-channel-compare.json 是静态文件）；
 *   2. 两列都渲染出来，且各画布上真的画了框（读像素非空白）；
 *   3. cv 列的"0 条 = 适用范围问题"说明在页面上出现。
 *
 * 登录态：路由守卫只查 localStorage 里有没有未过期令牌（前端本地显隐，
 * 真正的权限裁决在后端）。这里注入一个 exp 很远的自签 JWT 仅为通过守卫
 * 渲染页面 —— 对照面板不发任何请求，所以不构成鉴权绕过。
 */
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const require = createRequire(import.meta.url)
const playwright = require(
  process.env.PLAYWRIGHT_CORE_PATH || 'playwright-core',
)
const CHROME = process.env.SEASIGHT_BROWSER_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const BASE = process.env.SEASIGHT_BASE_URL || 'http://127.0.0.1:5188'

// exp = 2099 年；payload/base64url 手工拼，只为过前端守卫
const b64u = (obj) =>
  Buffer.from(JSON.stringify(obj)).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const fakeToken = `${b64u({ alg: 'none' })}.${b64u({ sub: 'render-check', role: 'viewer', exp: 4102444800 })}.x`

const outDir = path.resolve('artifacts/metrics/render-check')
await mkdir(outDir, { recursive: true })

const browser = await playwright.chromium.launch({ headless: true, executablePath: CHROME })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

const errors = []
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(`console: ${m.text()}`)
})

await page.addInitScript(
  ([token]) => localStorage.setItem('seasight_token', token),
  [fakeToken],
)

await page.goto(`${BASE}/vision-compare?sample=fishing-gear&demo=1`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500) // 等两张 4K 图解码 + 画框

const summary = await page.evaluate(() => {
  const canvases = [...document.querySelectorAll('.vcompare__column-canvas')]
  const text = document.body.innerText
  return {
    url: location.href,
    title: document.title,
    appHtmlLen: document.getElementById('app')?.innerHTML.length ?? -1,
    bodyHead: text.replace(/\s+/g, ' ').slice(0, 300),
    modeButtons: [...document.querySelectorAll('.analyze__mode-buttons button')].map((b) => b.textContent.trim()),
    columns: canvases.length,
    canvasStats: canvases.map((c) => {
      const ctx = c.getContext('2d')
      const { data } = ctx.getImageData(0, 0, c.width, c.height)
      let nonBg = 0
      for (let i = 0; i < data.length; i += 4) if (data[i + 3] !== 0) nonBg++
      return { w: c.width, h: c.height, paintedRatio: +(nonBg / (c.width * c.height)).toFixed(3) }
    }),
    hasZeroNote: /适用范围/.test(text),
    hasScaleNote: /不可横向比较/.test(text),
    hasRegime: /连续帧/.test(text),
    hasCaveats: /合成数据口径|严禁外推/.test(text),
    hasCaptionNote: /双通道对照为定性/.test(text),
    navHasItem: /双通道对照/.test(
      [...document.querySelectorAll('a,nav,aside')].map((e) => e.innerText).join(' '),
    ),
    stillInLogin: !!document.querySelector('input[type=password]'),
  }
})

await page.screenshot({ path: path.join(outDir, 'compare-fishing-gear.png'), fullPage: true })

// 再看一个 0 检出的样例，确认左列的说明文案
await page.goto(`${BASE}/vision-compare?sample=polystyrene-cup&demo=1`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2200)
const note = await page.evaluate(() => {
  const els = [...document.querySelectorAll('.analyze__column-note')]
  return els.map((e) => e.innerText.replace(/\s+/g, ' ').slice(0, 80))
})
await page.screenshot({ path: path.join(outDir, 'compare-styrofoam-cup.png'), fullPage: true })

const report = { base: BASE, summary, zeroNote: note, errors }
await writeFile(path.join(outDir, 'render-check.json'), JSON.stringify(report, null, 2), 'utf-8')
console.log(JSON.stringify(report, null, 2))

await browser.close()
if (errors.length) process.exit(1)

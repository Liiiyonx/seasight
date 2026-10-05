// 截取「干净」的登录页（生产构建，无演示账号面板）—— 用于软著/答辩材料。
//
// 为什么重截而不是涂改旧图：
//   旧图是硬编码口令时代的产物。涂改需要精确定位 4 处坐标，
//   实测发现中文角色名有降部延伸到口令行、自动定位不可靠，
//   试了三版都会误盖或漏盖。直接用「面板不渲染」的生产构建重截，
//   从源头上不存在这个问题 —— 比事后打补丁可靠得多。
//
// 前置：
//   cd frontend && npx vite build --outDir dist-clean   # 不设 VITE_DEMO_ACCOUNTS
//   node ./node_modules/vite/bin/vite.js preview --outDir dist-clean --port 5197
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE || 'playwright-core',
)
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5197'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../artifacts/ui-responsive')

const JWT =
  'eyJhbGciOiJIUzI1NiJ9.' +
  Buffer.from(JSON.stringify({ exp: 4102444800 })).toString('base64url') + '.sig'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ headless: true, executablePath: CHROME })
const page = await (await browser.newContext({ viewport: { width: 1920, height: 1080 } })).newPage()
await page.addInitScript((t) => {
  localStorage.setItem('seasight_token', t)
  localStorage.setItem('seasight_user', JSON.stringify({ username: 'probe', role: 'admin' }))
  localStorage.setItem('seasight_theme', 'light')
}, JWT)
await page.route('**/api/v1/**', (r) =>
  r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))

await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)

const file = join(OUT, '1920x1080-login.png')
await page.screenshot({ path: file })

// 自证：面板不该存在
const demoPanel = await page.evaluate(() => ({
  panels: document.querySelectorAll('.login-demo').length,
  passwords: (document.body.innerText.match(/123456/g) || []).length,
}))
console.log('已保存', file)
console.log('演示账号面板数:', demoPanel.panels, '（应为 0）')
console.log('页面内明文口令数:', demoPanel.passwords, '（应为 0）')

await browser.close()
process.exit(demoPanel.panels === 0 && demoPanel.passwords === 0 ? 0 : 1)

// 用**真实后端数据**重截软著说明书需要的页面图。
//
// ## 为什么必须重截
// 旧截图是后端未启动时拍的 —— 满屏「服务暂不可用」「暂无数据」。
// 审查报告 P1-1 指出这是**最影响「软件已实现」说服力**的一项：
// 审查员看到满屏报错会怀疑系统没真正跑通。
//
// 而数据库里其实**数据很充足**（事件 43 / 工单 8 / 设备 10 / 用户 4），
// 所以问题不是缺数据，是截图时后端没起。这次接真后端重拍即可。
//
// ★ 与 shoot_clean_login.mjs 的区别：那个是**生产构建**（无演示账号面板，
//   用于脱敏）；这个是**演示构建**（注入账号，且连真实后端取数据）。
//
// 用法：
//   cd frontend && VITE_API_BASE=http://127.0.0.1:8001 \
//     VITE_DEMO_ACCOUNTS='[...]' npx vite build --outDir dist-shots
//   node ./node_modules/vite/bin/vite.js preview --outDir dist-shots --port 5196
//   node ../scripts/shoot_copyright_pages.mjs
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE || 'playwright-core',
)
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5196'
const API = process.env.SHOT_API || 'http://127.0.0.1:8001'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../artifacts/ui-responsive')

// 说明书第 5 章用到的页面。key 与 build_copyright_manual.py 里的
// `1920x1080-<key>.png` 对应。
const PAGES = [
  { key: 'dashboard', path: '/dashboard', root: '.dashboard' },
  { key: 'events', path: '/events', root: '.page' },
  { key: 'tasks', path: '/tasks', root: '.page' },
  { key: 'devices', path: '/devices', root: '.page' },
  { key: 'reports', path: '/reports', root: '.page' },
  { key: 'assistant', path: '/assistant', root: '.page' },
  { key: 'agents', path: '/agents', root: '.agents' },
  { key: 'knowledge', path: '/knowledge', root: '.knowledge' },
]

// admin 角色的真实 JWT 由登录接口换；这里用 demo 口令登录。
const CREDS = { username: process.env.SHOT_USER || 'admin',
                password: process.env.SHOT_PASS || 'admin123456' }

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ headless: true, executablePath: CHROME })
const ctx = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  locale: 'zh-CN',
})
const page = await ctx.newPage()

const errors = []
page.on('pageerror', (e) => errors.push(String(e.message).slice(0, 120)))

// ★ 用**真实登录**拿真JWT。之前用假 token 只会让页面走空态 ——
//   截图要证明的是「系统跑通了」，伪造 token 反而拍不到数据。
const loginRes = await page.request.post(`${API}/api/v1/auth/login`, {
  data: CREDS,
})
if (!loginRes.ok()) {
  console.error(`✗ 登录失败 ${loginRes.status()}：${await loginRes.text()}`)
  process.exit(1)
}
const loginBody = await loginRes.json()
const TOKEN = loginBody?.data?.access_token
if (!TOKEN) {
  console.error('✗ 未取到 access_token')
  process.exit(1)
}
console.log(`登录成功：${loginBody.data.username}（${loginBody.data.role}）`)

await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await page.evaluate(({ t, u, role }) => {
  localStorage.setItem('seasight_token', t)
  localStorage.setItem('seasight_user', JSON.stringify({ username: u, role }))
  localStorage.setItem('seasight_theme', 'light')
}, { t: TOKEN, u: loginBody.data.username, r: loginBody.data.role })

const report = []
for (const p of PAGES) {
  await page.goto(`${BASE}${p.path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2600)   // 等图表与列表渲染完
  // 让热力图/图表有稳定尺寸
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 400))))

  // 采样页面上的关键指标，用于自证"不是空态"
  const probe = await page.evaluate(() => {
    const txt = document.body.innerText
    return {
      chars: txt.replace(/\s+/g, '').length,
      emptyHints: (txt.match(/暂无|没有符合|加载失败|服务暂不可用|暂无可用/g) || []).length,
      zeros: (txt.match(/\b0\b/g) || []).length,
    }
  })
  const file = join(OUT, `1920x1080-${p.key}.png`)
  await page.screenshot({ path: file, fullPage: false })
  report.push({ page: p.key, ...probe })
  console.log(
    `  ${p.key.padEnd(10)} 字符 ${String(probe.chars).padStart(5)}  ` +
    `空态提示 ${probe.emptyHints}  零值 ${probe.zeros}`,
  )
}

await browser.close()

const bad = report.filter((r) => r.chars < 400 || r.emptyHints > 2)
console.log('')
console.log(`页面错误：${errors.length ? errors.join(' | ') : '无'}`)
if (bad.length) {
  console.log('✗ 以下页面疑似空态/报错：' + bad.map((b) => b.page).join(', '))
  process.exit(1)
}
console.log('✓ 全部页面均有实质内容')

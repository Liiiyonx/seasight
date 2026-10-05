// 机械臂控制台渲染核验：执行后端面板 + 可达性预检三态。
//
// 探针要验证的是「预检真的区分了可达/不可达」，所以给一个必然不可达的
// 地址（192.0.2.1 是 RFC 5737 保留的测试网段），断言它落到「未检测到」态。
// 再用 page.route 拦掉一个可返回的地址，断言它落到「可达」态 ——
// 只测失败分支会漏掉"永远显示失败"这种假通过。
import { createRequire } from 'node:module'
import { writeFileSync } from 'node:fs'

const require = createRequire(import.meta.url)
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE || 'playwright-core',
)
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
// 两个静态服务：dist-verify 未配置 VITE_ARM_CONSOLE_URL，dist-probe 注入了配置
const BASE_UNSET = 'http://127.0.0.1:5199'
const BASE_SET = 'http://127.0.0.1:5200'
const BASE_NX = 'http://127.0.0.1:5201'
// 遥测场景用 dev server：调试钩子 window.__oceanusStore 仅 DEV 挂载
const BASE_TELE = process.env.PROBE_DEV_URL || 'http://127.0.0.1:5174'

const FAKE_JWT =
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.' +
  Buffer.from(JSON.stringify({ exp: 4102444800 })).toString('base64url') +
  '.sig'

const report = []
const errors = []
const log = (ok, name, detail = '') => report.push({ ok, name, detail })

const browser = await chromium.launch({ headless: true, executablePath: CHROME })

try {
  // ===== 场景一：未配置 URL → 面板可见、按钮禁用 =====
  {
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
    const page = await ctx.newPage()
    page.on('pageerror', (e) => errors.push('[未配置] ' + e.message))
    await page.addInitScript((tok) => {
      localStorage.setItem('seasight_token', tok)
      localStorage.setItem('seasight_user', JSON.stringify({ username: 'p', role: 'admin' }))
    }, FAKE_JWT)
    // 拦截仿真会话接口，让页面进入仿真台视图
    await page.route('**/api/v1/**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))

    await page.goto(`${BASE_UNSET}/simulation/T-TEST-0001`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1200)
    // 切到「机械臂台」视图
    // 切到「仿真台」视图：按钮在 .sim-map__switch 里，role=tab
    await page.click('.sim-map__switch button[role="tab"]:nth-child(2)')
    await page.waitForTimeout(1500)

    const panel = await page.evaluate(() => {
      const root = document.querySelector('.arm-console')
      if (!root) return null
      return {
        drivers: Array.from(root.querySelectorAll('.arm-console__driver'))
          .map((d) => d.textContent.trim()),
        active: root.querySelector('.arm-console__driver.is-active')?.textContent.trim(),
        note: root.querySelector('.arm-console__backend-note')?.textContent.trim().slice(0, 60),
        tag: root.querySelector('.arm-console__tag')?.textContent.trim(),
        openDisabled: root.querySelector('.arm-console__open')?.disabled,
        svgCount: root.querySelectorAll('svg').length,
        hasUnicode: /[↗✓⧉↻◉■]/.test(root.textContent || ''),
      }
    })
    log(!!panel, '仿真台视图渲染出 arm-console', JSON.stringify(panel))
    log(!!panel && panel.drivers.length === 3,
      '执行后端面板列出三种驱动', panel ? panel.drivers.join(' / ') : '-')
    log(!!panel && !!panel.active, '当前驱动被高亮', panel ? panel.active : '-')
    log(!!panel && panel.openDisabled === true, '未配置 URL 时按钮禁用')
    log(!!panel && !panel.hasUnicode, '无Unicode 字形残留（已换SVG）')
    log(!!panel && panel.svgCount >= 3, '内联图标已渲染',
      panel ? panel.svgCount + ' 个' : '-')
    await page.screenshot({ path: 'artifacts/arm-console-unconfigured.png' })
    await ctx.close()
  }

  // ===== 场景二：配置了可达地址 → 预检应显示「可达」 =====
  {
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
    const page = await ctx.newPage()
    page.on('pageerror', (e) => errors.push('[可达] ' + e.message))
    await page.addInitScript((tok) => {
      localStorage.setItem('seasight_token', tok)
      localStorage.setItem('seasight_user', JSON.stringify({ username: 'p', role: 'admin' }))
    }, FAKE_JWT)
    await page.route('**/api/v1/**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))
    // 拦掉 NoMachine Web 地址并返回 200 —— 预检应判为可达
    await page.route('http://192.168.1.50:4080/**', (r) =>
      r.fulfill({ status: 200, contentType: 'text/html', body: '<html>ok</html>' }))

    await page.goto(`${BASE_SET}/simulation/T-TEST-0001`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1000)
    await page.click('.sim-map__switch button[role="tab"]:nth-child(2)')
    await page.waitForTimeout(2500)

    const reach = await page.evaluate(() => {
      const root = document.querySelector('.arm-console')
      return {
        tag: root?.querySelector('.arm-console__tag')?.textContent.trim(),
        tagClass: root?.querySelector('.arm-console__tag')?.className,
        hint: root?.querySelector('.arm-console__reach-hint')?.textContent.trim().slice(0, 80),
        mode: Array.from(root?.querySelectorAll('.arm-console__row') || [])
          .map((r) => r.textContent.trim())
          .find((t) => t.includes('接入方式')),
      }
    })
    log(!!reach && /可达/.test(reach.tag || ''),
      '预检命中：可返回的地址被判为「树莓派可达」', JSON.stringify(reach))
    log(!!reach && /direct|直连|NoMachine/.test(reach.mode || ''),
      '接入方式自动识别为浏览器直连', reach ? reach.mode : '-')
    await page.screenshot({ path: 'artifacts/arm-console-reachable.png' })
    await ctx.close()
  }
  // ===== 场景三：舵机遥测面板（真机证据链）=====
  {
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
    const page = await ctx.newPage()
    page.on('pageerror', (e) => errors.push('[telemetry] ' + e.message))
    await page.addInitScript((tok) => {
      localStorage.setItem('seasight_token', tok)
      localStorage.setItem('seasight_user', JSON.stringify({ username: 'p', role: 'admin' }))
    }, FAKE_JWT)
    await page.route('**/api/v1/**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))

    // ★ 遥测只走 WebSocket（realtime.js 的 robot_status 消息才进
    //   telemetryFeed），REST 拦截喂不进来 —— 上一版探针就是错在这里。
    //   直接调 store 的 pushTelemetry，走真实的 store → computed → 组件链路。
    await page.evaluate(() => {
      const btn = document.createElement('button')
      btn.id = 'seed-telemetry'
      btn.textContent = 'seed'
      btn.style.display = 'none'
      document.body.appendChild(btn)
    })

    await page.goto(`${BASE_TELE}/simulation/T-TEST-0001`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1000)
    await page.click('.sim-map__switch button[role="tab"]:nth-child(2)')
    await page.waitForTimeout(1500)

    // 注入一条带 servo_telemetry 的遥测（走 store 的公开入口）
    const seeded = await page.evaluate(async () => {
      const store = window.__oceanusStore || window.__seasightStore
      if (!store || typeof store.pushTelemetry !== 'function') {
        return { ok: false, why: 'store 未暴露到 window' }
      }
      store.pushTelemetry({
        robot_id: 'R-01', task_id: 'T-TEST-0001', battery: 76,
        servo_telemetry: {
          1: { vin: 11520, temp: 38, position: 620 },
          2: { vin: 11480, temp: 41, position: 585 },
        },
      })
      return { ok: true, size: store.telemetryFeed.length }
    })
    if (!seeded.ok) log(false, '遥测注入（store 需暴露到 window）', JSON.stringify(seeded))
    await page.waitForTimeout(1200)

    const tele = await page.evaluate(() => {
      const root = document.querySelector('.arm-console')
      const panel = root?.querySelector('.arm-console__servos')
      return {
        hasPanel: !!panel,
        rows: Array.from(root?.querySelectorAll('.arm-console__servo') || [])
          .map((r) => r.textContent.replace(/\s+/g, ' ').trim()),
        head: panel?.querySelector('.arm-console__servos-head')?.textContent.trim(),
      }
    })
    log(tele.hasPanel, '舵机遥测面板已渲染（接入真机后出现）', JSON.stringify(tele))
    log(tele.rows.length === 2, '两个舵机各一行', tele.rows.join(' | '))
    log(/11\.52V/.test(tele.rows.join(' ')), '电压已格式化为 V（11520mV → 11.52V）')
    log(/38°C/.test(tele.rows.join(' ')), '温度已显示')
    log(/620/.test(tele.rows.join(' ')), '位置已显示')
    await page.screenshot({ path: 'artifacts/arm-console-telemetry.png' })
    await ctx.close()
  }

  // ===== 场景四：nx:// 协议档（实测选定的档位）=====
  {
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
    const page = await ctx.newPage()
    page.on('pageerror', (e) => errors.push('[nx] ' + e.message))
    await page.addInitScript((tok) => {
      localStorage.setItem('seasight_token', tok)
      localStorage.setItem('seasight_user', JSON.stringify({ username: 'p', role: 'admin' }))
    }, FAKE_JWT)
    await page.route('**/api/v1/**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))

    await page.goto(`${BASE_NX}/simulation/T-TEST-0001`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1000)
    await page.click('.sim-map__switch button[role="tab"]:nth-child(2)')
    await page.waitForTimeout(2500)

    const nx = await page.evaluate(() => {
      const root = document.querySelector('.arm-console')
      return {
        tag: root?.querySelector('.arm-console__tag')?.textContent.trim(),
        hint: root?.querySelector('.arm-console__reach-hint')?.textContent.trim().slice(0, 90),
        mode: Array.from(root?.querySelectorAll('.arm-console__row') || [])
          .map((r) => r.textContent.trim())
          .find((t) => t.includes('接入方式')),
        target: Array.from(root?.querySelectorAll('.arm-console__row') || [])
          .map((r) => r.textContent.trim())
          .find((t) => t.includes('跳转目标')),
        openEnabled: !root?.querySelector('.arm-console__open')?.disabled,
      }
    })
    log(!!nx && nx.openEnabled, 'nx 档：按钮可用', JSON.stringify(nx))
    log(!!nx && /nx/.test(nx.tag || ''), 'nx 档：状态标签识别为协议模式', nx ? nx.tag : '-')
    log(!!nx && String(nx.target || '').indexOf('nx://') >= 0, 'nx 档：跳转目标显示正确', nx ? nx.target : '-')
    log(!!nx && /协议唤起/.test(nx.mode || ''), 'nx 档：接入方式识别为协议唤起', nx ? nx.mode : '-')
    log(!!nx && /NoMachine/.test(nx.hint || ''),
      'nx 档：提示含"未装客户端"的排障指引', nx ? nx.hint : '-')
    await page.screenshot({ path: 'artifacts/arm-console-nx.png' })
    await ctx.close()
  }
} finally {
  await browser.close()
}

writeFileSync('artifacts/arm-console-probe.json', JSON.stringify({ report, errors }, null, 2))
let fail = 0
for (const r of report) {
  if (!r.ok) fail += 1
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}`)
  if (r.detail) console.log(`        ${r.detail}`)
}
console.log(`\n${report.length - fail}/${report.length} 通过`)
console.log('页面错误:', errors.length ? errors.join('\n') : '无')
process.exit(fail ? 1 : 0)

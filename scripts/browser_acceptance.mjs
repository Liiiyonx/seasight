#!/usr/bin/env node
/**
 * Oceanus WP-17 集成浏览器验收（E1）
 * =====================================================================
 * 把原先「仅 Agent 页面 + API 拦截构造状态」的浏览器回归升级为
 * 「真实登录 + 主业务链路」的集成验收包：
 *
 *   A. 真实登录链路：未登录跳转、admin / viewer 走真实登录页与真实
 *      POST /api/v1/auth/login（禁止 localStorage 伪造登录），登录后
 *      保存 storageState 供同一角色上下文的业务页面复用；
 *   B. 真实业务页面读取：/dashboard /events /tasks /devices /reports
 *      /agents 六页 —— 页面根/稳定业务标识可见、关键 API 等待并检查
 *      2xx、空列表必须渲染空态而非崩溃、pageerror / 严重 console
 *      error 即失败、全页截图 + 原始 JSON 记录；
 *   C. 真实 CSV 导出：工单 /tasks/export 与日报 /reports/export 两条
 *      链路（HTTP 2xx、Content-Type 含 text/csv、文件非空、UTF-8 BOM、
 *      稳定表头），保存文件并记录字节数与 SHA256；
 *   D. viewer 权限：界面不暴露写操作 + 直接写接口真实返回 403
 *      （不修改业务数据，权限依赖在 handler 之前拦截）；
 *   E. MQTT 降级：真实 GET /health（不拦截），dependencies 含
 *      redis/mqtt/database；当前无 broker 时真实断言 mqtt 非 ok、
 *      status 非 ok；同时真实打开 /dashboard 确认只读业务读取仍可用；
 *   F. 保留原有 3 个 Agent 交互场景（page.route 构造「活跃运行 /
 *      waiting_approval」状态，逐项标注 mocked: true 与原因）。
 *
 * 证据等级：E1 —— 本机代码 + 真实后端服务的集成验收；不代表真实用户
 * 试点、真实设备、真实海域或真实网络压测。
 *
 * 运行前提：
 *   1. 前端 dev server  http://127.0.0.1:5174（/api 代理到后端）
 *      与后端          http://127.0.0.1:8001  在线；
 *   2. 全局 playwright-core，经 PLAYWRIGHT_CORE_PATH 指向其入口；
 *   3. 系统 Chrome（SEASIGHT_BROWSER_PATH，默认
 *      C:\Program Files\Google\Chrome\Application\chrome.exe）。
 *      本脚本不下载 Playwright 自带浏览器。
 *
 * 退出码：全部场景通过 = 0；任一场景失败 / 预检失败 = 1。
 */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { access, mkdir, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const PLAYWRIGHT_CORE_PATH = process.env.PLAYWRIGHT_CORE_PATH || 'playwright-core'

let playwright
try {
  playwright = require(PLAYWRIGHT_CORE_PATH)
} catch (err) {
  console.error('[预检] 无法加载 playwright-core，缺依赖，验收中止。')
  console.error(`   PLAYWRIGHT_CORE_PATH = ${PLAYWRIGHT_CORE_PATH}`)
  console.error('   本机已确认可用的全局 playwright-core：')
  console.error("     <全局 playwright-core 安装路径>")
  console.error('   修复命令（任选其一，然后重跑本脚本）：')
  console.error("     $env:PLAYWRIGHT_CORE_PATH='<全局 playwright-core 安装路径>'")
  console.error('     # 或 npm install -g @playwright/mcp （提供 playwright-core 的全局包）')
  console.error('   不要下载 Playwright 自带浏览器；本脚本使用系统 Chrome。')
  process.exit(1)
}
const { chromium } = playwright

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ARTIFACT_DIR = path.join(ROOT, 'artifacts', 'browser-acceptance')
const FRONTEND = process.env.SEASIGHT_FRONTEND_URL || 'http://127.0.0.1:5174'
const BACKEND = process.env.SEASIGHT_BACKEND_URL || 'http://127.0.0.1:8001'
const BROWSER_PATH =
  process.env.SEASIGHT_BROWSER_PATH ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'

const ADMIN = { username: 'admin', password: 'admin123456' }
const VIEWER = { username: 'viewer', password: 'viewer123456' }

const ADMIN_STATE_PATH = path.join(ARTIFACT_DIR, 'admin-storage.json')
const VIEWER_STATE_PATH = path.join(ARTIFACT_DIR, 'viewer-storage.json')

const results = []
const observedApis = new Set()
let browser

// ======================================================================
// 通用工具
// ======================================================================
function envelope(data) {
  return { code: 0, message: 'ok', data, trace_id: 'browser-acceptance' }
}

function baseRun(overrides = {}) {
  return {
    run_id: 'run_browser_active',
    status: 'executing',
    trigger_type: 'manual',
    objective: '浏览器验收：模拟活跃运行',
    policy_version: 'policy-browser-1',
    started_at: '2026-09-19T01:00:00Z',
    created_at: '2026-09-19T01:00:00Z',
    finished_at: null,
    termination_reason: null,
    error_code: null,
    trace_id: 'trace-browser-active',
    idempotent_replay: false,
    pending_approval_ids: [],
    task_id: null,
    task_action: null,
    ...overrides,
  }
}

function runtimeStatus(overrides = {}) {
  return {
    state: 'running',
    active_runs: 1,
    total_runs: 1,
    pending_approvals: 0,
    tools_registered: 1,
    model_available: false,
    policy_version: 'policy-browser-1',
    uptime_ms: 120000,
    ...overrides,
  }
}

/** 监听一个页面：收集 API 日志（含 JSON body）、pageerror、console error。 */
function watchPage(page) {
  const apiLog = []
  const pageErrors = []
  const consoleErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })
  page.on('response', async (res) => {
    let url
    try {
      url = new URL(res.url())
    } catch {
      return
    }
    if (!url.pathname.startsWith('/api/v1/')) return
    const entry = { method: res.request().method(), path: url.pathname, status: res.status() }
    observedApis.add(url.pathname)
    const ct = res.headers()['content-type'] || ''
    if (ct.includes('application/json')) {
      try {
        entry.body = await res.json()
      } catch {
        /* 只保留状态 */
      }
    }
    apiLog.push(entry)
  })
  return { apiLog, pageErrors, consoleErrors }
}

/** 环境性资源噪音（外部 CDN / favicon / 中断请求）不算应用运行时错误。 */
function isBenignConsoleError(text) {
  return (
    /Failed to load resource/.test(text) ||
    /net::ERR_/.test(text) ||
    /favicon/i.test(text) ||
    /The request was aborted/.test(text) ||
    /ERR_ABORTED/.test(text)
  )
}

function seriousConsoleErrors(consoleErrors) {
  return consoleErrors.filter((t) => !isBenignConsoleError(t))
}

/** pageerror 或严重 console error 出现即失败（要求 9 的“真实运行时错误”）。 */
function assertNoRuntimeErrors(pageErrors, consoleErrors) {
  const serious = seriousConsoleErrors(consoleErrors)
  assert.deepEqual(pageErrors, [], `页面存在运行时错误: ${pageErrors.join(' | ')}`)
  assert.deepEqual(serious, [], `页面存在 console 错误: ${serious.join(' | ')}`)
}

async function screenshot(page, name) {
  const file = path.join(ARTIFACT_DIR, name)
  await page.screenshot({ path: file, fullPage: true })
  return file
}

/** 先挂等待再导航（避免竞态），等待每个关键 API 至少一次响应并返回其 HTTP 状态。 */
async function gotoAndWaitKeyApis(page, route, keyPaths, { timeout = 25000 } = {}) {
  const pending = keyPaths.map((p) =>
    page
      .waitForResponse((res) => {
        try {
          return new URL(res.url()).pathname === p
        } catch {
          return false
        }
      }, { timeout })
      .then((r) => [p, r.status()])
      .catch(() => [p, null]),
  )
  await page.goto(`${FRONTEND}${route}`, { waitUntil: 'domcontentloaded' })
  const statuses = Object.fromEntries(await Promise.all(pending))
  const missing = Object.entries(statuses)
    .filter(([, s]) => s === null)
    .map(([p]) => p)
  if (missing.length) throw new Error(`关键 API 未在 ${timeout}ms 内返回: ${missing.join(', ')}`)
  return statuses
}

/** 从 ApiResponse 信封里取列表长度（data 数组 / data.items / 裸数组）。 */
function listLen(body) {
  if (!body) return null
  const d = body.data
  if (Array.isArray(d)) return d.length
  if (d && Array.isArray(d.items)) return d.items.length
  if (Array.isArray(body)) return body.length
  return null
}

/**
 * 空态校验：对应列表为空时页面必须渲染空态元素；非空时必须渲染内容元素。
 * 用 count()>=1 而非 isVisible()，避免同页多个空态元素触发 strict 模式。
 */
async function checkEmptyState(page, apiEntry, emptySelector, itemSelector) {
  const n = listLen(apiEntry?.body)
  if (n === null) return { checked: false, note: '响应无可数列表字段，跳过空态断言' }
  if (n === 0) {
    await page.locator(emptySelector).first().waitFor({ state: 'visible', timeout: 10000 })
    return { checked: true, list_empty: true, empty_state_visible: true }
  }
  await page.locator(itemSelector).first().waitFor({ state: 'visible', timeout: 10000 })
  return { checked: true, list_empty: false, content_visible: true }
}

async function runCase(name, { mocked = false, mockReason = '' } = {}, fn) {
  const startedAt = Date.now()
  try {
    const evidence = await fn()
    const item = {
      name,
      passed: true,
      elapsed_ms: Date.now() - startedAt,
      mocked,
      ...(mockReason ? { mock_reason: mockReason } : {}),
      evidence,
    }
    results.push(item)
    console.log(`[PASS] ${name}`)
    return item
  } catch (error) {
    const item = {
      name,
      passed: false,
      elapsed_ms: Date.now() - startedAt,
      mocked,
      ...(mockReason ? { mock_reason: mockReason } : {}),
      error: error.stack || String(error),
    }
    results.push(item)
    console.error(`[FAIL] ${name}\n${item.error}`)
    return item
  }
}

async function newContext(browserRef, opts = {}) {
  return browserRef.newContext({ viewport: { width: 1440, height: 1000 }, ...opts })
}

/**
 * 真实登录：走真实登录页表单提交（真实 POST /api/v1/auth/login），
 * 不允许 localStorage 注入令牌。登录后等待跳转 /dashboard 并确认页面渲染。
 */
async function loginViaUi(page, { username, password }) {
  const loginResponses = []
  page.on('response', (res) => {
    try {
      if (new URL(res.url()).pathname === '/api/v1/auth/login') loginResponses.push(res)
    } catch {
      /* ignore */
    }
  })

  await page.goto(`${FRONTEND}/login`, { waitUntil: 'domcontentloaded' })
  // 确认登录表单可见（要求 2：不得只检查 URL）
  await page.locator('#username').waitFor({ state: 'visible', timeout: 15000 })
  await page.locator('#password').waitFor({ state: 'visible' })
  await page.locator('.login__btn').waitFor({ state: 'visible' })

  await page.fill('#username', username)
  await page.fill('#password', password)
  await page.click('.login__btn')

  // 等待真实登录请求完成并跳转 /dashboard
  await page.waitForURL((url) => url.pathname === '/dashboard', { timeout: 20000 })
  const loginRes = loginResponses[loginResponses.length - 1]
  assert(loginRes, '未观察到真实 POST /api/v1/auth/login 请求')
  assert.equal(loginRes.status(), 200, `登录接口 HTTP ${loginRes.status()}`)
  const body = await loginRes.json()
  assert.equal(body.code, 0, `登录业务失败: ${body.message}`)
  assert.equal(body.data.role, username === 'admin' ? 'admin' : 'viewer', '登录返回角色不符')
  await page.locator('.dashboard').waitFor({ state: 'visible', timeout: 20000 })
  return { http_status: loginRes.status(), code: body.code, role: body.data.role }
}

// ======================================================================
// Agent 场景用：受控 API 拦截（要求 4：仅 Agent 状态构造允许拦截）
// ======================================================================
async function mockAgentApis(page, { runs, detail, steps, approvals, onCancel }) {
  await page.route('**/api/v1/agents/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname
    const method = request.method()

    if (method === 'GET' && pathname.endsWith('/agents/runtime/status')) {
      await route.fulfill({ json: envelope(runtimeStatus({ active_runs: runs.length })) })
      return
    }
    if (method === 'GET' && pathname.endsWith('/agents/runs')) {
      await route.fulfill({
        json: envelope({
          items: runs,
          meta: { total: runs.length, page: 1, page_size: 20 },
        }),
      })
      return
    }
    if (method === 'GET' && pathname.endsWith('/agents/runs/run_browser_active/steps')) {
      await route.fulfill({ json: envelope(steps) })
      return
    }
    if (method === 'GET' && pathname.endsWith('/agents/runs/run_browser_active')) {
      await route.fulfill({ json: envelope(detail) })
      return
    }
    if (method === 'GET' && pathname.endsWith('/agents/approvals')) {
      await route.fulfill({ json: envelope(approvals) })
      return
    }
    if (method === 'GET' && pathname.endsWith('/agents/tools')) {
      await route.fulfill({ json: envelope([]) })
      return
    }
    if (method === 'GET' && pathname.endsWith('/agents/evals/latest')) {
      await route.fulfill({
        json: { code: 6008, message: '评测产物不存在', data: null, trace_id: null },
      })
      return
    }
    if (method === 'POST' && pathname.endsWith('/agents/runs/run_browser_active/cancel')) {
      await onCancel()
      await route.fulfill({
        json: envelope({ run_id: 'run_browser_active', status: 'cancelled' }),
      })
      return
    }
    await route.continue()
  })

  await page.route('**/api/v1/ai/agents', async (route) => {
    await route.fulfill({ json: envelope([]) })
  })
}

// ======================================================================
// 主流程
// ======================================================================
async function acceptance() {
  await mkdir(ARTIFACT_DIR, { recursive: true })

  // 浏览器预检
  try {
    await access(BROWSER_PATH)
  } catch {
    console.error(`[预检] 系统 Chrome 不存在: ${BROWSER_PATH}`)
    console.error('  请设置 SEASIGHT_BROWSER_PATH 指向可用的 Chrome/Edge。')
    process.exitCode = 1
    return
  }
  browser = await chromium.launch({ headless: true, executablePath: BROWSER_PATH })

  // ---------------------------------------------------------------
  // 0. 环境预检：前端 / 后端可达（不拦截、不伪造）
  // ---------------------------------------------------------------
  const preflight = await runCase('环境预检：前端与后端可达', {}, async () => {
    const probe = await browser.newContext()
    const fe = await probe.request.get(`${FRONTEND}/`, { timeout: 15000 })
    const be = await probe.request.get(`${BACKEND}/health`, { timeout: 15000 })
    const feOk = fe.ok()
    const beOk = be.ok()
    await probe.close()
    assert.equal(feOk, true, `前端 ${FRONTEND} 不可达 (HTTP ${fe.status()})`)
    assert.equal(beOk, true, `后端 ${BACKEND} 不可达 (HTTP ${be.status()})`)
    return { frontend_http: fe.status(), backend_health_http: be.status() }
  })
  if (!preflight.passed) {
    console.error('环境预检失败：不停止已有服务；本包报告 blocker 并退出（不改脚本掩盖）。')
    return
  }

  // ---------------------------------------------------------------
  // 1. 未登录访问 /dashboard 必须跳转 /login（且登录表单可见）
  // ---------------------------------------------------------------
  await runCase('未登录访问 /dashboard 跳转 /login 且登录表单可见', {}, async () => {
    const context = await newContext(browser)
    const { page } = await newPage(context)
    await page.goto(`${FRONTEND}/dashboard`, { waitUntil: 'domcontentloaded' })
    await page.waitForURL((url) => url.pathname === '/login', { timeout: 15000 })
    // 不得只检查 URL：必须确认登录表单可见
    await page.locator('#username').waitFor({ state: 'visible', timeout: 15000 })
    await page.locator('#password').waitFor({ state: 'visible' })
    await page.locator('.login__card').waitFor({ state: 'visible' })
    const screenshotFile = await screenshot(page, '00-unauthenticated-dashboard-redirect.png')
    await context.close()
    return {
      started_route: '/dashboard',
      final_pathname: new URL(page.url()).pathname,
      login_form_visible: true,
      screenshot: screenshotFile,
    }
  })

  // ---------------------------------------------------------------
  // 2. admin 真实登录 + 保存 storageState
  // ---------------------------------------------------------------
  let adminLoggedIn = false
  await runCase('admin 真实登录（登录页表单 + 真实 POST /auth/login）', {}, async () => {
    const context = await newContext(browser)
    const { page, pageErrors, consoleErrors } = await newPage(context)
    const login = await loginViaUi(page, ADMIN)
    await screenshot(page, '01-admin-login-dashboard.png')
    await context.storageState({ path: ADMIN_STATE_PATH })
    assertNoRuntimeErrors(pageErrors, consoleErrors)
    await context.close()
    adminLoggedIn = true
    return {
      login,
      storage_state_saved: ADMIN_STATE_PATH,
      redirect_to: '/dashboard',
    }
  })

  // ---------------------------------------------------------------
  // 3~8. 真实访问六页（同一 admin 上下文，复用 storageState）
  // ---------------------------------------------------------------
  const PAGES = [
    {
      route: '/dashboard',
      title: '监测大屏',
      root: '.dashboard',
      keyApis: ['/api/v1/stats/dashboard', '/api/v1/devices', '/api/v1/robots', '/api/v1/events'],
      empties: [
        { path: '/api/v1/devices', emptySelector: '.dashboard .empty', itemSelector: '.dashboard__stats' },
        { path: '/api/v1/robots', emptySelector: '.dashboard .empty', itemSelector: '.dashboard__stats' },
      ],
      screenshot: '02-dashboard.png',
    },
    {
      route: '/events',
      title: '事件中心',
      root: '.page',
      keyApis: ['/api/v1/events'],
      empties: [
        { path: '/api/v1/events', emptySelector: '.page .empty', itemSelector: '.data-table' },
      ],
      screenshot: '03-events.png',
    },
    {
      route: '/tasks',
      title: '工单看板',
      root: '.page',
      keyApis: ['/api/v1/tasks'],
      empties: [
        { path: '/api/v1/tasks', emptySelector: '.kanban__empty', itemSelector: '.kanban__col' },
      ],
      screenshot: '04-tasks.png',
    },
    {
      route: '/devices',
      title: '设备态势',
      root: '.page',
      keyApis: ['/api/v1/devices'],
      empties: [
        { path: '/api/v1/devices', emptySelector: '.dev-list .empty', itemSelector: '.dev-item' },
      ],
      screenshot: '05-devices.png',
    },
    {
      route: '/reports',
      title: '治理报表',
      root: '.page',
      keyApis: ['/api/v1/reports/daily', '/api/v1/reports/summary'],
      empties: [
        { path: '/api/v1/reports/daily', emptySelector: '.table-panel .empty', itemSelector: '.data-table' },
      ],
      screenshot: '06-reports.png',
    },
    {
      route: '/agents',
      title: '智能体',
      root: '.agents',
      keyApis: [
        '/api/v1/agents/runtime/status',
        '/api/v1/agents/runs',
        '/api/v1/agents/approvals',
        '/api/v1/agents/tools',
        '/api/v1/agents/evals/latest',
        '/api/v1/ai/agents',
      ],
      empties: [
        { path: '/api/v1/agents/runs', emptySelector: '.agents .empty', itemSelector: '.agents__runs-tools' },
      ],
      screenshot: '07-agents.png',
    },
  ]

  for (const cfg of PAGES) {
    await runCase(`真实访问 ${cfg.route}（${cfg.title}）`, {}, async () => {
      assert.ok(adminLoggedIn, 'admin 未登录，跳过业务页访问')
      const context = await newContext(browser, { storageState: ADMIN_STATE_PATH })
      const { page, apiLog, pageErrors, consoleErrors } = await newPage(context)
      const keyApiStatus = await gotoAndWaitKeyApis(page, cfg.route, cfg.keyApis)
      // 稳定业务标识：路由根节点 + 文档标题
      await page.locator(cfg.root).first().waitFor({ state: 'visible', timeout: 15000 })
      const pageTitle = await page.title()
      assert.match(pageTitle, new RegExp(cfg.title), `页面标题应为「${cfg.title}·探海灵眸 Oceanus」`)
      // 关键 API 无 4xx/5xx
      const bad = Object.entries(keyApiStatus).filter(([, s]) => s < 200 || s >= 300)
      assert.deepEqual(bad, [], `关键 API 出现非 2xx: ${JSON.stringify(keyApiStatus)}`)
      // 空态/内容态校验
      const emptyChecks = []
      for (const ec of cfg.empties) {
        const entry = apiLog.find((e) => e.path === ec.path)
        emptyChecks.push(await checkEmptyState(page, entry, ec.emptySelector, ec.itemSelector))
      }
      assertNoRuntimeErrors(pageErrors, consoleErrors)
      const shot = await screenshot(page, cfg.screenshot)
      const evidence = {
        route: cfg.route,
        page_title: pageTitle,
        root_visible: true,
        key_apis: keyApiStatus,
        empty_checks: emptyChecks,
        api_log: apiLog.map(({ method, path, status }) => ({ method, path, status })),
        page_errors: pageErrors,
        console_errors: consoleErrors,
        screenshot: shot,
      }
      await context.close()
      return evidence
    })
  }

  // ---------------------------------------------------------------
  // 9. 真实 CSV 导出（工单 + 报表）
  // ---------------------------------------------------------------
  await runCase('真实 CSV 导出：工单与日报（BOM/表头/字节数/SHA256）', {}, async () => {
    assert.ok(adminLoggedIn, 'admin 未登录，跳过 CSV 导出')
    const context = await newContext(browser, { storageState: ADMIN_STATE_PATH })
    const { page } = await newPage(context)
    await page.goto(`${FRONTEND}/dashboard`, { waitUntil: 'domcontentloaded' })
    const token = await page.evaluate(() => localStorage.getItem('seasight_token'))
    assert.ok(token, '未能从已登录上下文读取 seasight_token')

    async function exportCsv(endpoint, saveName, stableHeader) {
      const resp = await context.request.get(`${FRONTEND}${endpoint}`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 30000,
      })
      assert.equal(resp.ok(), true, `${endpoint} HTTP ${resp.status()}`)
      const ct = (resp.headers()['content-type'] || '').toLowerCase()
      assert.match(ct, /text\/csv/, `${endpoint} Content-Type 应为 text/csv，实际: ${ct}`)
      const buf = await resp.body()
      assert.ok(buf.length > 0, `${endpoint} 导出的 CSV 为空`)
      assert.equal(buf[0], 0xef, `${endpoint} 缺少 UTF-8 BOM`)
      assert.equal(buf[1], 0xbb, `${endpoint} 缺少 UTF-8 BOM`)
      assert.equal(buf[2], 0xbf, `${endpoint} 缺少 UTF-8 BOM`)
      const text = buf.toString('utf8')
      assert.ok(text.includes(stableHeader), `${endpoint} CSV 缺少稳定表头「${stableHeader}」`)
      const file = path.join(ARTIFACT_DIR, saveName)
      await writeFile(file, buf)
      return {
        endpoint,
        http_status: resp.status(),
        content_type: ct,
        bytes: buf.length,
        sha256: createHash('sha256').update(buf).digest('hex'),
        utf8_bom: true,
        stable_header: stableHeader,
        saved_as: file,
      }
    }

    const tasks = await exportCsv('/api/v1/tasks/export', 'tasks-export.csv', '任务编号')
    const reports = await exportCsv('/api/v1/reports/export', 'reports-export.csv', '统计日期')
    await context.close()
    return { tasks, reports }
  })

  // ---------------------------------------------------------------
  // 10. viewer 权限：界面不暴露写操作 + 直接写接口真实 403
  // ---------------------------------------------------------------
  await runCase('viewer 真实登录：界面不暴露写操作且写接口真实 403', {}, async () => {
    const context = await newContext(browser)
    const { page, pageErrors, consoleErrors } = await newPage(context)
    const login = await loginViaUi(page, VIEWER)
    await context.storageState({ path: VIEWER_STATE_PATH })

    // /tasks 页：viewer 看不到「人工建单 / 触发补派」写按钮
    await gotoAndWaitKeyApis(page, '/tasks', ['/api/v1/tasks'])
    assert.equal(
      await page.getByRole('button', { name: '人工建单' }).count(),
      0,
      'viewer 不应看到「人工建单」写按钮',
    )
    assert.equal(
      await page.getByRole('button', { name: '触发补派' }).count(),
      0,
      'viewer 不应看到「触发补派」写按钮',
    )
    const screenshotFile = await screenshot(page, '08-viewer-readonly.png')

    // 直接写接口真实请求：require_operator 依赖在 handler 之前拦截 → 403，
    // 不会修改任何业务数据（任务号特意用不存在的探针 ID）。
    const token = await page.evaluate(() => localStorage.getItem('seasight_token'))
    const writeResp = await context.request.patch(
      `${FRONTEND}/api/v1/tasks/tsk_wp17_viewer_403_probe`,
      {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          status: 'collecting',
          robot_id: 'RBT-001',
          collected_weight: 0,
          review_result: 'pending',
          remark: 'wp17 viewer 403 probe',
        },
      },
    )
    assert.equal(writeResp.status(), 403, `viewer 写接口应返回 403，实际 ${writeResp.status()}`)
    assertNoRuntimeErrors(pageErrors, consoleErrors)
    await context.close()
    return {
      login,
      tasks_write_buttons_hidden: { manual_create: true, dispatch_pending: true },
      write_probe: {
        endpoint: '/api/v1/tasks/tsk_wp17_viewer_403_probe',
        method: 'PATCH',
        http_status: writeResp.status(),
        note: '权限依赖在 handler 之前拦截，未修改业务数据',
      },
      screenshot: screenshotFile,
    }
  })

  // ---------------------------------------------------------------
  // 11. MQTT 降级但只读平台仍可用（真实 /health，不拦截）
  // ---------------------------------------------------------------
  await runCase('MQTT 降级时只读平台仍可用（真实 /health）', {}, async () => {
    const context = await newContext(browser)
    const healthResp = await context.request.get(`${BACKEND}/health`, { timeout: 15000 })
    assert.equal(healthResp.ok(), true, `/health HTTP ${healthResp.status()}`)
    const health = await healthResp.json()
    const deps = health.dependencies || {}
    for (const k of ['redis', 'mqtt', 'database']) {
      assert.ok(k in deps, `/health.dependencies 缺少 ${k}`)
    }

    let degraded
    if (deps.mqtt !== 'ok') {
      // 当前无 broker：真实断言 mqtt 非 ok、status 非 ok（健康检查不得撒谎）
      assert.notEqual(
        health.status,
        'ok',
        `mqtt=${deps.mqtt} 但 status=${health.status} —— 健康检查在撒谎`,
      )
      degraded = {
        probe: 'mqtt 真实断开：status 必须非 ok',
        mqtt: deps.mqtt,
        status: health.status,
      }
    } else {
      degraded = {
        probe: '当前环境 broker 在线，降级断言不适用（如实记录，不伪造降级）',
        mqtt: 'ok',
        status: health.status,
      }
    }

    // 真实打开 /dashboard，确认只读业务读取仍可用
    assert.ok(adminLoggedIn, 'admin 未登录，跳过降级态 dashboard 读取')
    const dashContext = await newContext(browser, { storageState: ADMIN_STATE_PATH })
    const { page, apiLog, pageErrors, consoleErrors } = await newPage(dashContext)
    const keyApiStatus = await gotoAndWaitKeyApis(page, '/dashboard', [
      '/api/v1/stats/dashboard',
      '/api/v1/devices',
      '/api/v1/robots',
      '/api/v1/events',
    ])
    await page.locator('.dashboard').waitFor({ state: 'visible', timeout: 15000 })
    const bad = Object.entries(keyApiStatus).filter(([, s]) => s < 200 || s >= 300)
    assert.deepEqual(bad, [], `降级态 dashboard 关键 API 出现非 2xx: ${JSON.stringify(keyApiStatus)}`)
    assertNoRuntimeErrors(pageErrors, consoleErrors)
    const screenshotFile = await screenshot(page, '09-mqtt-degraded-dashboard.png')
    const evidence = {
      health: {
        status: health.status,
        app: health.app,
        dependencies: deps,
        ack_recovery: health.ack_recovery ?? null,
      },
      degraded,
      dashboard_key_apis: keyApiStatus,
      dashboard_api_log: apiLog.map(({ method, path, status }) => ({ method, path, status })),
      conclusion: 'MQTT 真实断开时只读能力可用；这不等于真实设备或网络压测。',
      screenshot: screenshotFile,
    }
    await context.close()
    await dashContext.close()
    return evidence
  })

  // ---------------------------------------------------------------
  // 12~14. 保留原有 3 个 Agent 交互场景（仅此处允许 page.route 拦截）
  // ---------------------------------------------------------------
  const AGENT_MOCK_REASON =
    'page.route 拦截 /api/v1/agents/** 构造「活跃运行 / waiting_approval」等当前数据库难以稳定生成的状态（要求 4 允许的受控拦截）'

  await runCase('活跃运行二次确认后才取消', { mocked: true, mockReason: AGENT_MOCK_REASON }, async () => {
    const context = await newContext(browser, { storageState: ADMIN_STATE_PATH })
    const { page, pageErrors, consoleErrors } = await newPage(context)
    const activeRun = baseRun()
    let cancelCalls = 0
    await mockAgentApis(page, {
      runs: [activeRun],
      detail: activeRun,
      steps: [],
      approvals: [],
      onCancel: async () => {
        cancelCalls += 1
      },
    })

    await page.goto(`${FRONTEND}/agents?run=${activeRun.run_id}`, { waitUntil: 'domcontentloaded' })
    const cancelButton = page.getByRole('button', { name: '取消运行' })
    await cancelButton.waitFor({ state: 'visible' })
    await cancelButton.click()
    assert.equal(cancelCalls, 0, '第一次点击不应发取消请求')
    await page.getByRole('button', { name: '确认取消' }).waitFor({ state: 'visible' })
    await page.getByRole('button', { name: '确认取消' }).click()
    for (let i = 0; i < 50 && cancelCalls === 0; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 20))
    }
    assert.equal(cancelCalls, 1, '第二次点击应且只应发一次取消请求')

    const screenshotFile = await screenshot(page, '10-active-run-cancel-confirm.png')
    assertNoRuntimeErrors(pageErrors, consoleErrors)
    await context.close()
    return { cancel_calls: cancelCalls, confirmation_visible: true, screenshot: screenshotFile }
  })

  await runCase('viewer 审批按钮禁用并显示权限提示', { mocked: true, mockReason: AGENT_MOCK_REASON }, async () => {
    const context = await newContext(browser, { storageState: VIEWER_STATE_PATH })
    const { page, pageErrors, consoleErrors } = await newPage(context)
    const pending = [
      {
        approval_id: 'apr_browser_pending',
        run_id: 'run_browser_active',
        requested_action: 'create_work_order',
        requested_by: 'operator',
        requested_at: '2026-09-19T01:00:01Z',
        risk_level: 'high',
        decision: null,
      },
    ]
    const run = baseRun({ status: 'waiting_approval', pending_approval_ids: ['apr_browser_pending'] })
    await mockAgentApis(page, {
      runs: [run],
      detail: run,
      steps: [],
      approvals: pending,
      onCancel: async () => {},
    })

    await page.goto(`${FRONTEND}/agents`, { waitUntil: 'domcontentloaded' })
    const approve = page.getByRole('button', { name: '同意' })
    const reject = page.getByRole('button', { name: '拒绝' })
    await approve.waitFor({ state: 'visible' })
    assert.equal(await approve.isDisabled(), true, 'viewer 的同意按钮应禁用')
    assert.equal(await reject.isDisabled(), true, 'viewer 的拒绝按钮应禁用')
    await page.getByText('无审批权限（需 admin/approver）').waitFor({ state: 'visible' })

    const screenshotFile = await screenshot(page, '11-viewer-approval-disabled.png')
    assertNoRuntimeErrors(pageErrors, consoleErrors)
    await context.close()
    return { approve_disabled: true, reject_disabled: true, permission_hint: true, screenshot: screenshotFile }
  })

  await runCase('运行详情关闭后刷新仍保持关闭', { mocked: true, mockReason: AGENT_MOCK_REASON }, async () => {
    const context = await newContext(browser, { storageState: ADMIN_STATE_PATH })
    const { page, pageErrors, consoleErrors } = await newPage(context)
    const finishedRun = baseRun({
      run_id: 'run_browser_active',
      status: 'succeeded',
      finished_at: '2026-09-19T01:01:00Z',
      termination_reason: 'completed',
    })
    await mockAgentApis(page, {
      runs: [finishedRun],
      detail: finishedRun,
      steps: [],
      approvals: [],
      onCancel: async () => {},
    })

    await page.goto(`${FRONTEND}/agents?run=${finishedRun.run_id}`, { waitUntil: 'domcontentloaded' })
    await page.locator('.run-detail').waitFor({ state: 'visible' })
    await page.getByRole('button', { name: '关闭' }).click()
    await page.locator('.run-detail').waitFor({ state: 'detached' })
    assert.equal(new URL(page.url()).searchParams.has('run'), false, '关闭后 URL 应移除 run 参数')

    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.agents').waitFor({ state: 'visible' })
    assert.equal(await page.locator('.run-detail').count(), 0, '刷新后详情应保持关闭')
    assert.equal(new URL(page.url()).searchParams.has('run'), false, '刷新后仍不应带 run 参数')

    const screenshotFile = await screenshot(page, '12-detail-closed-after-reload.png')
    assertNoRuntimeErrors(pageErrors, consoleErrors)
    await context.close()
    return { closed_before_reload: true, closed_after_reload: true, screenshot: screenshotFile }
  })
}

async function newPage(context) {
  const page = await context.newPage()
  const watch = watchPage(page)
  return { page, ...watch }
}

// ======================================================================
// 报告与退出码
// ======================================================================
let reportWritten = false

function writeReport() {
  const passed = results.filter((r) => r.passed).length
  const failed = results.filter((r) => !r.passed)
  const report = {
    generated_at: new Date().toISOString(),
    frontend_url: FRONTEND,
    backend_url: BACKEND,
    evidence_level: 'E1',
    scope_note:
      '真实登录（登录页表单 + 真实 POST /api/v1/auth/login）、真实业务读取链路、真实 CSV 导出、真实 /health 降级探针；' +
      '仅 Agent 页交互场景使用 page.route 拦截以稳定构造「活跃运行 / waiting_approval」状态（逐项标注 mocked: true 与原因）。' +
      '本包是 E1 集成验收：代表本机代码 + 真实后端服务的联调结果，不代表真实用户试点、真实设备、真实海域或真实网络压测。',
    results,
    summary: { total: results.length, passed, failed: failed.length },
    observation: { api_paths: [...observedApis].sort() },
  }
  return `${JSON.stringify(report, null, 2)}\n`
}

try {
  await acceptance()
} catch (error) {
  console.error(`[ABORT] 验收流程异常终止：\n${error.stack || error}`)
  results.push({
    name: '验收流程异常',
    passed: false,
    elapsed_ms: 0,
    mocked: false,
    error: String(error),
  })
} finally {
  if (browser) await browser.close()
  const json = writeReport()
  await mkdir(ARTIFACT_DIR, { recursive: true })
  const reportFile = path.join(ARTIFACT_DIR, 'latest.json')
  await writeFile(reportFile, json, 'utf8')
  reportWritten = true
  console.log(`\n报告已写入: ${reportFile}`)
}

if (!reportWritten) {
  console.error('报告未写入（异常路径），退出码置 1')
  process.exitCode = 1
} else {
  const failed = results.filter((item) => !item.passed)
  if (failed.length) {
    console.error(`浏览器验收失败 ${failed.length} 项：`)
    for (const f of failed) console.error(`  - ${f.name}`)
    console.error(`报告：${path.join(ARTIFACT_DIR, 'latest.json')}`)
    process.exitCode = 1
  } else {
    console.log(`浏览器验收通过 ${results.length} 项，报告：${path.join(ARTIFACT_DIR, 'latest.json')}`)
    process.exitCode = 0
  }
}

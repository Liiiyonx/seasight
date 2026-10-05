#!/usr/bin/env node
/**
 * 工单执行仿真的真实浏览器验收。
 *
 * 覆盖：工单卡入口、自动启动、4x、暂停、单步、继续、完成闭环、
 * 历史轨迹只读、viewer 控制禁用，以及桌面/移动端布局溢出检查。
 */
import assert from 'node:assert/strict'
import { access, mkdir, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const playwright = require(
  process.env.PLAYWRIGHT_CORE_PATH ||
    'playwright-core',
)
const { chromium } = playwright

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ARTIFACT_DIR = path.join(ROOT, 'artifacts', 'browser-acceptance')
const FRONTEND = process.env.SEASIGHT_FRONTEND_URL || 'http://127.0.0.1:5174'
const BACKEND = process.env.SEASIGHT_BACKEND_URL || 'http://127.0.0.1:8001'
const BROWSER_PATH =
  process.env.SEASIGHT_BROWSER_PATH ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const TASK_ID = process.env.SIMULATION_TASK_ID || 'task_ad8008810797'
const HISTORY_TASK_ID = process.env.SIMULATION_HISTORY_TASK_ID || 'tsk_demo_0001'
const ADMIN = { username: 'admin', password: 'admin123456' }
const VIEWER = { username: 'viewer', password: 'viewer123456' }

const results = []
const runtimeErrors = []
let browser

function record(name, details) {
  results.push({ name, passed: true, ...details })
  console.log(`[PASS] ${name}`)
}

function asyncPage(page) {
  page.on('pageerror', (error) => runtimeErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error' && !/favicon|Failed to load resource|net::ERR_/.test(message.text())) {
      runtimeErrors.push(message.text())
    }
  })
}

async function login(page, credentials) {
  await page.goto(`${FRONTEND}/login`, { waitUntil: 'domcontentloaded' })
  await page.locator('#username').fill(credentials.username)
  await page.locator('#password').fill(credentials.password)
  await page.locator('.login__btn').click()
  await page.waitForURL((url) => url.pathname === '/dashboard', { timeout: 20000 })
  await page.locator('.dashboard').waitFor({ state: 'visible', timeout: 20000 })
}

async function waitJsonResponse(page, method, suffix, action) {
  const responsePromise = page.waitForResponse(
    (response) => {
      try {
        const url = new URL(response.url())
        return response.request().method() === method && url.pathname.endsWith(suffix)
      } catch {
        return false
      }
    },
    { timeout: 20000 },
  )
  await action()
  const response = await responsePromise
  const body = await response.json()
  assert.equal(response.status(), 200, `${method} ${suffix} HTTP ${response.status()}`)
  assert.equal(body.code, 0, `${method} ${suffix} 返回业务错误: ${body.message}`)
  return body.data
}

async function fetchSnapshot(context, taskId) {
  const token = await context.pages()[0].evaluate(() =>
    localStorage.getItem('seasight_token'),
  )
  assert.ok(token, '未读取到登录令牌')
  const response = await context.request.get(
    `${BACKEND}/api/v1/simulations/${taskId}`,
    { headers: { Authorization: `Bearer ${token}` } },
  )
  assert.equal(response.status(), 200, `仿真状态 HTTP ${response.status()}`)
  const body = await response.json()
  assert.equal(body.code, 0)
  return body.data
}

async function layoutAudit(page, label) {
  const audit = await page.evaluate(() => {
    const elements = Array.from(
      document.querySelectorAll(
        '.sim-page, .sim-head, .sim-layout, .sim-map, .sim-side, .sim-controls',
      ),
    )
    const horizontalOverflow = elements
      .filter((element) => element.scrollWidth > element.clientWidth + 1)
      .map((element) => ({
        selector: element.className,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }))
    const outsideViewport = Array.from(
      document.querySelectorAll('.sim-controls button, .sim-head__action'),
    )
      .map((element) => ({
        text: element.textContent.trim(),
        rect: element.getBoundingClientRect().toJSON(),
      }))
      .filter(
        ({ rect }) =>
          rect.left < -1 ||
          rect.right > window.innerWidth + 1 ||
          rect.top < -1 ||
          rect.bottom > document.documentElement.scrollHeight + 1,
      )
    const clippedText = Array.from(
      document.querySelectorAll(
        '.sim-head__title h1, .sim-head__facts span, .sim-controls__hint, .sim-kv strong, .sim-stream__body span',
      ),
    )
      .filter(
        (element) =>
          element.scrollWidth > element.clientWidth + 1 &&
          getComputedStyle(element).overflow !== 'hidden',
      )
      .map((element) => ({
        text: element.textContent.trim(),
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }))
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      document_width: document.documentElement.scrollWidth,
      horizontalOverflow,
      outsideViewport,
      clippedText,
    }
  })
  assert.equal(
    audit.document_width <= audit.viewport.width + 1,
    true,
    `${label} 页面出现横向滚动: ${JSON.stringify(audit)}`,
  )
  assert.deepEqual(
    audit.horizontalOverflow,
    [],
    `${label} 容器横向溢出: ${JSON.stringify(audit.horizontalOverflow)}`,
  )
  assert.deepEqual(
    audit.outsideViewport,
    [],
    `${label} 控件超出视口: ${JSON.stringify(audit.outsideViewport)}`,
  )
  return audit
}

async function screenshot(page, name) {
  const file = path.join(ARTIFACT_DIR, name)
  await page.screenshot({ path: file, fullPage: true })
  return file
}

async function waitSimulationState(page, stateText) {
  await page
    .locator('.sim-state')
    .filter({ hasText: stateText })
    .waitFor({ state: 'visible', timeout: 25000 })
}

async function run() {
  await mkdir(ARTIFACT_DIR, { recursive: true })
  await access(BROWSER_PATH)
  browser = await chromium.launch({ headless: true, executablePath: BROWSER_PATH })

  const adminContext = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  })
  const adminPage = await adminContext.newPage()
  asyncPage(adminPage)
  await login(adminPage, ADMIN)

  await adminPage.goto(`${FRONTEND}/tasks`, { waitUntil: 'domcontentloaded' })
  const card = adminPage.locator('.tcard').filter({ hasText: TASK_ID })
  await card.waitFor({ state: 'visible', timeout: 20000 })
  const start = await waitJsonResponse(
    adminPage,
    'POST',
    `/api/v1/simulations/${TASK_ID}/start`,
    () => card.getByRole('button', { name: '仿真' }).click(),
  )
  assert.equal(start.state, 'running')
  assert.equal(start.phase, 'ack')
  await adminPage.waitForURL((url) => url.pathname === `/simulation/${TASK_ID}`)
  await waitSimulationState(adminPage, '运行中')
  await adminPage.locator('.sim-stream__row').first().waitFor({ timeout: 20000 })

  const speed = await waitJsonResponse(
    adminPage,
    'POST',
    `/api/v1/simulations/${TASK_ID}/speed`,
    () => adminPage.getByRole('button', { name: '4x' }).click(),
  )
  assert.equal(speed.speed, 4)

  const paused = await waitJsonResponse(
    adminPage,
    'POST',
    `/api/v1/simulations/${TASK_ID}/pause`,
    () => adminPage.getByRole('button', { name: '暂停' }).click(),
  )
  assert.equal(paused.state, 'paused')
  await waitSimulationState(adminPage, '已暂停')
  const beforeStep = await fetchSnapshot(adminContext, TASK_ID)

  const stepped = await waitJsonResponse(
    adminPage,
    'POST',
    `/api/v1/simulations/${TASK_ID}/step`,
    () => adminPage.getByRole('button', { name: '单步' }).click(),
  )
  assert.equal(stepped.state, 'paused')
  await adminPage.waitForTimeout(1200)
  const afterStep = await fetchSnapshot(adminContext, TASK_ID)
  assert.ok(
    afterStep.logs.length > beforeStep.logs.length,
    `单步后日志未推进: ${beforeStep.logs.length} -> ${afterStep.logs.length}`,
  )

  const resumed = await waitJsonResponse(
    adminPage,
    'POST',
    `/api/v1/simulations/${TASK_ID}/resume`,
    () => adminPage.getByRole('button', { name: '继续' }).click(),
  )
  assert.equal(resumed.state, 'running')
  await waitSimulationState(adminPage, '运行中')
  await waitSimulationState(adminPage, '已完成')
  const finalSnapshot = await fetchSnapshot(adminContext, TASK_ID)
  assert.equal(finalSnapshot.state, 'done')
  assert.equal(finalSnapshot.phase, 'done')
  assert.equal(finalSnapshot.progress, 1)
  assert.ok(finalSnapshot.route.length > 2)
  assert.ok(finalSnapshot.battery < start.battery)
  assert.ok(finalSnapshot.logs.length >= 20)

  await adminPage.getByRole('tab', { name: /运行日志/ }).click()
  await adminPage.getByText('工单闭环完成，事件已同步为已解决').waitFor({
    state: 'visible',
  })
  const desktopAudit = await layoutAudit(adminPage, '桌面端')
  const desktopShot = await screenshot(adminPage, '13-simulation-admin-desktop.png')
  record('admin 工单卡入口与完整控制闭环', {
    task_id: TASK_ID,
    final_state: finalSnapshot.state,
    route_points: finalSnapshot.route.length,
    log_entries: finalSnapshot.logs.length,
    desktop_audit: desktopAudit,
    screenshot: desktopShot,
  })

  const historyPage = await adminContext.newPage()
  asyncPage(historyPage)
  await historyPage.goto(`${FRONTEND}/simulation/${HISTORY_TASK_ID}`, {
    waitUntil: 'domcontentloaded',
  })
  await waitSimulationState(historyPage, '历史轨迹')
  assert.equal(
    await historyPage.getByRole('button', { name: '启动' }).isDisabled(),
    true,
  )
  assert.equal(
    await historyPage.getByRole('button', { name: '1x' }).isDisabled(),
    true,
  )
  assert.equal(
    await historyPage.getByRole('button', { name: '停止' }).isDisabled(),
    true,
  )
  const historyAudit = await layoutAudit(historyPage, '历史轨迹')
  const historyShot = await screenshot(
    historyPage,
    '14-simulation-history-readonly.png',
  )
  record('历史轨迹只读且控制禁用', {
    task_id: HISTORY_TASK_ID,
    audit: historyAudit,
    screenshot: historyShot,
  })
  await historyPage.close()
  await adminContext.storageState({
    path: path.join(ARTIFACT_DIR, 'simulation-admin-state.json'),
  })

  const mobileContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    storageState: path.join(ARTIFACT_DIR, 'simulation-admin-state.json'),
  })
  const mobilePage = await mobileContext.newPage()
  asyncPage(mobilePage)
  await mobilePage.goto(`${FRONTEND}/simulation/${TASK_ID}`, {
    waitUntil: 'domcontentloaded',
  })
  await waitSimulationState(mobilePage, '已完成')
  const mobileAudit = await layoutAudit(mobilePage, '移动端')
  const mobileShot = await screenshot(mobilePage, '15-simulation-mobile.png')
  record('移动端无横向溢出且核心区域可见', {
    audit: mobileAudit,
    screenshot: mobileShot,
  })
  await mobileContext.close()
  await adminContext.close()

  const viewerContext = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  })
  const viewerPage = await viewerContext.newPage()
  asyncPage(viewerPage)
  await login(viewerPage, VIEWER)
  await viewerPage.goto(`${FRONTEND}/simulation/${TASK_ID}`, {
    waitUntil: 'domcontentloaded',
  })
  await waitSimulationState(viewerPage, '已完成')
  const disabledControls = await viewerPage
    .locator('.sim-controls__primary .sim-control')
    .evaluateAll((buttons) => buttons.map((button) => button.disabled))
  assert.ok(disabledControls.length > 0)
  assert.equal(disabledControls.every(Boolean), true)
  const disabledSpeeds = await viewerPage
    .locator('.sim-controls__speeds button')
    .evaluateAll((buttons) => buttons.map((button) => button.disabled))
  assert.equal(disabledSpeeds.every(Boolean), true)
  await viewerPage.getByText('当前账号为只读权限，可查看轨迹但不能控制仿真').waitFor({
    state: 'visible',
  })
  const viewerAudit = await layoutAudit(viewerPage, 'viewer 只读')
  const viewerShot = await screenshot(viewerPage, '16-simulation-viewer-readonly.png')
  record('viewer 权限下仿真控制按钮全部禁用', {
    audit: viewerAudit,
    screenshot: viewerShot,
  })
  await viewerContext.close()

  assert.deepEqual(runtimeErrors, [], `浏览器运行时错误: ${runtimeErrors.join(' | ')}`)
}

try {
  await run()
} catch (error) {
  console.error(error.stack || String(error))
  process.exitCode = 1
} finally {
  if (browser) await browser.close()
  const report = {
    generated_at: new Date().toISOString(),
    frontend: FRONTEND,
    backend: BACKEND,
    task_id: TASK_ID,
    history_task_id: HISTORY_TASK_ID,
    passed: process.exitCode !== 1,
    runtime_errors: runtimeErrors,
    results,
  }
  await mkdir(ARTIFACT_DIR, { recursive: true })
  const reportFile = path.join(ARTIFACT_DIR, 'simulation-latest.json')
  await writeFile(reportFile, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  console.log(`报告已写入: ${reportFile}`)
}

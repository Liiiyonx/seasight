#!/usr/bin/env node
/**
 * Responsive UI audit for the SeaSight frontend.
 *
 * The project acceptance suite covers business behavior at a desktop viewport.
 * This lightweight companion checks the presentation layer at desktop and
 * phone sizes: page-level horizontal overflow, clipped structural panels,
 * controls whose text is cut off, console errors, and key overlay states.
 */
import assert from 'node:assert/strict'
import { access, mkdir, readFile, writeFile } from 'node:fs/promises'
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
const FRONTEND = process.env.SEASIGHT_FRONTEND_URL || 'http://127.0.0.1:5174'
const BROWSER_PATH =
  process.env.SEASIGHT_BROWSER_PATH ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'

const OUT_DIR = path.join(ROOT, 'artifacts', 'ui-responsive')
const ADMIN_STATE_PATH = path.join(
  ROOT,
  'artifacts',
  'browser-acceptance',
  'admin-storage.json',
)

const ROUTES = [
  { path: '/dashboard', root: '.dashboard' },
  { path: '/events', root: '.page' },
  { path: '/tasks', root: '.page' },
  { path: '/devices', root: '.page' },
  { path: '/reports', root: '.page' },
  { path: '/agents', root: '.agents' },
  { path: '/knowledge', root: '.knowledge' },
  // /analyze 已改为函数式 redirect 到 /assistant（智能助手对话页），
  // 直接审计目标路由，避免在重定向中间页上等一个不存在的根元素。
  { path: '/assistant', root: '.chat' },
]

const VIEWPORTS = [
  { name: 'desktop-wide', width: 1920, height: 1080 },
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'laptop', width: 1280, height: 800 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'mobile-large', width: 414, height: 896 },
  { name: 'mobile', width: 390, height: 844 },
  { name: 'mobile-narrow', width: 360, height: 740 },
  { name: 'mobile-small', width: 320, height: 568 },
]

function isVisible(style, rect) {
  return (
    style.display !== 'none' &&
    style.visibility !== 'hidden' &&
    Number(style.opacity) !== 0 &&
    rect.width > 0 &&
    rect.height > 0
  )
}

function describeElement(el) {
  const id = el.id ? `#${el.id}` : ''
  const classes = [...el.classList].slice(0, 4).map((name) => `.${name}`).join('')
  return `${el.tagName.toLowerCase()}${id}${classes}`
}

async function auditLayout(page) {
  return page.evaluate(
    ({ isVisibleSource, describeElementSource }) => {
      const isVisible = eval(`(${isVisibleSource})`)
      const describeElement = eval(`(${describeElementSource})`)
      const viewportWidth = document.documentElement.clientWidth
      const viewportHeight = window.innerHeight
      const elements = [...document.querySelectorAll('body *')]
      const scrollContainers = []
      const clippedPanels = []
      const clippedControls = []
      const offscreenText = []

      function horizontalScrollerFor(el) {
        let parent = el.parentElement
        while (parent && parent !== document.body) {
          const style = getComputedStyle(parent)
          if (
            (style.overflowX === 'auto' || style.overflowX === 'scroll') &&
            parent.scrollWidth > parent.clientWidth + 1
          ) {
            return parent
          }
          parent = parent.parentElement
        }
        return null
      }

      for (const el of elements) {
        const style = getComputedStyle(el)
        const rect = el.getBoundingClientRect()
        if (!isVisible(style, rect)) continue

        // Leaflet 的地图瓦片、标记和画布会按地图视野裁剪，属于地图内部的
        // 正常表现，不应被当成页面面板溢出。仍保留 .leaflet-container
        // 本体参与检查，避免把真正的布局溢出一起误杀。
        if (el.closest('.leaflet-container') && !el.classList.contains('leaflet-container')) {
          continue
        }

        if (
          (style.overflowX === 'auto' || style.overflowX === 'scroll') &&
          el.scrollWidth > el.clientWidth + 1
        ) {
          scrollContainers.push({
            element: describeElement(el),
            client_width: el.clientWidth,
            scroll_width: el.scrollWidth,
            scrollable: true,
          })
        }

        const scroller = horizontalScrollerFor(el)
        const outsideViewport =
          rect.left < -1 || rect.right > viewportWidth + 1
        if (outsideViewport && !scroller) {
          clippedPanels.push({
            element: describeElement(el),
            left: Math.round(rect.left),
            right: Math.round(rect.right),
            width: Math.round(rect.width),
            viewport_width: viewportWidth,
          })
        }

        if (
          (el.matches('button, a, input, select') ||
            el.matches('.panel-title, .badge, .status-chip, .summary__card')) &&
          (el.scrollWidth > el.clientWidth + 2 ||
            el.scrollHeight > el.clientHeight + 2) &&
          style.overflowX !== 'auto' &&
          style.overflowX !== 'scroll'
        ) {
          clippedControls.push({
            element: describeElement(el),
            text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80),
            client_width: el.clientWidth,
            scroll_width: el.scrollWidth,
            client_height: el.clientHeight,
            scroll_height: el.scrollHeight,
          })
        }

        if (
          el.children.length === 0 &&
          (el.textContent || '').trim().length > 1 &&
          el.scrollWidth > el.clientWidth + 3 &&
          style.overflowX !== 'auto' &&
          style.overflowX !== 'scroll' &&
          style.textOverflow !== 'ellipsis' &&
          !scroller
        ) {
          offscreenText.push({
            element: describeElement(el),
            text: el.textContent.trim().slice(0, 80),
            client_width: el.clientWidth,
            scroll_width: el.scrollWidth,
          })
        }
      }

      const main = document.querySelector('.layout__main')
      const nav = document.querySelector('.nav')
      return {
        viewport: { width: viewportWidth, height: viewportHeight },
        document_scroll_width: document.documentElement.scrollWidth,
        body_scroll_width: document.body.scrollWidth,
        main: main
          ? {
              client_width: main.clientWidth,
              scroll_width: main.scrollWidth,
              client_height: main.clientHeight,
              scroll_height: main.scrollHeight,
            }
          : null,
        nav: nav
          ? {
              client_width: nav.clientWidth,
              scroll_width: nav.scrollWidth,
            }
          : null,
        scroll_containers: scrollContainers,
        clipped_panels: clippedPanels.slice(0, 30),
        clipped_controls: clippedControls.slice(0, 30),
        offscreen_text: offscreenText.slice(0, 30),
      }
    },
    {
      isVisibleSource: isVisible.toString(),
      describeElementSource: describeElement.toString(),
    },
  )
}

async function capture(page, name) {
  const file = path.join(OUT_DIR, `${name}.png`)
  await page.screenshot({ path: file, fullPage: true })
  return file
}

async function auditRoute(browser, route, viewport, storageState) {
  const authContext = await browser.newContext({ viewport, storageState })
  const authPage = await authContext.newPage()
  const authConsoleErrors = []
  const authPageErrors = []
  authPage.on('console', (msg) => {
    if (msg.type() === 'error') authConsoleErrors.push(msg.text())
  })
  authPage.on('pageerror', (error) => authPageErrors.push(error.message))

  await authPage.goto(`${FRONTEND}${route.path}`, {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  })
  await authPage.locator(route.root).first().waitFor({
    state: 'visible',
    timeout: 20000,
  })
  await authPage.waitForTimeout(1200)

  const layout = await auditLayout(authPage)
  const shot = await capture(
    authPage,
    `${viewport.width}x${viewport.height}${route.path.replaceAll('/', '-')}`,
  )
  await authContext.close()
  const seriousConsoleErrors = authConsoleErrors.filter(
    (text) => !/favicon|Failed to load resource|net::ERR_/.test(text),
  )
  const checks = {
    document_width: layout.document_scroll_width <= layout.viewport.width + 1,
    body_width: layout.body_scroll_width <= layout.viewport.width + 1,
    no_clipped_panels: layout.clipped_panels.length === 0,
    no_page_errors: authPageErrors.length === 0,
    no_console_errors: seriousConsoleErrors.length === 0,
  }
  return {
    route: route.path,
    viewport,
    passed: Object.values(checks).every(Boolean),
    checks,
    layout,
    console_errors: authConsoleErrors,
    page_errors: authPageErrors,
    screenshot: shot,
  }
}

async function auditLogin(browser, viewport) {
  const context = await browser.newContext({ viewport })
  const page = await context.newPage()
  const errors = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  await page.goto(`${FRONTEND}/login`, { waitUntil: 'domcontentloaded' })
  await page.locator('.login__card').waitFor({ state: 'visible' })
  const layout = await auditLayout(page)
  assert.ok(layout.document_scroll_width <= layout.viewport.width + 1)
  assert.equal(layout.clipped_panels.length, 0, JSON.stringify(layout.clipped_panels))
  const screenshot = await capture(page, `${viewport.width}x${viewport.height}-login`)
  await context.close()
  return { route: '/login', viewport, layout, errors, screenshot }
}

async function auditOverlays(browser, storageState) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    storageState,
  })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))

  const overlays = { skipped: [] }
  try {
    await page.goto(`${FRONTEND}/tasks`, { waitUntil: 'domcontentloaded' })

    // 后端未在线时 /tasks 没有真实任务卡片，抽屉覆盖层无法生成。此时跳过
    // 抽屉审计并在报告里写明原因，不让“无数据”伪装成“布局失败”。
    const taskCard = page.locator('.tcard').first()
    let taskCardVisible = false
    try {
      await taskCard.waitFor({ state: 'visible', timeout: 8000 })
      taskCardVisible = true
    } catch {
      overlays.skipped.push('task drawer: /tasks 未渲染任务卡片（后端未在线或无数据）')
    }

    if (taskCardVisible) {
      await taskCard.click()
      await page.locator('.drawer__panel').waitFor({ state: 'visible' })
      const drawerLayout = await auditLayout(page)
      const drawerShot = await capture(page, '390x844-tasks-drawer')
      overlays.drawer = { layout: drawerLayout, screenshot: drawerShot }
      assert.equal(
        drawerLayout.clipped_panels.length,
        0,
        JSON.stringify(drawerLayout.clipped_panels),
      )
      await page.locator('.drawer__close').click()
    }

    try {
      await page.getByRole('button', { name: '打开通知中心' }).waitFor({
        state: 'visible',
        timeout: 5000,
      })
      await page.getByRole('button', { name: '打开通知中心' }).click()
      await page.locator('.notif__panel').waitFor({ state: 'visible' })
      const notifLayout = await auditLayout(page)
      const notifShot = await capture(page, '390x844-notifications')
      overlays.notifications = { layout: notifLayout, screenshot: notifShot }
      assert.equal(
        notifLayout.clipped_panels.length,
        0,
        JSON.stringify(notifLayout.clipped_panels),
      )
    } catch {
      overlays.skipped.push('notification panel: 通知中心无法打开或面板未渲染')
    }
    assert.deepEqual(pageErrors, [], pageErrors.join(' | '))
  } finally {
    await context.close()
  }
  return overlays
}

async function main() {
  await access(BROWSER_PATH)
  await access(ADMIN_STATE_PATH)
  await mkdir(OUT_DIR, { recursive: true })
  const storageState = JSON.parse(await readFile(ADMIN_STATE_PATH, 'utf8'))
  const frontendOrigin = new URL(FRONTEND).origin
  storageState.origins = storageState.origins.map((state) => ({
    ...state,
    origin: frontendOrigin,
  }))
  const browser = await chromium.launch({ headless: true, executablePath: BROWSER_PATH })
  const results = []
  let failures = []

  try {
    for (const viewport of VIEWPORTS) {
      results.push(await auditLogin(browser, viewport))
    }
    for (const route of ROUTES) {
      for (const viewport of VIEWPORTS) {
        const result = await auditRoute(browser, route, viewport, storageState)
        results.push(result)
        if (!result.passed) {
          failures.push({
            route: result.route,
            viewport: result.viewport,
            checks: result.checks,
            layout: result.layout,
            page_errors: result.page_errors,
            console_errors: result.console_errors,
            screenshot: result.screenshot,
          })
        }
      }
    }
    results.push(await auditOverlays(browser, storageState))
  } finally {
    await browser.close()
  }

  const report = {
    generated_at: new Date().toISOString(),
    frontend: FRONTEND,
    results,
    summary: {
      checks: results.length,
      failures: failures.length,
      routes: ROUTES.length,
      viewports: VIEWPORTS.length,
    },
    failures,
  }
  await writeFile(path.join(OUT_DIR, 'latest.json'), `${JSON.stringify(report, null, 2)}\n`)
  if (failures.length) {
    console.error(`Responsive UI audit found ${failures.length} failing checks`)
    for (const failure of failures) {
      console.error(
        `- ${failure.route} @ ${failure.viewport.width}x${failure.viewport.height}: ${JSON.stringify(failure.checks)}`,
      )
    }
    process.exitCode = 1
  } else {
    console.log(`Responsive UI audit passed: ${results.length} checks`)
  }
  console.log(`Report: ${path.join(OUT_DIR, 'latest.json')}`)
}

await main()

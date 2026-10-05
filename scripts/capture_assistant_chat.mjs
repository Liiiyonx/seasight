#!/usr/bin/env node
/**
 * 补拍「智能助手」对话页：软著说明书图 5-7 用。
 *
 * 背景：平台数据已灌好（事件/工单/设备/知识资产都有真实条目），但助手页
 * 截图时是空白对话区——软著审查员看到空白的「智能助手对话页面」会认为
 * 该功能未实现。本脚本在真实运行的平台上发起一轮问答，拿到真实回答后
 * 截图，让图 5-7 名副其实。
 *
 * 用法（需先起好前端 5173 与后端 8001）：
 *   PLAYWRIGHT_CORE_PATH=<全局 playwright-core 路径> \
 *   SEASIGHT_FRONTEND_URL=http://127.0.0.1:5173 \
 *   node scripts/capture_assistant_chat.mjs
 */
import { mkdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = require(
  process.env.PLAYWRIGHT_CORE_PATH || 'playwright-core',
)

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const FRONTEND = process.env.SEASIGHT_FRONTEND_URL || 'http://127.0.0.1:5173'
const BROWSER =
  process.env.SEASIGHT_BROWSER_PATH ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const OUT_DIR = path.join(ROOT, 'artifacts', 'ui-responsive')
const STORAGE = path.join(
  ROOT, 'artifacts', 'browser-acceptance', 'admin-storage.json',
)

// 说明书图 5-7 的三个引导问题，逐个问一遍，覆盖不同能力面：
// 统计查询 / 策略问答 / 平台操作。
const QUESTIONS = [
  '今天泡沫类事件有多少？',
  '高风险写操作为什么需要人工审批？',
  '帮我看看当前有哪些设备离线',
]

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function main() {
  await mkdir(OUT_DIR, { recursive: true })
  const browser = await chromium.launch({
    executablePath: BROWSER,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    storageState: STORAGE,
    locale: 'zh-CN',
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (m) => {
    if (m.type() === 'error' && !/favicon|ERR_/.test(m.text())) {
      errors.push(m.text())
    }
  })

  await page.goto(`${FRONTEND}/assistant`, {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  })
  await page.locator('.chat').first().waitFor({
    state: 'visible',
    timeout: 20000,
  })
  await sleep(1500)

  const box = page.locator('textarea, input[placeholder*="输入"]').first()
  if ((await box.count()) === 0) {
    throw new Error('未找到输入框，无法发起对话')
  }

  for (const q of QUESTIONS) {
    await box.fill(q)
    await page.keyboard.press('Enter')
    // 等回答渲染完：气泡数量增加且出现非流式内容后留出余量
    await sleep(4500)
  }
  // 最后一轮可能仍在流式输出，等输入框重新可用即视为完成
  try {
    await page.locator('textarea, input[placeholder*="输入"]').first()
      .waitFor({ state: 'visible', timeout: 15000 })
    await sleep(2500)
  } catch {
    await sleep(2000)
  }

  const file = path.join(OUT_DIR, '1920x1080-assistant.png')
  await page.screenshot({ path: file, fullPage: true })
  await context.close()
  await browser.close()

  console.log(`截图已保存: ${file}`)
  if (errors.length) {
    console.log(`页面错误 ${errors.length} 条:`)
    errors.slice(0, 5).forEach((e) => console.log(`  - ${e}`))
  } else {
    console.log('无页面错误')
  }
}

main().catch((err) => {
  console.error('失败:', err.message)
  process.exit(1)
})

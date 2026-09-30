#!/usr/bin/env node
/**
 * SeaSight 知识域完整问答轨迹采集（E1/E2）
 * =====================================================================
 * 采集一条可复现的“问题 → 检索 → 多跳路径 → 带资产版本引用的回答/证据链”
 * 轨迹：真实登录后端 → 前端知识域检索页执行问题检索 → 记录决策 →
 * 打开决策证据链，保存 API JSON 轨迹 + 页面截图。
 *
 * 诚实口径：
 *   - 这是确定性“检索-推理”链路（本体图检索 + 决策证据链），
 *     不是 LLM 生成式问答，也不是真实脱敏行业数据评测；
 *   - 资产为演示资产（标题带 DEMO），证据等级 E1/E2；
 *   - 不配置模型时不会伪装成“智能问答已跑通”。
 *
 * 运行前提：后端 http://127.0.0.1:8001、前端 dev server
 * http://127.0.0.1:5174 在线；系统 Chrome；全局 playwright-core。
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const PLAYWRIGHT_CORE_PATH = process.env.PLAYWRIGHT_CORE_PATH || 'playwright-core'

let playwright
try {
  playwright = require(PLAYWRIGHT_CORE_PATH)
} catch (err) {
  console.error('[预检] 无法加载 playwright-core:', err.message)
  process.exit(1)
}
const { chromium } = playwright

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const OUT_DIR = path.join(ROOT, 'artifacts', 'knowledge-qa-trace')
const FRONTEND = process.env.SEASIGHT_FRONTEND_URL || 'http://127.0.0.1:5174'
const BACKEND = process.env.SEASIGHT_BACKEND_URL || 'http://127.0.0.1:8001'
const BROWSER_PATH = process.env.SEASIGHT_BROWSER_PATH || undefined

const ADMIN = { username: 'admin', password: 'admin123456' }
const QUESTION =
  '马鼻镇出现泡沫聚集时，应如何处置并留存证据链？'
const ANSWER_SUMMARY =
  '根据治理方案与月度台账，由值班研判人员复核后派单，调度岸基机械臂或打捞机器人拾取，并回传拾取回执形成闭环。'

async function api(pathname, payload) {
  const res = await fetch(`${BACKEND}/api/v1${pathname}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await res.json()
  return body
}

async function login() {
  const body = await api('/auth/login', ADMIN)
  if (body.code !== 0 || !body.data?.access_token) {
    throw new Error(`login failed: ${body.message || 'no token'}`)
  }
  return body.data
}

async function screenshot(page, name) {
  const file = path.join(OUT_DIR, name)
  await page.screenshot({ path: file, fullPage: false })
  return file
}

async function waitForApi(page, prefix, { timeout = 20000, method = null } = {}) {
  return page
    .waitForResponse(
      (res) => {
        if (method && res.request().method().toUpperCase() !== method.toUpperCase()) {
          return false
        }
        try {
          return new URL(res.url()).pathname.startsWith(`/api/v1${prefix}`)
        } catch {
          return false
        }
      },
      { timeout },
    )
    .then(async (res) => ({
      path: new URL(res.url()).pathname,
      status: res.status(),
      body: (await res.json().catch(() => null)),
    }))
}

async function main() {
  const auth = await login()
  const apiTraces = []

  const browser = await chromium.launch({
    executablePath: BROWSER_PATH,
    headless: true,
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const pageErrors = []
  page.on('pageerror', (err) => pageErrors.push(err.message))

  // 真实登录响应 → 前端会话状态（与 LoginView 的 setAuth 相同结构）。
  await page.goto(`${FRONTEND}/`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(
    ({ token, user }) => {
      localStorage.setItem('seasight_token', token)
      localStorage.setItem('seasight_user', JSON.stringify(user))
    },
    {
      token: auth.access_token,
      user: {
        username: ADMIN.username,
        role: auth.role,
        full_name: auth.full_name,
        township_scope: auth.township_scope,
      },
    },
  )

  // 1) 检索页：执行“马鼻镇泡沫聚集处置”问题。
  await page.goto(`${FRONTEND}/knowledge`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('tab', { name: '检索' }).click()
  const searchApiPromise = waitForApi(page, '/knowledge/search', { method: 'POST' })
  await page.locator('.knowledge__form input[type="text"]').first().waitFor({ state: 'visible' })
  const searchInput = page.locator('.knowledge__form input[type="text"]').first()
  await searchInput.fill(QUESTION)
  await page.getByRole('button', { name: '检索' }).first().click()
  const searchApi = await searchApiPromise
  apiTraces.push({ phase: 'search', ...searchApi })
  await page.locator('.knowledge__result').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(600)
  const searchShot = await screenshot(page, '01-检索结果-本体图检索与引用.png')

  // 2) 决策页：用同一问题记录决策并自动生成证据链。
  await page.getByRole('tab', { name: '决策' }).click()
  await page.getByRole('button', { name: '＋ 记录决策' }).click()
  await page.locator('.knowledge__form input[type="text"]').first().fill(QUESTION)
  const summaryArea = page.locator('textarea').first()
  await summaryArea.fill(ANSWER_SUMMARY)
  const decisionApiPromise = waitForApi(page, '/knowledge/decisions', {
    timeout: 25000,
    method: 'POST',
  })
  await page.getByRole('button', { name: '生成决策与证据链' }).click()
  const decisionApi = await decisionApiPromise
  apiTraces.push({ phase: 'create_decision', ...decisionApi })

  // 新决策默认不自动展开证据链，点击列表第一项即可打开最近轨迹。
  await page.locator('.knowledge__list-item').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.locator('.knowledge__list-item').first().click()
  const evidenceApiPromise = waitForApi(page, '/knowledge/decisions/', {
    timeout: 25000,
    method: 'GET',
  })
  await page.locator('.knowledge__evidence-item').first().waitFor({ state: 'visible', timeout: 15000 })
  const evidenceApi = await evidenceApiPromise
  apiTraces.push({ phase: 'decision_evidence', ...evidenceApi })
  await page.waitForTimeout(600)
  const evidenceShot = await screenshot(page, '02-决策证据链-资产版本引用.png')

  if (pageErrors.length) {
    throw new Error(`page errors: ${pageErrors.join(' | ')}`)
  }
  await browser.close()

  const searchBody = searchApi.body?.data || {}
  const decisionBody = decisionApi.body?.data || {}
  const traceId = decisionBody.trace_id
  const report = {
    status: 'ok',
    generated_at: new Date().toISOString(),
    evidence_level: 'E1/E2',
    scope_note:
      '确定性检索-推理链路演示：本体图检索 + 决策证据链，非 LLM 生成式问答；' +
      '资产为演示资产，非真实脱敏行业数据评测，不代表感知精度或真实部署。',
    question: QUESTION,
    search: {
      mode: searchBody.mode,
      ontology_version_id: searchBody.ontology_version_id,
      total: searchBody.total,
      results: (searchBody.results || []).map((hit) => ({
        title: hit.title,
        asset_id: hit.asset_id,
        asset_version_id: hit.asset_version_id,
        hop_count: hit.hop_count,
        score: hit.score,
        path: (hit.path || []).map((step) => ({
          hop_no: step.hop_no,
          relation_type: step.relation_type,
        })),
        citations: hit.citations,
      })),
    },
    decision: {
      trace_id: traceId,
      status: decisionBody.status,
      answer_summary: decisionBody.answer_summary,
      ontology_version_id: decisionBody.ontology_version_id,
    },
    evidence: (evidenceApi.body?.data || []).map((item) => ({
      rank_no: item.rank_no,
      asset_id: item.asset_id,
      asset_version_id: item.asset_version_id,
      hop_no: item.hop_no,
      citation_text: item.citation_text,
    })),
    api_traces: apiTraces,
    screenshots: [searchShot, evidenceShot],
  }

  await mkdir(OUT_DIR, { recursive: true })
  await writeFile(path.join(OUT_DIR, 'latest.json'), JSON.stringify(report, null, 2), 'utf-8')
  console.log(`QA TRACE OK -> ${path.join(OUT_DIR, 'latest.json')}`)
  console.log(
    `mode=${searchBody.mode} hits=${searchBody.total} trace=${traceId} evidence=${report.evidence.length}`,
  )
}

main().catch((err) => {
  console.error('[FAIL]', err)
  process.exit(1)
})

<script setup>
/**
 * 智能助手对话页 —— 取代原 AgentsView（运维控制台）+ AnalyzeView（手动上传分析）。
 *
 * 一个对话框完成三件事：打字问答（规则兜底 / 可插拔 LLM）、拖入或粘贴图片
 * 自动检测、对事件生成派单建议卡并一键确认（确认仍走 POST /agents/runs
 * 审批流程，本页不直接写库）。
 *
 * 后端契约（app/schemas/chat.py）：
 * - 回复块统一 { type, data } 信封，type ∈
 *   text / detection / event_card / event_list / stats / dispatch_suggest / error
 * - detection.data = { image_index, detections[{class,confidence,bbox}], count, ... }
 *   图片二进制不入库：本次会话的图存在本地 dataURL，用 image_index 关联画框；
 *   历史消息无本地图 → 检测块降级为列表展示。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentsApi, assistantApi } from '@/api'
import DetectionCanvas from '@/components/DetectionCanvas.vue'
import { canWrite, isLoggedIn } from '@/utils/auth'
import { classColor, classLabel } from '@/utils/constants'
import { VISION_SAMPLES, visionAssetUrl } from '@/utils/visionSamples'
import { fmtRelative, fmtShortTime } from '@/utils/format'

// ?session=<session_id> 直达指定会话（router props 注入）
const props = defineProps({
  sessionId: { type: String, default: '' },
})

const route = useRoute()
const router = useRouter()

// ---------- 常量 ----------
const MAX_IMAGES = 6
const MAX_IMAGE_MB = 8 // 与后端 assistant_max_image_mb 默认值对齐
const HISTORY_PAGE_SIZE = 100

const QUICK_PROMPTS = ['最新事件列表', '今天有多少事件', '平台运营统计']

// 多智能体一键直聊：专家 key 必须与后端 _EXPERT_PERSONAS 一致；颜色仅用于徽章区分
// greeting/prompts 用于"进入专家会话后主区空态"——点谁就显示谁，不能还是通用助手
const EXPERTS = [
  {
    key: 'data', name: '数据分析师', badge: '数', desc: '统计口径 · 趋势 · 聚类分析', color: '#18a999',
    greeting: '我是数据分析师，帮你用数据看平台：事件趋势、类别分布、区域对比。',
    prompts: ['今天的运营统计', '近 7 天事件趋势', '哪类垃圾最多？'],
  },
  {
    key: 'dispatch', name: '外业调度师', badge: '调', desc: '就近派单 · 调度 · 作业窗口', color: '#3b82f6',
    greeting: '我是外业调度师，帮你把打捞任务派得又快又省。',
    prompts: ['当前有多少待处理工单？', '给我最近的派单建议', '哪台机器人离得最近？'],
  },
  {
    key: 'env', name: '环境评估师', badge: '环', desc: '污染影响 · 潮汐风向 · 扩散研判', color: '#22c55e',
    greeting: '我是环境评估师，帮你研判污染影响与垃圾漂移方向。',
    prompts: ['结合风向判断垃圾漂移？', '近岸聚集风险如何？', '今天适合出海作业吗？'],
  },
  {
    key: 'device', name: '设备运维师', badge: '维', desc: '摄像头 · 机器人 · 无人机排障', color: '#f59e0b',
    greeting: '我是设备运维师，帮你排查摄像头、机器人、无人机的状态与故障。',
    prompts: ['设备在线情况如何？', '有哪些设备离线？', '机器人电量状态怎么样？'],
  },
  {
    key: 'law', name: '执法文书员', badge: '法', desc: '处置台账 · 文书模板 · 合规', color: '#a855f7',
    greeting: '我是执法文书员，帮你整理处置台账与规范文书。',
    prompts: ['帮我拟一份处置记录模板', '最近的处置台账统计', '处置流程要注意什么？'],
  },
  {
    key: 'emergency', name: '应急响应员', badge: '急', desc: '突发集聚 · 响应分级 · 资源协调', color: '#ef4444',
    greeting: '我是应急响应员，帮你应对突发垃圾集聚事件。',
    prompts: ['当前有没有需要紧急处理的事件？', '突发集聚怎么分级响应？', '应急资源怎么协调？'],
  },
]

// ---------- 状态 ----------
const sessions = ref([])
const sessionsLoading = ref(false)
const sideCollapsed = ref(false)
const confirmDeleteId = ref('') // 侧栏删除两段确认：第一次点 × 出「确认？」

const activeSessionId = ref('')
const messages = ref([]) // 消息视图模型，见 historyToVm / send
const historyLoading = ref(false)

const inputText = ref('')
const pendingImages = ref([]) // [{ file, dataUrl }]
const sending = ref(false)
const pageError = ref('')

// 拖拽计数防闪烁：子元素间 dragenter/dragleave 成对触发，计数归零才算真正离开
const dragDepth = ref(0)

// 派单确认状态：key = `${messageKey}:${blockIndex}` → { status, runId, replay, error }
const dispatchState = ref({})

const msgList = ref(null)
const fileInput = ref(null)
const composerTextarea = ref(null)

// ---------- 多智能体直聊：会话 ↔ 专家绑定 ----------
// ref(new Map())：Vue 3 对 Map 集合做了响应式代理，get/set 可被 computed 追踪
const expertBySession = ref(new Map()) // session_id -> expert key
const expertBusy = ref('') // 正在为某专家建会话时置 key，防重复点击

const currentExpertKey = computed(() => {
  const sid = activeSessionId.value
  if (!sid) return ''
  const bound = expertBySession.value.get(sid)
  if (bound) return bound
  // 刷新页面/直达 ?session= 时内存绑定会丢：从会话标题前缀恢复
  //（"设备运维师 · 直聊"）—— 否则点专家进去会显示成通用助手
  const title = sessions.value.find((s) => s.session_id === sid)?.title || ''
  const hit = EXPERTS.find((e) => e.name && title.startsWith(e.name))
  return hit ? hit.key : ''
})
const currentExpert = computed(
  () => EXPERTS.find((e) => e.key === currentExpertKey.value) || null,
)

// 已存在标题以「专家名」开头的会话 → 视为该专家的直聊会话，直接复用（不重复建）
function findExpertSession(expert) {
  return sessions.value.find((s) => s.title && s.title.startsWith(expert.name))
}

async function startExpertChat(expert) {
  if (expertBusy.value) return
  const existing = findExpertSession(expert)
  if (existing) {
    expertBySession.value.set(existing.session_id, expert.key)
    if (activeSessionId.value === existing.session_id) {
      composerTextarea.value?.focus()
      return
    }
    await openSession(existing.session_id)
    composerTextarea.value?.focus()
    return
  }
  expertBusy.value = expert.key
  try {
    const s = await assistantApi.createSession({ title: `${expert.name} · 直聊` })
    const sid = s?.session_id
    if (!sid) throw new Error('会话创建失败')
    // 新会话插到列表头部（按 updated_at 排序本就该在最前）
    sessions.value = [s, ...sessions.value.filter((x) => x.session_id !== sid)]
    expertBySession.value.set(sid, expert.key)
    await openSession(sid)
    composerTextarea.value?.focus()
  } catch (err) {
    pageError.value = err.message || '创建专家会话失败'
  } finally {
    expertBusy.value = ''
  }
}

function exitExpertMode() {
  if (!activeSessionId.value) return
  expertBySession.value.delete(activeSessionId.value)
}

// ---------- 会话列表 ----------
async function loadSessions() {
  sessionsLoading.value = true
  try {
    sessions.value = (await assistantApi.listSessions()) || []
  } catch (err) {
    pageError.value = err.message || '会话列表加载失败'
  } finally {
    sessionsLoading.value = false
  }
}

function historyToVm(m) {
  return {
    key: m.message_id,
    role: m.role,
    content: m.content || '',
    blocks: m.role === 'assistant' ? m.payload?.blocks || [] : [],
    localImages: [], // 历史消息没有本地图（图片二进制本就不入库）
    imageCount: m.content_type === 'image' ? m.payload?.image_count || 1 : 0,
    time: m.created_at,
    pending: false,
    failed: false,
  }
}

async function openSession(id) {
  if (!id || id === activeSessionId.value) return
  activeSessionId.value = id
  historyLoading.value = true
  messages.value = []
  try {
    const res = await assistantApi.listMessages(id, { page: 1, page_size: HISTORY_PAGE_SIZE })
    messages.value = (res?.items || []).map(historyToVm)
    syncQuery()
    scrollToBottom(true)
  } catch (err) {
    activeSessionId.value = ''
    pageError.value = err.message || '会话加载失败'
  } finally {
    historyLoading.value = false
  }
}

function newChat() {
  activeSessionId.value = ''
  messages.value = []
  syncQuery()
  composerTextarea.value?.focus()
}

async function removeSession(s) {
  // 两段确认：第一次点击仅进入确认态，再次点击才真正删除
  if (confirmDeleteId.value !== s.session_id) {
    confirmDeleteId.value = s.session_id
    setTimeout(() => {
      if (confirmDeleteId.value === s.session_id) confirmDeleteId.value = ''
    }, 3000)
    return
  }
  confirmDeleteId.value = ''
  try {
    await assistantApi.deleteSession(s.session_id)
    sessions.value = sessions.value.filter((x) => x.session_id !== s.session_id)
    if (activeSessionId.value === s.session_id) newChat()
  } catch (err) {
    pageError.value = err.message || '删除会话失败'
  }
}

function syncQuery() {
  const query = { ...route.query }
  if (activeSessionId.value) query.session = activeSessionId.value
  else delete query.session
  router.replace({ query })
}

// ---------- ?sample= 直达（来自「知识智能体 · 视觉实测」的样本图） ----------
// 视觉实测点一张样本图会带 sample id 跳到这里。以前这条链路是断的：
// 路由 /analyze 没有 name，push 直接抛错中止导航，点了毫无反应。
// 现在把样本还原成一张待发送图片，复用与手动上传完全相同的 addFiles 路径，
// 用户只需点「发送」（不自动发送，避免在用户没看清时就把请求打出去）。
const consumedSample = ref('') // 已消费的样本 id，防止重复触发

async function consumeQuerySample() {
  const sampleId = typeof route.query.sample === 'string' ? route.query.sample : ''
  if (!sampleId || sampleId === consumedSample.value) return
  consumedSample.value = sampleId

  // 先把 sample 从 URL 摘掉：否则每次回到本页都会重新灌一张图
  const rest = { ...route.query }
  delete rest.sample
  router.replace({ query: rest })

  const sample = VISION_SAMPLES.find((s) => s.id === sampleId)
  if (!sample) {
    pageError.value = `未找到演示样本「${sampleId}」`
    return
  }

  try {
    const res = await fetch(visionAssetUrl(sample.image))
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const ext = String(sample.image).split('.').pop() || 'webp'
    const file = new File([blob], `${sample.id}.${ext}`, {
      type: blob.type || 'image/webp',
    })
    await addFiles([file])
    if (!inputText.value.trim()) {
      inputText.value = `请分析这张样例图：${sample.title}`
    }
  } catch (err) {
    pageError.value = `样例图载入失败：${err?.message || err}`
  }
}

// ---------- 发送 ----------
async function send() {
  if (sending.value) return
  const text = inputText.value.trim()
  const imgs = pendingImages.value.slice()
  if (!text && !imgs.length) return
  sending.value = true
  pageError.value = ''

  // 乐观插入用户气泡 + 「正在思考」占位气泡
  messages.value.push({
    key: `local-u-${Date.now()}`,
    role: 'user',
    content: text || '[图片]',
    blocks: [],
    localImages: imgs.map((i) => i.dataUrl),
    imageCount: imgs.length,
    time: new Date().toISOString(),
  })
  const placeholder = {
    key: `local-a-${Date.now()}`,
    role: 'assistant',
    content: '',
    blocks: [],
    localImages: [],
    pending: true,
    failed: false,
    time: new Date().toISOString(),
  }
  messages.value.push(placeholder)
  inputText.value = ''
  pendingImages.value = []
  scrollToBottom(true)

  try {
    const reply = await assistantApi.chat({
      text,
      sessionId: activeSessionId.value,
      images: imgs.map((i) => i.file),
      // 仅当前会话绑定了专家时才传 expert（空串 api 内部不带该字段）
      expert: currentExpertKey.value,
    })
    // 新会话：服务端已建会话，记录 id 并后台刷新会话列表（标题/时间）
    if (!activeSessionId.value && reply.session_id) {
      activeSessionId.value = reply.session_id
      syncQuery()
    }
    replacePlaceholder(placeholder, {
      key: reply.message_id,
      role: 'assistant',
      content: reply.text || '',
      blocks: reply.blocks || [],
      // detection 块用 image_index 关联本次发送的本地图
      localImages: imgs.map((i) => i.dataUrl),
      time: new Date().toISOString(),
      modelUsed: reply.model_used,
      fallback: reply.fallback,
    })
    loadSessions()
  } catch (err) {
    replacePlaceholder(placeholder, {
      key: placeholder.key,
      role: 'assistant',
      content: '',
      blocks: [],
      failed: true,
      errorText: err.message || '发送失败，请稍后再试',
      time: new Date().toISOString(),
    })
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

function replacePlaceholder(placeholder, vm) {
  const idx = messages.value.indexOf(placeholder)
  if (idx >= 0) messages.value.splice(idx, 1, vm)
  else messages.value.push(vm)
}

function ask(q) {
  inputText.value = q
  send()
}

// ---------- 派单确认（走 POST /agents/runs 审批流程，本页不写库） ----------
function dispatchKey(msg, bi) {
  return `${msg.key}:${bi}`
}

function dispatchStateOf(msg, bi) {
  return dispatchState.value[dispatchKey(msg, bi)] || { status: 'idle' }
}

function dispatchButtonText(msg, bi) {
  const st = dispatchStateOf(msg, bi).status
  if (st === 'confirming') return '创建中…'
  if (st === 'done') return '已创建'
  if (st === 'error') return '重试派单'
  return '确认派单'
}

async function confirmDispatch(msg, block, bi) {
  const key = dispatchKey(msg, bi)
  const st = dispatchState.value[key]
  if (st && (st.status === 'confirming' || st.status === 'done')) return
  dispatchState.value = { ...dispatchState.value, [key]: { status: 'confirming' } }
  try {
    const run = await agentsApi.createRun({
      event_id: block.data.event_id,
      // 幂等键与消息/块位绑定：刷新后重复点击只会重放，不会重复建单
      idempotency_key: `chat-${msg.key}-${bi}`,
    })
    dispatchState.value = {
      ...dispatchState.value,
      [key]: {
        status: 'done',
        runId: run?.run_id || '',
        replay: !!run?.idempotent_replay,
      },
    }
  } catch (err) {
    dispatchState.value = {
      ...dispatchState.value,
      [key]: { status: 'error', error: err.message || '派单失败' },
    }
  }
}

// ---------- 图片输入（拖拽 / 粘贴 / 点选） ----------
function hasFiles(e) {
  return Array.from(e.dataTransfer?.types || []).includes('Files')
}

function onDragEnter(e) {
  if (!hasFiles(e)) return
  e.preventDefault()
  dragDepth.value += 1
}

function onDragOver(e) {
  if (hasFiles(e)) e.preventDefault()
}

function onDragLeave(e) {
  if (!hasFiles(e)) return
  dragDepth.value = Math.max(0, dragDepth.value - 1)
}

function onDrop(e) {
  if (!hasFiles(e)) return
  e.preventDefault()
  dragDepth.value = 0
  addFiles(e.dataTransfer.files)
}

function onPaste(e) {
  const files = Array.from(e.clipboardData?.files || [])
  if (files.length) {
    e.preventDefault()
    addFiles(files)
  }
}

function onPickFiles(e) {
  addFiles(e.target.files)
  e.target.value = ''
}

async function addFiles(fileList) {
  const files = Array.from(fileList || []).filter((f) => f.type.startsWith('image/'))
  if (!files.length) return
  for (const f of files) {
    if (pendingImages.value.length >= MAX_IMAGES) {
      pageError.value = `一次最多发送 ${MAX_IMAGES} 张图片`
      break
    }
    if (f.size > MAX_IMAGE_MB * 1024 * 1024) {
      pageError.value = `「${f.name}」超过 ${MAX_IMAGE_MB}MB 限制`
      continue
    }
    try {
      const dataUrl = await readAsDataUrl(f)
      pendingImages.value.push({ file: f, dataUrl })
    } catch {
      pageError.value = `「${f.name}」读取失败`
    }
  }
}

function readAsDataUrl(f) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('读取图片失败'))
    r.readAsDataURL(f)
  })
}

// ---------- 展示辅助 ----------
function imageFor(msg, block) {
  const idx = block?.data?.image_index
  if (typeof idx !== 'number') return ''
  return msg.localImages?.[idx] || ''
}

function chipStyle(cls) {
  const c = classColor(cls)
  return { background: `${c}22`, color: c, borderColor: `${c}55` }
}

function statTiles(d) {
  const t = d?.tasks_by_status || {}
  const dev = d?.devices_by_status || {}
  const inFlight = ['assigned', 'navigating', 'collecting'].reduce(
    (s, k) => s + Number(t[k] || 0),
    0,
  )
  const devTotal = Object.values(dev).reduce((s, v) => s + Number(v || 0), 0)
  return [
    { label: '24h 新增事件', value: d?.event_count_24h ?? 0 },
    { label: '待派任务', value: t.pending ?? 0 },
    { label: '进行中任务', value: inFlight },
    { label: '24h 完成任务', value: d?.done_tasks_24h ?? 0 },
    { label: '累计清理(kg)', value: d?.collected_kg_total ?? 0 },
    { label: '设备在线', value: `${dev.online ?? 0}/${devTotal}` },
  ]
}

function fillInput(text) {
  inputText.value = text
  composerTextarea.value?.focus()
}

async function scrollToBottom(force = false) {
  await nextTick()
  const el = msgList.value
  if (!el) return
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  if (force || nearBottom) el.scrollTop = el.scrollHeight
}

// ---------- 语音输入（浏览器原生 Web Speech API） ----------
// 刻意不引入任何 npm 依赖：项目依赖清单只有 axios/echarts/leaflet/mpegts.js/pinia/three/vue，
// 而语音输入是"可选加分项"，为它装一个语音 SDK 不值得——浏览器原生 API 零体积、零维护。
const SpeechRecognitionCtor =
  typeof window !== 'undefined'
    ? window.SpeechRecognition || window.webkitSpeechRecognition
    : null
const speechSupported = !!SpeechRecognitionCtor

const micState = ref('idle') // idle | listening | error —— 单一状态机，避免出现"既在听又出错"的幻觉
const micError = ref('')
const interimText = ref('') // 中间识别结果：只做实时预览，不写入输入框
// 非响应式：SpeechRecognition 实例不该进 Vue 响应系统（代理化会干扰浏览器原生事件对象）
let recognition = null

// 不支持时按钮 disabled 而不是隐藏：藏起来用户会以为"这产品没做语音"，
// 禁用 + title 说明原因，才是诚实降级（与本项目"不伪造状态"的铁律一致）。
const voiceButtonHint = computed(() => {
  if (!speechSupported) return '当前浏览器不支持语音输入，建议使用 Chrome / Edge'
  if (micState.value === 'listening') return '点击停止语音输入'
  return '点击开始语音输入（中文）'
})

function voiceErrorText(code) {
  switch (code) {
    case 'not-allowed':
    case 'service-not-allowed':
      return '麦克风权限被拒绝，请在浏览器站点设置里允许麦克风后重试'
    case 'no-speech':
      return '没有听到声音，请靠近麦克风再说一次'
    case 'audio-capture':
      return '未检测到可用的麦克风设备'
    case 'network':
      return '语音识别服务连接失败，请检查网络后重试'
    default:
      return `语音识别失败（${code || '未知错误'}）`
  }
}

// 用空格把语音结果"追加"进输入框，而不是覆盖：用户很可能已经手打了半句，
// 语音是补写而非重写。同时清掉尾部空白，避免出现双空格。
function appendVoiceText(text) {
  const t = String(text || '').trim()
  if (!t) return
  const cur = inputText.value
  inputText.value = cur ? `${cur.replace(/\s+$/, '')} ${t}` : t
  composerTextarea.value?.focus()
}

function createRecognition() {
  if (!SpeechRecognitionCtor) return null
  // 每次 start 都新建实例：Chrome 上复用同一实例重复 start 存在状态残留问题
  const rec = new SpeechRecognitionCtor()
  rec.lang = 'zh-CN' // 本平台面向中文场景，固定中文识别
  rec.continuous = false // 说完整句自动结束：对话输入都是短句，没必要一直挂着麦克风
  rec.interimResults = true // 边说边出中间结果，用户能立刻看到"识别到了"，不用干等一整句
  rec.maxAlternatives = 1

  rec.onstart = () => {
    micState.value = 'listening'
    micError.value = ''
  }
  rec.onresult = (event) => {
    let interim = ''
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i]
      const text = result[0]?.transcript || ''
      // 只有 isFinal 才是确定结果；中间结果只做预览，避免反复改写输入框造成抖动
      if (result.isFinal) appendVoiceText(text)
      else interim += text
    }
    interimText.value = interim
  }
  rec.onerror = (event) => {
    // 主动 abort（手动停止/组件卸载）不算错误，不打扰用户
    if (event.error === 'aborted') {
      micState.value = 'idle'
      return
    }
    micError.value = voiceErrorText(event.error)
    micState.value = 'error'
  }
  rec.onend = () => {
    // 说话自然结束也会走 onend；只有仍在 listening 时才复位，
    // 否则会把 onerror 刚设置的错误提示冲掉，用户就看不到真实原因了
    recognition = null
    interimText.value = ''
    if (micState.value === 'listening') micState.value = 'idle'
  }
  return rec
}

function startVoice() {
  if (!speechSupported) return // 按钮已 disabled，这里只是兜底防误触
  // 令牌可能在页面停留期间过期（路由守卫只在进入时校验一次）；与其让用户说完才被后端 401 拒绝，
  // 不如在开口前就如实提示。这是"显示真实错误"，不是拦着不让用。
  if (!isLoggedIn()) {
    micState.value = 'error'
    micError.value = '登录状态已失效，请重新登录后再使用语音输入'
    return
  }
  micError.value = ''
  interimText.value = ''
  micState.value = 'idle' // 清掉上一次的错误态：真实状态由 onstart/onerror 重新确定
  const rec = createRecognition()
  if (!rec) return
  recognition = rec
  try {
    rec.start()
  } catch {
    // start() 重复调用会抛 InvalidStateError：说明上一次识别其实还没真正结束
    micState.value = 'error'
    micError.value = '语音识别启动失败，请稍后重试'
  }
}

function stopVoice() {
  // 用 stop() 而非 abort()：让浏览器把已识别的部分作为 final 结果补发出来，
  // 用户手动停止时不会丢掉刚说的半句
  try {
    recognition?.stop()
  } catch {
    // 实例可能已结束，忽略
  }
}

function toggleVoice() {
  if (micState.value === 'listening') stopVoice()
  else startVoice()
}

function dismissMicError() {
  micError.value = ''
  if (micState.value === 'error') micState.value = 'idle'
}

// ---------- 生命周期 ----------
onMounted(async () => {
  await loadSessions()
  if (props.sessionId) openSession(props.sessionId)
  else if (sessions.value.length) openSession(sessions.value[0].session_id)
  // 放在 openSession 之后：openSession 里的 syncQuery 会重写 query，
  // 先消费会被它覆盖掉。
  await consumeQuerySample()
})

// ?session= 直达变化（如从其他页带参跳转）
watch(
  () => props.sessionId,
  (id, prev) => {
    if (id && id !== prev) openSession(id)
  },
)

// ?sample= 直达变化（同一路由内再点另一张样本图，组件不会重新挂载）
watch(
  () => route.query.sample,
  () => {
    void consumeQuerySample()
  },
)

watch(
  () => messages.value.length,
  () => scrollToBottom(),
)

onUnmounted(() => {
  // 卸载必须 abort()：否则识别实例会继续占着麦克风，用户离开页面后系统麦克风指示灯仍亮着，
  // 这是浏览器层面感知不到"组件已销毁"造成的真实泄露，不是样式问题
  try {
    recognition?.abort()
  } catch {
    // 忽略：实例可能已完成
  }
  recognition = null
})
</script>
<template>
  <div
    class="chat"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- 会话侧栏 -->
    <aside v-show="!sideCollapsed" class="chat__side">
      <div class="chat__side-head">
        <button class="chat__new" type="button" @click="newChat">
          <svg class="chat__new-ico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          新建对话
        </button>
      </div>
      <!-- 专家智囊团：点卡片一键开启与该专家的直聊 -->
      <div class="chat__experts">
        <div class="chat__experts-title">专家智囊团</div>
        <button
          v-for="ex in EXPERTS"
          :key="ex.key"
          type="button"
          class="expert"
          :class="{ 'expert--on': currentExpertKey === ex.key }"
          :disabled="expertBusy === ex.key"
          @click="startExpertChat(ex)"
        >
          <span class="expert__badge" :style="{ background: ex.color }">{{ ex.badge }}</span>
          <span class="expert__body">
            <span class="expert__name">{{ ex.name }}</span>
            <span class="expert__desc">{{ ex.desc }}</span>
          </span>
        </button>
      </div>
      <div class="chat__sessions">
        <div v-if="sessionsLoading" class="chat__pad text-dim">加载中…</div>
        <div v-else-if="!sessions.length" class="chat__pad text-dim">暂无历史会话</div>
        <div
          v-for="s in sessions"
          :key="s.session_id"
          class="sess"
          :class="{ 'sess--active': s.session_id === activeSessionId }"
          @click="openSession(s.session_id)"
        >
          <div class="sess__title">{{ s.title || '未命名会话' }}</div>
          <div class="sess__meta">
            <span>{{ fmtRelative(s.updated_at) }}</span>
            <button
              class="sess__del"
              :class="{ 'sess__del--confirm': confirmDeleteId === s.session_id }"
              type="button"
              :title="confirmDeleteId === s.session_id ? '再次点击确认删除' : '删除会话'"
              @click.stop="removeSession(s)"
            >
              {{ confirmDeleteId === s.session_id ? '删？' : '×' }}
            </button>
          </div>
        </div>
      </div>
    </aside>
    <!-- 对话主区 -->
    <section class="chat__main">
      <header class="chat__topbar">
        <button
          class="chat__collapse"
          type="button"
          :title="sideCollapsed ? '展开会话列表' : '收起会话列表'"
          @click="sideCollapsed = !sideCollapsed"
        >
          {{ sideCollapsed ? '»' : '«' }}
        </button>
        <span class="chat__topbar-title">智能助手</span>
        <div v-if="currentExpert" class="chat__expertbar">
          <span class="chat__expertbar-badge" :style="{ background: currentExpert.color }">{{ currentExpert.badge }}</span>
          <span class="chat__expertbar-text">当前专家：{{ currentExpert.name }}</span>
          <button class="chat__expertbar-exit" type="button" @click="exitExpertMode">退出专家模式</button>
        </div>
        <span v-else class="chat__topbar-hint text-dim">拖入或粘贴图片即可自动检测</span>
      </header>

      <div ref="msgList" class="chat__messages">
        <div class="chat__stream">
        <!-- 空状态：专家会话里必须显示该专家的身份，不能还是通用助手 -->
        <div v-if="!messages.length && !historyLoading" class="chat__empty">
          <template v-if="currentExpert">
            <div
              class="chat__empty-logo"
              :style="{ background: currentExpert.color }"
            >{{ currentExpert.badge }}</div>
            <p class="chat__empty-title">{{ currentExpert.name }}</p>
            <p>{{ currentExpert.greeting }}</p>
            <p class="text-dim">{{ currentExpert.desc }} —— 直接提问即可，回答将按该专家视角给出。</p>
            <div class="chat__prompts">
              <button
                v-for="q in currentExpert.prompts"
                :key="q"
                type="button"
                class="chat__prompt"
                @click="ask(q)"
              >
                {{ q }}
              </button>
            </div>
          </template>
          <template v-else>
            <div class="chat__empty-logo">灵</div>
            <p class="chat__empty-title">我是探海灵眸智能助手</p>
            <p>可以查询事件、统计运营数据、给出派单建议。</p>
            <p class="text-dim">直接把海漂垃圾照片拖进来，我会自动检测；或从左侧「专家智囊团」选一位专家，一键开启直聊。</p>
            <div class="chat__prompts">
              <button
                v-for="q in QUICK_PROMPTS"
                :key="q"
                type="button"
                class="chat__prompt"
                @click="ask(q)"
              >
                {{ q }}
              </button>
            </div>
          </template>
        </div>
        <div v-if="historyLoading" class="chat__pad text-dim">正在加载会话…</div>
        <!-- 消息流 -->
        <div
          v-for="m in messages"
          :key="m.key"
          class="msg"
          :class="`msg--${m.role}`"
        >
          <div
            v-if="m.role === 'assistant'"
            class="msg__avatar"
            :style="currentExpert ? { background: currentExpert.color } : {}"
            aria-hidden="true"
          >{{ currentExpert ? currentExpert.badge : '灵' }}</div>
          <div class="msg__bubble">
            <!-- 用户消息 -->
            <template v-if="m.role === 'user'">
              <div v-if="m.localImages?.length" class="msg__imgs">
                <img
                  v-for="(src, i) in m.localImages"
                  :key="i"
                  :src="src"
                  class="msg__img"
                  alt="上传图片"
                />
              </div>
              <div v-else-if="m.imageCount" class="msg__imgchip text-dim">
                🖼 图片 ×{{ m.imageCount }}（历史消息不保留原图）
              </div>
              <div v-if="m.content && m.content !== '[图片]'" class="msg__text">
                {{ m.content }}
              </div>
            </template>
            <!-- 助手消息 -->
            <template v-else>
              <div v-if="m.pending" class="msg__thinking text-dim">
                <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                正在思考
              </div>
              <div v-else-if="m.failed" class="msg__error text-danger">
                ⚠ {{ m.errorText || '发送失败' }}
              </div>
              <template v-else>
                <div v-if="m.content" class="msg__text">{{ m.content }}</div>
                <div v-for="(b, bi) in m.blocks" :key="bi" class="blk">
                  <template v-if="b.type === 'detection'">
                    <div class="blk__head text-dim">第 {{ (b.data.image_index ?? 0) + 1 }} 张图 · 发现 {{ b.data.count ?? 0 }} 个目标</div>
                    <DetectionCanvas v-if="imageFor(m, b)" :src="imageFor(m, b)" :detections="b.data.detections || []" />
                    <ul v-if="(b.data.detections || []).length" class="det-list"><li v-for="(d, di) in b.data.detections" :key="di" class="det-item"><i class="det-dot" :style="{ background: classColor(d.class) }"></i>{{ classLabel(d.class) }}<span class="text-dim"> {{ (Number(d.confidence || 0) * 100).toFixed(0) }}%</span></li></ul>
                  </template>
                  <template v-else-if="b.type === 'event_card'"><div class="ev"><div class="ev__head"><span class="mono">{{ b.data.event_id }}</span><span class="chip" :style="chipStyle(b.data.main_class)">{{ b.data.main_class_label || classLabel(b.data.main_class) }}</span><span class="text-dim">{{ b.data.status_label }}</span></div><div class="ev__row text-sub">{{ fmtShortTime(b.data.event_time) }} · ({{ b.data.lng }}, {{ b.data.lat }})</div><div class="ev__row text-sub">目标 {{ b.data.det_count }} 个 · 最高置信度 {{ (Number(b.data.max_confidence || 0) * 100).toFixed(0) }}%</div></div></template>
                  <template v-else-if="b.type === 'event_list'"><div class="evl"><div class="blk__head text-dim">最近 {{ b.data.hours ?? 24 }} 小时 · 共 {{ b.data.total ?? 0 }} 起</div><div v-for="e in (b.data.items || [])" :key="e.event_id" class="evl__row" @click="fillInput(e.event_id)"><i class="det-dot" :style="{ background: classColor(e.main_class) }"></i><span class="mono">{{ e.event_id }}</span><span class="text-dim">{{ e.main_class_label || classLabel(e.main_class) }}</span><span class="text-dim evl__time">{{ fmtRelative(e.event_time) }}</span></div></div></template>
                  <template v-else-if="b.type === 'stats'"><div class="tiles"><div v-for="t in statTiles(b.data)" :key="t.label" class="tile"><div class="tile__v">{{ t.value }}</div><div class="tile__l text-dim">{{ t.label }}</div></div></div></template>
                  <template v-else-if="b.type === 'dispatch_suggest'"><div class="dsp"><div class="blk__head text-dim">派单建议 · <span class="mono">{{ b.data.event_id }}</span></div><template v-if="b.data.found"><div v-for="c in (b.data.candidates || [])" :key="c.device_id" class="dsp__row"><span>{{ c.name || c.device_id }}</span><span class="text-dim">约 {{ Math.round(c.distance_m || 0) }}m</span></div><div v-if="b.data.hint" class="dsp__hint text-dim">{{ b.data.hint }}</div><div class="dsp__foot"><button v-if="canWrite()" class="btn btn--primary" type="button" :disabled="['confirming','done'].includes(dispatchStateOf(m, bi).status)" @click="confirmDispatch(m, b, bi)">{{ dispatchButtonText(m, bi) }}</button><span v-if="dispatchStateOf(m, bi).status === 'done'" class="text-dim dsp__run">run: <span class="mono">{{ dispatchStateOf(m, bi).runId }}</span><template v-if="dispatchStateOf(m, bi).replay">（幂等重放）</template></span><span v-if="dispatchStateOf(m, bi).status === 'error'" class="text-danger"> {{ dispatchStateOf(m, bi).error }}</span></div></template><div v-else class="text-dim">{{ b.data.hint || '未找到该事件' }}</div></div></template>
                  <template v-else-if="b.type === 'error'"><div class="text-danger">⚠ {{ b.data.message }}</div></template>
                </div>
                <div class="msg__meta text-dim">
                  {{ fmtShortTime(m.time) }}
                  <span v-if="m.fallback"> · 规则兜底</span>
                  <span v-else-if="m.modelUsed"> · {{ m.modelUsed }}</span>
                </div>
              </template>
            </template>
          </div>
        </div>
        </div>
      </div>
      <!-- 页面错误条 -->
      <div v-if="pageError" class="chat__err text-danger">⚠ {{ pageError }}</div>
      <!-- 输入区 -->
      <footer class="composer">
        <div v-if="pendingImages.length" class="composer__previews"><div v-for="(im, i) in pendingImages" :key="i" class="pv"><img :src="im.dataUrl" class="pv__img" alt="待发送"/><button class="pv__del" type="button" @click="pendingImages.splice(i, 1)">×</button></div></div>
        <!-- 语音状态：聆听中的实时预览 / 真实错误提示。二选一，绝不共存，避免"正在聆听"和报错同时出现 -->
        <div v-if="micState === 'listening'" class="composer__voice text-dim">
          <span class="composer__voice-dot"></span>
          <span>正在聆听…</span>
          <span v-if="interimText" class="composer__voice-live">{{ interimText }}</span>
        </div>
        <div v-else-if="micState === 'error'" class="composer__voice composer__voice--err">
          <span class="text-danger">⚠ {{ micError }}</span>
          <button class="composer__voice-close" type="button" @click="dismissMicError">知道了</button>
        </div>
        <div class="composer__row">
          <textarea
            ref="composerTextarea"
            v-model="inputText"
            class="composer__input"
            rows="1"
            :placeholder="currentExpert ? `向${currentExpert.name}提问…` : '输入消息，或拖入/粘贴图片…'"
            @keydown.enter.exact.prevent="send"
            @paste="onPaste"
          ></textarea>
          <div class="composer__actions">
            <button class="composer__icon" type="button" title="添加图片" aria-label="添加图片" @click="fileInput?.click()">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M4 17l4.5-4.5 3 3L15 12l5 5"/></svg>
            </button>
            <!-- 语音按钮：不支持 SpeechRecognition 的浏览器（Firefox 绝大多数版本、部分 Safari）保持 disabled 并说明原因 -->
            <button
              class="composer__icon composer__mic"
              :class="{ 'composer__mic--on': micState === 'listening' }"
              type="button"
              :disabled="!speechSupported"
              :title="voiceButtonHint"
              :aria-label="voiceButtonHint"
              :aria-pressed="micState === 'listening'"
              @click="toggleVoice"
            >
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
            </button>
            <button
              class="composer__send"
              type="button"
              :disabled="sending || (!inputText.trim() && !pendingImages.length)"
              :title="sending ? '发送中…' : '发送'"
              aria-label="发送"
              @click="send"
            >
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M6 11l6-6 6 6"/></svg>
            </button>
          </div>
        </div>
        <div class="composer__hint">Enter 发送 · Shift+Enter 换行 · 支持拖入或粘贴图片</div>
        <input ref="fileInput" type="file" accept="image/*" multiple hidden @change="onPickFiles" />
      </footer>
    </section>
    <div v-if="dragDepth > 0" class="chat__dropmask"><div class="chat__dropbox">松开鼠标，把图片发给我检测</div></div>
  </div>
</template>
<style scoped>
/* Codex 风格：近黑底、极细分割线、弱化边框、大量留白；强调色用平台青绿 #18a999 */
.chat {
  --cx-accent: #18a999;
  --cx-accent-dim: #12907f;
  --cx-line: var(--separator);
  display: flex;
  height: 100%;
  position: relative;
  background: var(--bg-page);
  color: var(--text-main);
}

/* ---------- 侧栏（固定 260px） ---------- */
.chat__side { width: 260px; flex-shrink: 0; display: flex; flex-direction: column; border-right: 1px solid var(--cx-line); background: var(--bg-panel); }
.chat__side-head { padding: 12px; }
.chat__new {
  width: 100%; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  min-height: 40px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-panel-2); color: var(--text-main); font-size: 13px; font-weight: 500;
  cursor: pointer; transition: border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.chat__new:hover { border-color: var(--cx-accent); color: var(--cx-accent); }
.chat__new-ico { flex-shrink: 0; }

/* 专家智囊团 */
.chat__experts { padding: 2px 12px 12px; border-bottom: 1px solid var(--cx-line); }
.chat__experts-title { font-size: 11px; letter-spacing: 0.08em; color: var(--text-dim); margin: 2px 2px 8px; }
.expert {
  width: 100%; display: flex; align-items: center; gap: 10px; padding: 8px 10px; margin-bottom: 4px;
  border: 1px solid transparent; border-radius: 10px; background: transparent; text-align: left; cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.expert:hover { background: var(--bg-hover); }
.expert:disabled { opacity: 0.5; cursor: progress; }
.expert--on { background: var(--bg-active); border-color: var(--cx-accent); }
.expert__badge { width: 28px; height: 28px; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; border-radius: 8px; color: #fff; font-size: 13px; font-weight: 600; }
.expert__body { display: flex; flex-direction: column; min-width: 0; }
.expert__name { font-size: 13px; color: var(--text-main); }
.expert__desc { font-size: 11px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.chat__sessions { flex: 1; min-height: 0; overflow-y: auto; padding: 6px 8px; }
.chat__pad { padding: 16px 12px; }
.sess { padding: 8px 10px; border-radius: 8px; cursor: pointer; transition: background var(--dur) var(--ease); }
.sess:hover { background: var(--bg-hover); }
.sess--active { background: var(--bg-active); }
.sess__title { font-size: 13px; color: var(--text-main); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess__meta { display: flex; justify-content: space-between; align-items: center; margin-top: 3px; font-size: 11px; color: var(--text-dim); }
.sess__del { border: none; background: none; color: var(--text-dim); cursor: pointer; padding: 0 4px; font-size: 14px; line-height: 1; }
.sess__del:hover { color: var(--c-danger); }
.sess__del--confirm { color: var(--c-danger); font-size: 12px; }

/* ---------- 主区 ---------- */
.chat__main { flex: 1; min-width: 0; display: flex; flex-direction: column; background: var(--bg-page); }
.chat__topbar { display: flex; align-items: center; gap: 10px; padding: 12px 20px; border-bottom: 1px solid var(--cx-line); background: var(--bg-page); }
.chat__collapse { border: 1px solid var(--border); background: transparent; color: var(--text-sub); cursor: pointer; border-radius: 8px; width: 28px; height: 28px; }
.chat__collapse:hover { border-color: var(--cx-accent); color: var(--cx-accent); }
.chat__topbar-title { font-weight: 600; color: var(--text-main); font-size: 14px; }
.chat__topbar-hint { font-size: 12px; margin-left: auto; }
.chat__expertbar { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.chat__expertbar-badge { width: 22px; height: 22px; border-radius: 6px; color: #fff; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; }
.chat__expertbar-text { font-size: 12px; color: var(--text-sub); }
.chat__expertbar-exit { border: 1px solid var(--border); background: transparent; color: var(--text-dim); border-radius: 999px; padding: 3px 10px; font-size: 12px; cursor: pointer; transition: color var(--dur) var(--ease), border-color var(--dur) var(--ease); }
.chat__expertbar-exit:hover { color: var(--cx-accent); border-color: var(--cx-accent); }

/* ---------- 消息流（居中 760px） ---------- */
.chat__messages { flex: 1; min-height: 0; overflow-y: auto; }
.chat__stream { max-width: 760px; margin: 0 auto; padding: 24px 24px 32px; }
.chat__empty { text-align: center; padding: 56px 16px; color: var(--text-sub); }
.chat__empty-logo { width: 46px; height: 46px; margin: 0 auto 16px; display: flex; align-items: center; justify-content: center; border-radius: 12px; background: var(--cx-accent); color: #fff; font-size: 22px; font-weight: 600; }
.chat__empty-title { color: var(--text-main); font-size: 16px; font-weight: 600; margin-bottom: 8px; }
.chat__empty p { font-size: 13px; line-height: 1.7; }
.chat__prompts { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 20px; }
.chat__prompt { border: 1px solid var(--border); background: var(--bg-panel); color: var(--text-sub); border-radius: 999px; padding: 6px 14px; cursor: pointer; font-size: 13px; transition: all var(--dur) var(--ease); }
.chat__prompt:hover { border-color: var(--cx-accent); color: var(--cx-accent); }

.msg { display: flex; gap: 12px; margin-bottom: 22px; }
.msg--user { justify-content: flex-end; }
.msg__avatar { width: 26px; height: 26px; flex-shrink: 0; margin-top: 2px; display: inline-flex; align-items: center; justify-content: center; border-radius: 8px; background: var(--cx-accent); color: #fff; font-size: 13px; font-weight: 600; }
.msg__bubble { min-width: 0; }
.msg--assistant .msg__bubble { flex: 1; max-width: 100%; }
.msg--user .msg__bubble { max-width: 76%; padding: 10px 14px; border-radius: 16px 16px 4px 16px; background: var(--bg-panel-2); border: 1px solid var(--panel-border); }
.msg__text { white-space: pre-wrap; word-break: break-word; color: var(--text-main); font-size: 14px; line-height: 1.7; }
.msg__imgs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.msg__img { max-width: 160px; max-height: 160px; border-radius: var(--radius); }
.msg__imgchip { font-size: 12px; margin-bottom: 4px; }
.msg__meta { margin-top: 8px; font-size: 11px; }
.msg__thinking { display: flex; align-items: center; gap: 4px; font-size: 13px; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-dim); animation: blink 1.2s infinite; }
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 80%, 100% { opacity: 0.3; } 40% { opacity: 1; } }
.blk { margin-top: 10px; }
.blk__head { font-size: 12px; margin-bottom: 6px; }
.det-list { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
.det-item { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-sub); background: var(--bg-panel-2); border: 1px solid var(--border); border-radius: 999px; padding: 3px 10px; }
.det-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.chip { font-size: 11px; border: 1px solid; border-radius: 999px; padding: 1px 8px; }
.ev, .evl, .dsp, .tiles { background: var(--bg-panel-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px; }
.ev__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ev__row { font-size: 12px; margin-top: 4px; }
.evl__row { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 13px; color: var(--text-main); cursor: pointer; }
.evl__row:hover { color: var(--cx-accent); }
.evl__time { margin-left: auto; font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.tile { text-align: center; padding: 8px 4px; background: var(--bg-panel); border-radius: var(--radius); }
.tile__v { font-size: 18px; font-weight: 600; color: var(--cx-accent); }
.tile__l { font-size: 11px; margin-top: 2px; }
.dsp__row { display: flex; justify-content: space-between; font-size: 13px; color: var(--text-main); padding: 4px 0; }
.dsp__hint { font-size: 12px; margin-top: 6px; }
.dsp__foot { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.dsp__run { font-size: 12px; }
.chat__err { padding: 8px 20px; font-size: 13px; border-top: 1px solid var(--cx-line); }

/* ---------- 输入区（圆角 composer 悬浮于内容下方） ---------- */
.composer { flex-shrink: 0; padding: 0 20px 14px; background: var(--bg-page); }
.composer__row { max-width: 760px; margin: 0 auto; display: flex; align-items: flex-end; gap: 8px; padding: 8px 8px 8px 14px; border: 1px solid var(--border); border-radius: 16px; background: var(--bg-panel); transition: border-color var(--dur) var(--ease); }
.composer__row:focus-within { border-color: var(--cx-accent); }
.composer__input { flex: 1; min-width: 0; resize: none; max-height: 160px; padding: 6px 0; border: none; background: transparent; color: var(--text-main); font-size: 14px; font-family: inherit; line-height: 1.6; }
.composer__input:focus { outline: none; }
.composer__input::placeholder { color: var(--text-dim); }
.composer__actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.composer__icon { width: 34px; height: 34px; border: none; border-radius: 50%; background: transparent; color: var(--text-dim); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; transition: background var(--dur) var(--ease), color var(--dur) var(--ease); }
.composer__icon:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-main); }
.composer__icon:disabled { opacity: 0.42; cursor: not-allowed; }
/* 聆听中用主色描边 + 脉冲环：状态靠"动效"表达，不依赖具体颜色值 */
.composer__mic--on { color: var(--cx-accent); background: var(--bg-active); animation: mic-pulse 1.4s ease-out infinite; }
@keyframes mic-pulse {
  0% { box-shadow: 0 0 0 0 var(--c-primary-glow); }
  70% { box-shadow: 0 0 0 8px rgba(0, 0, 0, 0); }
  100% { box-shadow: 0 0 0 0 rgba(0, 0, 0, 0); }
}
.composer__send { width: 34px; height: 34px; border: none; border-radius: 50%; background: var(--cx-accent); color: #fff; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background var(--dur) var(--ease), opacity var(--dur) var(--ease); }
.composer__send:hover:not(:disabled) { background: var(--cx-accent-dim); }
.composer__send:disabled { opacity: 0.35; cursor: not-allowed; }
.composer__previews { max-width: 760px; margin: 0 auto 8px; display: flex; flex-wrap: wrap; gap: 8px; }
.composer__voice { max-width: 760px; margin: 0 auto 8px; display: flex; align-items: center; gap: 8px; font-size: 12px; }
.composer__voice-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c-danger); flex-shrink: 0; animation: blink 1.2s infinite; }
.composer__voice-live { color: var(--cx-accent); }
.composer__voice--err { justify-content: space-between; }
.composer__voice-close { border: none; background: none; color: var(--text-dim); cursor: pointer; font-size: 12px; padding: 0; flex-shrink: 0; }
.composer__voice-close:hover { color: var(--text-sub); }
.composer__hint { max-width: 760px; margin: 8px auto 0; font-size: 11px; color: var(--text-dim); text-align: center; }
.pv { position: relative; }
.pv__img { width: 56px; height: 56px; object-fit: cover; border-radius: var(--radius); border: 1px solid var(--border); }
.pv__del { position: absolute; top: -6px; right: -6px; width: 18px; height: 18px; border-radius: 50%; border: none; background: var(--c-danger); color: #fff; font-size: 12px; line-height: 1; cursor: pointer; }
.chat__dropmask { position: absolute; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5); display: flex; align-items: center; justify-content: center; pointer-events: none; }
.chat__dropbox { border: 1.5px dashed var(--cx-accent); border-radius: var(--radius-xl); padding: 28px 40px; color: var(--cx-accent); font-size: 15px; background: var(--bg-panel); }

@media (max-width: 720px) {
  .chat__side { width: 220px; }
  .chat__stream { padding: 18px 14px 24px; }
  .composer { padding: 0 12px 12px; }
  .msg--user .msg__bubble { max-width: 88%; }
}
</style>

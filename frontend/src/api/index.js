/**
 * 接口封装 —— 与后端 app/api/v1/*.py 一一对应。
 */
import http from './http'

// ---------- 事件 ----------
export const eventsApi = {
  /** 事件列表 */
  list(params = {}) {
    return http.get('/events', { params })
  },

  /** 事件详情 */
  detail(eventId) {
    return http.get(`/events/${eventId}`)
  },

  /** 人工更新事件状态（忽略误报 / 确认清理） */
  updateStatus(eventId, payload) {
    return http.patch(`/events/${eventId}`, payload)
  },

  /** 热力图网格聚合 */
  heatmap(params = {}) {
    return http.get('/events/heatmap', { params })
  },

  /** 事件上报（边缘盒 HTTP 通道，MQTT 为主、HTTP 为备份） */
  report(payload) {
    return http.post('/events', payload)
  },
}

// ---------- 任务（工单） ----------
export const tasksApi = {
  list(params = {}) {
    return http.get('/tasks', { params })
  },

  detail(taskId) {
    return http.get(`/tasks/${taskId}`)
  },

  /** 人工创建任务 */
  create(payload) {
    return http.post('/tasks', payload)
  },

  /** 更新任务状态（走后端状态机校验） */
  updateStatus(taskId, payload) {
    return http.patch(`/tasks/${taskId}`, payload)
  },

  /** 手动触发补派 */
  dispatchPending() {
    return http.post('/tasks/dispatch/pending')
  },

  /** 导出工单 CSV（返回 Blob） */
  export(params = {}) {
    return http.get('/tasks/export', { params, responseType: 'blob' })
  },
}

// ---------- 工单执行仿真 ----------
export const simulationApi = {
  /** 当前运行快照；无运行会话且有历史轨迹时返回 state=history */
  status(taskId) {
    return http.get(`/simulations/${taskId}`)
  },

  /** 已落库的 t_track 轨迹 */
  trajectory(taskId) {
    return http.get(`/simulations/${taskId}/trajectory`)
  },

  start(taskId, payload = { speed: 1 }) {
    return http.post(`/simulations/${taskId}/start`, payload)
  },

  pause(taskId) {
    return http.post(`/simulations/${taskId}/pause`)
  },

  resume(taskId) {
    return http.post(`/simulations/${taskId}/resume`)
  },

  step(taskId) {
    return http.post(`/simulations/${taskId}/step`)
  },

  speed(taskId, speed) {
    return http.post(`/simulations/${taskId}/speed`, { speed })
  },

  stop(taskId, payload = {}) {
    return http.post(`/simulations/${taskId}/stop`, payload)
  },
}

// ---------- 设备 ----------
export const devicesApi = {
  list(params = {}) {
    return http.get('/devices', { params })
  },

  detail(deviceId) {
    return http.get(`/devices/${deviceId}`)
  },

  /** 取视频流地址（go2rtc 的 flv / webrtc / hls） */
  stream(deviceId) {
    return http.get(`/devices/${deviceId}/stream`)
  },
}

// ---------- 机器人 ----------
export const robotsApi = {
  list() {
    return http.get('/robots')
  },

  detail(robotId) {
    return http.get(`/robots/${robotId}`)
  },
}

// ---------- 统计 ----------
export const statsApi = {
  /** 大屏指标卡 */
  dashboard() {
    return http.get('/stats/dashboard')
  },

  /** 类别分布（饼图） */
  classes(hours = 24) {
    return http.get('/stats/classes', { params: { hours } })
  },

  /** 事件趋势（折线） */
  trend(hours = 24) {
    return http.get('/stats/trend', { params: { hours } })
  },

  /** 站内通知（最近告警 + 工单动态） */
  notifications(params = {}) {
    return http.get('/stats/notifications', { params })
  },
}

// ---------- 报表 ----------
export const reportsApi = {
  daily(params = {}) {
    return http.get('/reports/daily', { params })
  },

  summary(days = 7) {
    return http.get('/reports/summary', { params: { days } })
  },

  /** 导出日报表 CSV（返回 Blob） */
  export(params = {}) {
    return http.get('/reports/export', { params, responseType: 'blob' })
  },
}

// ---------- 认证 ----------
export const authApi = {
  login(username, password) {
    return http.post('/auth/login', { username, password })
  },

  me() {
    return http.get('/auth/me')
  },
}

// ---------- AI（智能体 + 图片分析） ----------
export const aiApi = {
  /** 智能体列表（能力定义 + 真实运行状态 idle/unavailable/running） */
  agents() {
    return http.get('/ai/agents')
  },

  /** 图片分析（OpenCV 检测） */
  analyzeImage(file) {
    const fd = new FormData()
    fd.append('file', file)
    return http.post('/ai/analyze-image', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}

// ---------- Agent（智能体运行时控制台，契约见 Harness 手册 3.8） ----------
// 所有接口均返回 ApiResponse 信封（code=0 成功），错误码 6001-6009，
// 前端按 code 映射提示，不得把非 0 code 当成功渲染。
export const agentsApi = {
  /** 运行时状态：{ state: idle|running|... } */
  runtimeStatus() {
    return http.get('/agents/runtime/status')
  },

  /** 运行列表：PageResult{ items: AgentRunOut[], meta{ total,page,page_size } }，按创建时间倒序 */
  listRuns(params = {}) {
    return http.get('/agents/runs', { params })
  },

  /** 运行详情（含真实 status，如 waiting_approval） */
  runDetail(runId) {
    return http.get(`/agents/runs/${runId}`)
  },

  /** 步骤轨迹（step_type/status/error_code/decision_summary 等） */
  runSteps(runId) {
    return http.get(`/agents/runs/${runId}/steps`)
  },

  /** 启动事件处置运行（body 含 idempotency_key；重复提交返回既有 run 且 idempotent_replay=true） */
  createRun(payload) {
    return http.post('/agents/runs', payload)
  },

  /** 取消运行（终态 run 返回 6004） */
  cancelRun(runId, payload = {}) {
    return http.post(`/agents/runs/${runId}/cancel`, payload)
  },

  /** 工具目录（name/version/risk_level/idempotent/allowed_roles） */
  tools() {
    return http.get('/agents/tools')
  },

  /** 审批队列 */
  approvals() {
    return http.get('/agents/approvals')
  },

  /** 审批决定（decision: approved|rejected；approver/admin 角色，否则 403） */
  decideApproval(approvalId, payload) {
    return http.post(`/agents/approvals/${approvalId}/decide`, payload)
  },

  /** 最新评测产物（文件缺失返回 6008，data 为空态） */
  latestEval() {
    return http.get('/agents/evals/latest')
  },
}

// ---------- 对话助手（自由对话问答 + 拖图/粘贴即分析，与 /agents 派单控制台并行） ----------
// 全部返回 ApiResponse 信封（code=0 成功）；错误码 8xxx 对话段 + 复用 5001 组件不可用。
// 工具全只读：派单不在此写库，仅生成建议卡，确认仍走 agentsApi.createRun（含审批）。
export const assistantApi = {
  /**
   * 一轮对话：文字 + 0..n 张图片（multipart/form-data）。
   * images 为 File 数组；后端逐张做 OpenCV 检测，返回 AssistantReplyOut
   * { message_id, session_id, text, blocks[], model_used, fallback }。
   */
  chat({ text = '', sessionId = '', images = [] } = {}) {
    const fd = new FormData()
    if (text) fd.append('text', text)
    if (sessionId) fd.append('session_id', sessionId)
    for (const file of images) fd.append('images', file)
    return http.post('/assistant/chat', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  /** 当前用户会话列表（按最近更新排序） */
  listSessions() {
    return http.get('/assistant/sessions')
  },

  /** 新建空会话（title 可空，服务端给默认值） */
  createSession(payload = {}) {
    return http.post('/assistant/sessions', payload)
  },

  /** 会话历史消息（按时间正序分页）：{ items, total, page, page_size } */
  listMessages(sessionId, params = {}) {
    return http.get(`/assistant/sessions/${sessionId}/messages`, { params })
  },

  /** 删除会话（级联删消息，仅属主） */
  deleteSession(sessionId) {
    return http.delete(`/assistant/sessions/${sessionId}`)
  },
}

// ---------- 领域知识智能体（资产 / 本体 / 多跳检索 / 决策证据） ----------
export const knowledgeApi = {
  /** 知识资产分页列表 */
  listAssets(params = {}) {
    return http.get('/knowledge/assets', { params })
  },

  /** 资产详情与不可变版本 */
  assetDetail(assetId) {
    return http.get(`/knowledge/assets/${assetId}`)
  },

  /** 登记多模态知识资产 */
  createAsset(payload) {
    return http.post('/knowledge/assets', payload)
  },

  /** 追加资产版本 */
  appendAssetVersion(assetId, payload) {
    return http.post(`/knowledge/assets/${assetId}/versions`, payload)
  },

  /** 本体版本列表 */
  listOntologyVersions() {
    return http.get('/knowledge/ontology/versions')
  },

  /** 创建本体版本 */
  createOntologyVersion(payload) {
    return http.post('/knowledge/ontology/versions', payload)
  },

  /** 本体节点 */
  ontologyNodes(versionId) {
    return http.get(`/knowledge/ontology/versions/${versionId}/nodes`)
  },

  /** 本体关系 */
  ontologyRelations(versionId) {
    return http.get(`/knowledge/ontology/versions/${versionId}/relations`)
  },

  /** 半自动抽取候选节点与关系 */
  extractOntology(payload) {
    return http.post('/knowledge/ontology/extract', payload)
  },

  /** 审核节点 */
  reviewNode(nodeId, payload) {
    return http.post(`/knowledge/ontology/nodes/${nodeId}/review`, payload)
  },

  /** 审核关系 */
  reviewRelation(relationId, payload) {
    return http.post(`/knowledge/ontology/relations/${relationId}/review`, payload)
  },

  /** 发布已审核本体 */
  publishOntology(versionId) {
    return http.post(`/knowledge/ontology/versions/${versionId}/publish`)
  },

  /** 跨文档多跳检索 */
  search(payload) {
    return http.post('/knowledge/search', payload)
  },

  /** 创建可追溯决策轨迹；未显式传证据时由后端自动检索 */
  createDecision(payload) {
    return http.post('/knowledge/decisions', payload)
  },

  /** 决策轨迹分页列表 */
  listDecisions(params = {}) {
    return http.get('/knowledge/decisions', { params })
  },

  /** 决策证据链 */
  decisionEvidence(traceId) {
    return http.get(`/knowledge/decisions/${traceId}/evidence`)
  },
}

/** 把 Blob 保存为本地文件（导出 CSV 用） */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

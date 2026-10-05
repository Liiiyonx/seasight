/**
 * 实时数据 store。
 *
 * 职责：把 WebSocket 推送与轮询结果统一收敛到这里，
 * 各页面只读 store，不各自建连接 —— 否则开三个页面就有三条 WebSocket。
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { statsApi, eventsApi, devicesApi, robotsApi } from '@/api'
import { useRealtime } from '@/utils/realtime'

export const useRealtimeStore = defineStore('realtime', () => {
  // ---------- 状态 ----------
  const stats = ref({})
  const recentEvents = ref([])       // 最新事件（大屏滚动列表）
  const devices = ref([])
  const robots = ref([])
  const heatmap = ref([])
  const taskFeed = ref([])           // 工单动态
  const telemetryFeed = ref([])      // 机器人遥测原文（仿真报文流）

  const loading = ref(false)
  const error = ref('')
  const connected = ref(false)

  const MAX_FEED = 80
  const MAX_TELEMETRY = 200

  // ---------- 派生 ----------
  const onlineDevices = computed(() => devices.value.filter((d) => d.status === 'online'))
  const onlineRobots = computed(() => robots.value.filter((r) => r.status === 'online'))

  // ---------- WebSocket ----------
  const ws = useRealtime({
    onOpen() {
      connected.value = true
      error.value = ''
    },
    onMessage(msg) {
      if (msg.type === 'new_event' && msg.data) {
        pushEvent(msg.data)
      } else if (msg.type === 'task_update' && msg.data) {
        pushTask(msg.data)
      } else if (msg.type === 'robot_status' && msg.data) {
        patchRobot(msg.data)
        pushTelemetry(msg.data, msg.ts)
      }
    },
    onError() {
      /* 重连由 realtime.js 处理，这里不弹错，避免刷屏 */
    },
  })

  // 同步 realtime.js 的连接状态
  ws.connected && null
  const wsConnected = ws.connected

  function startRealtime() {
    ws.connect()
  }

  function stopRealtime() {
    ws.close()
  }

  function pushEvent(data) {
    // 去重：同 event_id 只保留一条
    if (recentEvents.value.some((e) => e.event_id === data.event_id)) return
    recentEvents.value.unshift({ ...data, _isNew: true })
    if (recentEvents.value.length > MAX_FEED) {
      recentEvents.value = recentEvents.value.slice(0, MAX_FEED)
    }
    // 2 秒后去掉新事件高亮标记
    setTimeout(() => {
      const item = recentEvents.value.find((e) => e.event_id === data.event_id)
      if (item) item._isNew = false
    }, 2000)
  }

  function pushTask(data) {
    if (taskFeed.value.some((t) => t.task_id === data.task_id && t.status === data.status)) {
      return
    }
    taskFeed.value.unshift({ ...data, _ts: Date.now() })
    if (taskFeed.value.length > MAX_FEED) {
      taskFeed.value = taskFeed.value.slice(0, MAX_FEED)
    }
  }

  function patchRobot(data) {
    const idx = robots.value.findIndex((r) => r.robot_id === data.robot_id)
    if (idx >= 0) {
      robots.value[idx] = { ...robots.value[idx], ...data }
      return 'updated'
    }
    // 仿真机器人可能在机器人列表首次加载完成后才出现；不能像旧实现那样
    // 直接丢弃，否则地图与右侧资源面板会一直看不到遥测来源。
    if (data?.robot_id) {
      robots.value = [data, ...robots.value]
      return 'inserted'
    }
    return 'ignored'
  }

  function pushTelemetry(data, receivedAt = null) {
    if (!data?.robot_id) return
    const item = {
      ...data,
      _receivedAt: receivedAt || new Date().toISOString(),
      _key: `${data.robot_id}-${receivedAt || Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    }
    telemetryFeed.value.unshift(item)
    if (telemetryFeed.value.length > MAX_TELEMETRY) {
      telemetryFeed.value = telemetryFeed.value.slice(0, MAX_TELEMETRY)
    }
  }

  // ---------- 数据加载 ----------
  async function loadOverview({ silent = false } = {}) {
    if (!silent) loading.value = true
    try {
      const [s, d, r] = await Promise.all([
        statsApi.dashboard(),
        devicesApi.list(),
        robotsApi.list(),
      ])
      stats.value = s || {}
      // ★ 同上：显式判 Array。接口返回 {} 时 `d?.items || d` 会因{}
      //   是真值而返回 {}，下游渲染/ 展开会抛。
      devices.value = Array.isArray(d?.items) ? d.items : Array.isArray(d) ? d : []
      robots.value = Array.isArray(r?.items) ? r.items : Array.isArray(r) ? r : []
      error.value = ''
    } catch (err) {
      error.value = err.message || '数据加载失败'
    } finally {
      loading.value = false
    }
  }

  async function loadEvents({ hours = 24, limit = 50 } = {}) {
    try {
      const res = await eventsApi.list({ hours, page: 1, page_size: limit })
      //★ 必须显式判 Array：`res?.items || res || []` 在 res = {} 时
      //   会因 {} 是真值而返回 {}，随后 for...of {} 直接抛
      //   "items is not iterable"。接口少给一个字段就能让整页白屏。
      const raw = res?.items ?? res
      const items = Array.isArray(raw) ? raw : []
      // 保留已经通过 WebSocket 推来的高亮状态
      const seen = new Set(recentEvents.value.map((e) => e.event_id))
      const merged = [...recentEvents.value]
      for (const it of items) {
        if (!seen.has(it.event_id)) merged.push(it)
      }
      recentEvents.value = merged
        .sort((a, b) => new Date(b.event_time || 0) - new Date(a.event_time || 0))
        .slice(0, MAX_FEED)
    } catch (err) {
      error.value = err.message
    }
  }

  async function loadHeatmap({ hours = 24, gridSize = 500 } = {}) {
    try {
      const res = await eventsApi.heatmap({ hours, grid_size: gridSize })
      heatmap.value = Array.isArray(res) ? res : []
    } catch (err) {
      // 热力图失败不阻断大屏其余部分
      console.warn('[热力图] 加载失败：', err.message)
    }
  }

  async function refreshAll() {
    await Promise.all([
      loadOverview({ silent: true }),
      loadEvents(),
      loadHeatmap(),
    ])
  }

  return {
    // state
    stats, recentEvents, devices, robots, heatmap, taskFeed, telemetryFeed,
    loading, error, wsConnected, connected,
    // derived
    onlineDevices, onlineRobots,
    // actions
    startRealtime, stopRealtime, refreshAll,
    loadOverview, loadEvents, loadHeatmap,
    pushEvent, pushTask, patchRobot, pushTelemetry,
  }
})

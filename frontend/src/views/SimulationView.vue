<template>
  <div class="sim-page">
    <header class="sim-head panel">
      <div class="sim-head__identity">
        <button
          class="sim-head__back"
          type="button"
          title="返回工单看板"
          aria-label="返回工单看板"
          @click="router.push('/tasks')"
        >
          ‹
        </button>
        <div class="sim-head__title">
          <span>工单执行仿真</span>
          <h1>{{ taskId || '—' }}</h1>
        </div>
        <span class="sim-state" :class="`sim-state--${stateTone}`">
          {{ stateLabel }}
        </span>
      </div>

      <div class="sim-head__facts">
        <span>机器人 <strong>{{ robotId || '未绑定' }}</strong></span>
        <span>阶段 <strong>{{ phaseLabel }}</strong></span>
        <span>进度 <strong>{{ Math.round(progress * 100) }}%</strong></span>
      </div>

      <div class="sim-head__actions">
        <button
          v-if="eventId"
          class="sim-head__action"
          type="button"
          @click="openEvent"
        >
          关联事件
        </button>
        <button class="sim-head__action" type="button" @click="router.push('/tasks')">
          返回看板
        </button>
      </div>
    </header>

    <div class="sim-layout">
      <section class="sim-map panel">
        <div class="sim-map__viewbar">
          <span>作业可视化</span>
          <div class="sim-map__switch" role="tablist" aria-label="可视化视图">
            <button
              type="button"
              role="tab"
              :aria-selected="viewMode === 'map'"
              :class="{ 'is-active': viewMode === 'map' }"
              @click="viewMode = 'map'"
            >
              地图
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="viewMode === 'console'"
              :class="{ 'is-active': viewMode === 'console' }"
              @click="viewMode = 'console'"
            >
              仿真台
            </button>
          </div>
        </div>
        <div class="sim-map__stage">
          <SimulationMap
            v-if="viewMode === 'map'"
            ref="mapRef"
            :position="mapPosition"
            :heading="Number(snapshot.heading || 0)"
            :home="mapHome"
            :target="mapTarget"
            :route="snapshot.route || []"
            :remaining="snapshot.remaining || []"
            :phase="snapshot.phase || fallbackPhase"
            :state="state"
          />
          <ArmSimConsole v-else />
        </div>
        <div v-if="loading" class="sim-map__loading">
          <span class="sim-map__spinner"></span>
          正在载入仿真会话
        </div>
      </section>

      <aside class="sim-side panel">
        <div class="sim-side__head">
          <div>
            <strong>执行档案</strong>
            <span>{{ task?.event_id || run?.event_id || '未关联事件' }}</span>
          </div>
          <span class="sim-side__sync" :class="{ 'is-live': hasActiveSession }">
            {{ hasActiveSession ? '实时同步' : '只读快照' }}
          </span>
        </div>

        <div class="sim-side__scroll">
          <!-- 失败原因：后端 state=error 时会带 error 字段（可能是整段堆栈）。
               以前这里什么都不显示，界面上只有一个「异常」标签，
               排查时完全不知道发生了什么 —— 所以必须露出来。 -->
          <section v-if="state === 'error' && failureReason" class="sim-section sim-section--error">
            <div class="sim-section__title">
              <span>失败原因</span>
            </div>
            <p class="sim-failure__brief">{{ failureReasonBrief || '仿真会话执行失败' }}</p>
            <details class="sim-failure__more">
              <summary>查看完整错误</summary>
              <pre class="sim-failure__raw">{{ failureReason }}</pre>
            </details>
          </section>

          <section class="sim-section">
            <div class="sim-section__title">工单详情</div>
            <div class="sim-kv" v-for="row in detailRows" :key="row.label">
              <span>{{ row.label }}</span>
              <strong>{{ row.value }}</strong>
            </div>
          </section>

          <section class="sim-section">
            <div class="sim-section__title">生命周期</div>
            <div class="sim-timeline">
              <div v-for="step in timeline" :key="step.label" class="sim-timeline__item">
                <span
                  class="sim-timeline__dot"
                  :class="{ 'is-done': step.time, 'is-current': step.current }"
                ></span>
                <span>{{ step.label }}</span>
                <time>{{ step.time ? fmtTime(step.time) : '—' }}</time>
              </div>
            </div>
          </section>

          <section class="sim-section">
            <div class="sim-section__title">
              <span>实时资源</span>
              <span class="sim-section__hint">{{ resourceUpdatedAt }}</span>
            </div>
            <div class="sim-resource">
              <div class="sim-resource__head">
                <span>电量</span>
                <strong :style="{ color: batteryColor(battery) }">{{ battery }}%</strong>
              </div>
              <div class="sim-meter">
                <i
                  :style="{
                    width: `${clampPercent(battery)}%`,
                    background: batteryColor(battery),
                  }"
                ></i>
              </div>
            </div>

            <div class="sim-bins">
              <div v-for="bin in binRows" :key="bin.key" class="sim-bin">
                <div class="sim-bin__head">
                  <span>{{ bin.label }}</span>
                  <strong>{{ fmtUsage(bin.value) }}</strong>
                </div>
                <div class="sim-meter sim-meter--small">
                  <i :style="{ width: `${clampPercent(bin.value * 100)}%`, background: bin.color }"></i>
                </div>
              </div>
            </div>
          </section>

          <section class="sim-section sim-section--stream">
            <div class="sim-stream__tabs" role="tablist" aria-label="仿真数据流">
              <button
                type="button"
                role="tab"
                :aria-selected="streamMode === 'telemetry'"
                :class="{ 'is-active': streamMode === 'telemetry' }"
                @click="streamMode = 'telemetry'"
              >
                机器人报文
                <span>{{ telemetryItems.length }}</span>
              </button>
              <button
                type="button"
                role="tab"
                :aria-selected="streamMode === 'logs'"
                :class="{ 'is-active': streamMode === 'logs' }"
                @click="streamMode = 'logs'"
              >
                运行日志
                <span>{{ logItems.length }}</span>
              </button>
            </div>

            <div v-if="streamMode === 'telemetry'" class="sim-stream">
              <div v-for="item in telemetryItems" :key="item._key" class="sim-stream__row">
                <time>{{ fmtTime(item._receivedAt) }}</time>
                <span class="sim-stream__tag" :class="`is-${item.status || 'idle'}`">
                  {{ robotStatusLabel(item.status) }}
                </span>
                <div class="sim-stream__body">
                  <span>电量 {{ numberOrDash(item.battery) }}% · {{ fmtCoord(item.lng, item.lat, 5) }}</span>
                  <code>
                    heading {{ numberOrDash(item.heading) }}° · speed
                    {{ numberOrDash(item.speed) }} · task {{ item.task_id || taskId }}
                  </code>
                </div>
              </div>
              <div v-if="!telemetryItems.length" class="sim-stream__empty">
                {{ telemetryEmptyText }}
              </div>
            </div>

            <div v-else class="sim-stream">
              <div v-for="item in logItems" :key="item.seq" class="sim-stream__row">
                <time>{{ fmtTime(item.ts) }}</time>
                <span class="sim-stream__tag" :class="`is-log-${item.kind}`">
                  {{ logKindLabel(item.kind) }}
                </span>
                <div class="sim-stream__body">
                  <span>{{ item.message }}</span>
                  <code v-if="item.payload?.command_id">
                    {{ item.payload.command_id }}
                  </code>
                </div>
              </div>
              <div v-if="!logItems.length" class="sim-stream__empty">
                {{ logEmptyText }}
              </div>
            </div>
          </section>
        </div>
      </aside>
    </div>

    <footer class="sim-controls panel">
      <div class="sim-controls__primary">
        <button
          class="sim-control sim-control--primary"
          type="button"
          :disabled="busy || !canTogglePlayback"
          @click="togglePlayback"
        >
          <span aria-hidden="true">{{ isRunning ? 'Ⅱ' : '▶' }}</span>
          {{ playbackLabel }}
        </button>
        <button
          class="sim-control"
          type="button"
          :disabled="busy || !canStep"
          @click="stepSimulation"
        >
          <span aria-hidden="true">›|</span>
          单步
        </button>
        <button
          class="sim-control sim-control--danger"
          type="button"
          :disabled="busy || !canStop"
          @click="stopSimulation"
        >
          <span aria-hidden="true">■</span>
          停止
        </button>
      </div>

      <div class="sim-controls__speeds" role="group" aria-label="仿真倍速">
        <button
          v-for="speed in SPEEDS"
          :key="speed"
          type="button"
          :class="{ 'is-active': selectedSpeed === speed }"
          :disabled="busy || !canControl"
          @click="changeSpeed(speed)"
        >
          {{ speed }}x
        </button>
      </div>

      <span class="sim-controls__hint">{{ controlHint }}</span>

      <div class="sim-controls__secondary">
        <!-- ★ busy 是字符串状态（''/'start'/'pause'…），不能裸绑 :disabled：
             Vue 判定布尔属性用 includeBooleanAttr(v) = !!v || v === ''，
             空字符串同样算 true —— 裸绑会让这个按钮永远处于禁用态，谁都点不动。 -->
        <button class="sim-control" type="button" :disabled="!!busy" @click="refreshAll">
          <span aria-hidden="true">↻</span>
          刷新
        </button>
        <button
          class="sim-control"
          type="button"
          :disabled="viewMode !== 'map'"
          @click="fitMap"
        >
          <span aria-hidden="true">⌖</span>
          全局视野
        </button>
      </div>
    </footer>
  </div>
</template>

<script setup>
import {
  computed,
  onMounted,
  onUnmounted,
  ref,
  watch,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { simulationApi, tasksApi } from '@/api'
import { useRealtimeStore } from '@/stores/realtime'
import { canWrite } from '@/utils/auth'
import {
  REVIEW_RESULT,
  TASK_STATUS,
} from '@/utils/constants'
import {
  batteryColor,
  fmtCoord,
  fmtTime,
  fmtUsage,
} from '@/utils/format'
import SimulationMap from '@/components/SimulationMap.vue'
import ArmSimConsole from '@/components/ArmSimConsole.vue'

const route = useRoute()
const router = useRouter()
const store = useRealtimeStore()

const SPEEDS = [1, 2, 4]
const ACTIVE_STATES = ['running', 'paused']
const ACTIVE_TASK_STATES = ['assigned', 'navigating', 'collecting']
const WRITABLE = canWrite()

const taskId = computed(() => String(route.params.taskId || ''))
const task = ref(null)
const run = ref(null)
const loading = ref(true)
const refreshing = ref(false)
const busy = ref('')
const selectedSpeed = ref(1)
const streamMode = ref('telemetry')
const mapRef = ref(null)
const viewMode = ref('console')
const autoStartAttempted = ref(false)

let pollTimer = null

const snapshot = computed(() => run.value || {})
const state = computed(() => snapshot.value.state || 'idle')
const phase = computed(() => snapshot.value.phase || fallbackPhase.value)
const robotId = computed(
  () => snapshot.value.robot_id || task.value?.robot_id || '',
)
const eventId = computed(
  () => snapshot.value.event_id || task.value?.event_id || '',
)
const progress = computed(() => {
  const value = Number(snapshot.value.progress)
  return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0
})
const hasActiveSession = computed(() => ACTIVE_STATES.includes(state.value))
const isRunning = computed(() => state.value === 'running')
const canControl = computed(() => WRITABLE && hasActiveSession.value)

// 工单是否已终结（done / cancelled）。
// ★ 用途：区分两处空态的文案。历史工单没有遥测时，真实原因是
//   「这台机器人没上报过 / 没跑过仿真」，而模板过去一律写「等待机器人遥测上报」，
//   看上去像页面卡住了。终态工单必须换一句实话。
const isSettledTask = computed(() =>
  ['done', 'cancelled'].includes(task.value?.status),
)
const telemetryEmptyText = computed(() =>
  isSettledTask.value
    ? '该工单无遥测落库（未执行过仿真或机器人未上报）'
    : '等待机器人遥测上报',
)
const logEmptyText = computed(() =>
  isSettledTask.value ? '该工单无可还原的运行记录' : '暂无可展示的运行日志',
)
const canStop = computed(() => canControl.value)
const canStep = computed(() => WRITABLE && state.value === 'paused')

const fallbackPhase = computed(() => {
  if (task.value?.status === 'assigned') return 'ack'
  if (task.value?.status === 'navigating') return 'navigating'
  if (task.value?.status === 'collecting') return 'collecting'
  if (task.value?.status === 'done') return 'done'
  return 'ack'
})

const canStart = computed(
  () =>
    WRITABLE &&
    !run.value &&
    !!task.value?.robot_id &&
    ACTIVE_TASK_STATES.includes(task.value?.status),
)

const canTogglePlayback = computed(() => {
  if (isRunning.value || state.value === 'paused') return canControl.value
  return canStart.value
})

const playbackLabel = computed(() => {
  if (isRunning.value) return '暂停'
  if (state.value === 'paused') return '继续'
  return '启动'
})

const mapHome = computed(
  () =>
    snapshot.value.home || {
      lng: 119.86,
      lat: 26.35,
    },
)
const mapPosition = computed(() => snapshot.value.position || mapHome.value)
const mapTarget = computed(() => {
  if (snapshot.value.target) return snapshot.value.target
  const lng = Number(task.value?.lng)
  const lat = Number(task.value?.lat)
  return Number.isFinite(lng) && Number.isFinite(lat) ? { lng, lat } : null
})

const STATE_LABELS = {
  idle: '等待启动',
  running: '运行中',
  paused: '已暂停',
  done: '已完成',
  stopped: '已停止',
  error: '异常',
  history: '历史轨迹',
}

const PHASE_LABELS = {
  ack: '等待机器人确认',
  navigating: '前往目标点',
  collecting: '清理作业中',
  returning: '返航中',
  done: '闭环完成',
}

const stateLabel = computed(() => STATE_LABELS[state.value] || state.value)
const phaseLabel = computed(() => PHASE_LABELS[phase.value] || '等待任务')
const stateTone = computed(() => {
  if (state.value === 'running') return 'running'
  if (state.value === 'paused') return 'paused'
  if (['done', 'history'].includes(state.value)) return 'done'
  if (['stopped', 'error'].includes(state.value)) return 'stopped'
  return 'idle'
})

const battery = computed(() => {
  const value = Number(snapshot.value.battery)
  return Number.isFinite(value) ? Math.round(value) : 100
})

const binRows = computed(() => {
  const bins = snapshot.value.bins || {}
  return [
    { key: 'foam', label: '泡沫仓', value: Number(bins.foam || 0), color: 'var(--c-foam)' },
    { key: 'plastic', label: '塑料仓', value: Number(bins.plastic || 0), color: 'var(--c-plastic)' },
    { key: 'mixed', label: '混合仓', value: Number(bins.mixed || 0), color: 'var(--c-fishing)' },
  ]
})

const resourceUpdatedAt = computed(() =>
  snapshot.value.updated_at ? fmtTime(snapshot.value.updated_at) : '未开始',
)

const detailRows = computed(() => {
  const t = task.value || {}
  // 字段与工单看板的详情抽屉保持一致（8 项）。
  // 以前这里只有 5 项，缺少打捞量/复核结果/备注 —— 同一张工单在两个页面
  // 显示的信息量不一致，现场按仿真页讲解时会缺料。
  return [
    { label: '事件编号', value: t.event_id || snapshot.value.event_id || '—' },
    { label: '执行机器人', value: robotId.value || '未分配' },
    { label: '优先级', value: t.priority ? `P${t.priority}` : '—' },
    { label: '目标坐标', value: fmtCoord(t.lng, t.lat, 5) },
    { label: '任务状态', value: TASK_STATUS[t.status] || t.status || '—' },
    {
      label: '打捞重量',
      value: t.collected_weight ? `${Number(t.collected_weight).toFixed(2)} kg` : '—',
    },
    {
      label: '复核结果',
      value: REVIEW_RESULT[t.review_result] || t.review_result || '—',
    },
    { label: '备注', value: t.remark || '—' },
  ]
})

const timeline = computed(() => {
  const t = task.value || {}
  const rows = [
    { label: '创建', time: t.created_at, current: t.status === 'pending' },
    { label: '派单', time: t.assigned_at, current: t.status === 'assigned' },
    { label: '机器人确认', time: t.ack_at, current: false },
    { label: '开始作业', time: t.started_at, current: t.status === 'collecting' },
    { label: '完成', time: t.finished_at, current: t.status === 'done' },
  ]
  const currentIndex = rows.findIndex((row) => row.current)
  if (currentIndex >= 0) rows[currentIndex].current = true
  return rows
})

const telemetryItems = computed(() => {
  return store.telemetryFeed
    .filter(
      (item) =>
        (robotId.value && item.robot_id === robotId.value) ||
        (taskId.value && item.task_id === taskId.value),
    )
    .slice(0, 60)
})

const logItems = computed(() =>
  [...(snapshot.value.logs || [])].reverse().slice(0, 80),
)

const controlHint = computed(() => {
  if (!WRITABLE) return '当前账号为只读权限，可查看轨迹但不能控制仿真'
  if (state.value === 'error') {
    return '仿真会话创建/执行失败，原因见右侧「失败原因」，修正后点刷新重试'
  }
  if (['done', 'stopped'].includes(state.value)) {
    return '本次仿真会话已结束，刷新页面可重新读取结果'
  }
  if (state.value === 'history') return '历史轨迹只读；地图视图可查看轨迹回放'
  if (!run.value && !task.value?.robot_id) return '工单未绑定机器人，无法启动仿真'
  if (!run.value && task.value?.status === 'pending') return '工单尚未派单，无法启动仿真'
  if (!run.value && task.value?.status === 'cancelled') return '已取消工单不可执行仿真'
  if (!run.value && task.value?.status === 'done') return '该工单没有轨迹记录，且终态不可重跑'
  if (!run.value) return '可启动仿真，任务状态将沿正式状态机自动推进'
  if (state.value === 'paused') return '已暂停，可单步推进或继续运行'
  return '仿真正在通过正式遥测、ACK 和状态机链路执行'
})

// 失败原因：后端 error 字段可能是一整段 SQLAlchemy/asyncpg 堆栈，
// 直接铺在界面上没法看，这里压成一行并保留可展开的完整文本。
const failureReason = computed(() => String(snapshot.value.error || '').trim())
const failureReasonBrief = computed(() => {
  const raw = failureReason.value
  if (!raw) return ''
  // 优先抽取 asyncpg 的 DETAIL / 关键异常名，其余折成单行
  const detail = raw.match(/DETAIL:\s*([^\n\[]+)/i)
  if (detail) return detail[1].trim()
  const exc = raw.match(/(\w+Error|\w+Exception)\b/)
  const head = raw.split('\n')[0]
  return exc && !head.includes(exc[1]) ? `${exc[1]}: ${head}` : head
})

function clampPercent(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return 0
  return Math.max(0, Math.min(100, number))
}

function numberOrDash(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(Number.isInteger(number) ? 0 : 1) : '—'
}

function robotStatusLabel(status) {
  return {
    navigating: '导航',
    collecting: '清理',
    returning: '返航',
    done: '完成',
    idle: '待机',
  }[status] || status || '待机'
}

function logKindLabel(kind) {
  return {
    system: '系统',
    control: '控制',
    ack: 'ACK',
    telemetry: '遥测',
    progress: '进度',
    done: '完成',
    error: '异常',
  }[kind] || kind || '日志'
}

async function loadState({ autoStart = false } = {}) {
  if (!taskId.value || loading.value && task.value) return
  loading.value = true
  try {
    const [taskData, runData] = await Promise.all([
      tasksApi.detail(taskId.value),
      simulationApi.status(taskId.value),
    ])
    task.value = taskData
    run.value = runData
    if (autoStart) await maybeAutoStart()
  } catch (err) {
    store.error = err.message
    task.value = null
    run.value = null
  } finally {
    loading.value = false
  }
}

async function refreshTask() {
  if (!taskId.value) return
  try {
    task.value = await tasksApi.detail(taskId.value)
  } catch (err) {
    store.error = err.message
  }
}

async function refreshRun({ silent = true } = {}) {
  if (!taskId.value || refreshing.value) return
  refreshing.value = true
  try {
    run.value = await simulationApi.status(taskId.value)
  } catch (err) {
    if (!silent) store.error = err.message
  } finally {
    refreshing.value = false
  }
}

async function maybeAutoStart() {
  if (autoStartAttempted.value || !canStart.value) return
  autoStartAttempted.value = true
  await startSimulation()
}

async function startSimulation() {
  if (!canStart.value || busy.value) return
  busy.value = 'start'
  try {
    run.value = await simulationApi.start(taskId.value, { speed: selectedSpeed.value })
  } catch (err) {
    store.error = err.message
  } finally {
    busy.value = ''
  }
}

async function togglePlayback() {
  if (busy.value || !canTogglePlayback.value) return
  if (isRunning.value) {
    busy.value = 'pause'
    try {
      run.value = await simulationApi.pause(taskId.value)
    } catch (err) {
      store.error = err.message
    } finally {
      busy.value = ''
    }
    return
  }
  if (state.value === 'paused') {
    busy.value = 'resume'
    try {
      run.value = await simulationApi.resume(taskId.value)
    } catch (err) {
      store.error = err.message
    } finally {
      busy.value = ''
    }
    return
  }
  await startSimulation()
}

async function changeSpeed(speed) {
  selectedSpeed.value = speed
  if (!canControl.value || busy.value) return
  busy.value = 'speed'
  try {
    run.value = await simulationApi.speed(taskId.value, speed)
  } catch (err) {
    store.error = err.message
  } finally {
    busy.value = ''
  }
}

async function stepSimulation() {
  if (!canStep.value || busy.value) return
  busy.value = 'step'
  try {
    run.value = await simulationApi.step(taskId.value)
  } catch (err) {
    store.error = err.message
  } finally {
    busy.value = ''
  }
}

async function stopSimulation() {
  if (!canStop.value || busy.value) return
  busy.value = 'stop'
  try {
    run.value = await simulationApi.stop(taskId.value, {
      reason: '操作员手动停止仿真',
    })
    await refreshTask()
  } catch (err) {
    store.error = err.message
  } finally {
    busy.value = ''
  }
}

async function refreshAll() {
  await loadState()
}

function fitMap() {
  mapRef.value?.fitToData(true)
}

function openEvent() {
  if (!eventId.value) return
  router.push({ name: 'events', query: { event: eventId.value } })
}

function syncPolling() {
  clearInterval(pollTimer)
  pollTimer = null
  if (!hasActiveSession.value) return
  pollTimer = setInterval(() => {
    refreshRun()
  }, 1000)
}

watch([state, () => snapshot.value.run_id], syncPolling)

watch(state, (next, previous) => {
  if (next !== previous && ['done', 'stopped', 'error'].includes(next)) {
    refreshTask()
  }
})

watch(
  () => store.taskFeed[0]?._ts,
  async (ts, previous) => {
    if (!ts || ts === previous) return
    const item = store.taskFeed[0]
    if (item?.task_id !== taskId.value) return
    await refreshTask()
    if (!run.value || hasActiveSession.value) await refreshRun()
    await maybeAutoStart()
  },
)

watch(taskId, async () => {
  clearInterval(pollTimer)
  pollTimer = null
  autoStartAttempted.value = false
  selectedSpeed.value = 1
  streamMode.value = 'telemetry'
  task.value = null
  run.value = null
  loading.value = false
  await loadState({ autoStart: true })
})

onMounted(() => {
  loadState({ autoStart: true })
})

onUnmounted(() => {
  clearInterval(pollTimer)
})
</script>

<style scoped>
.sim-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.sim-head {
  display: grid;
  grid-template-columns: minmax(280px, auto) minmax(0, 1fr) auto;
  align-items: center;
  gap: 16px;
  min-height: 66px;
  padding: 10px 14px;
  flex-shrink: 0;
}

.sim-head__identity,
.sim-head__facts,
.sim-head__actions {
  display: flex;
  align-items: center;
}

.sim-head__identity {
  gap: 11px;
  min-width: 0;
}

.sim-head__back {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border: 0;
  border-radius: 50%;
  background: var(--bg-panel-2);
  color: var(--text-main);
  font-size: 28px;
  line-height: 1;
  cursor: pointer;
}

.sim-head__back:hover {
  background: var(--bg-hover);
}

.sim-head__title {
  min-width: 0;
}

.sim-head__title span {
  display: block;
  color: var(--text-sub);
  font-size: 11px;
}

.sim-head__title h1 {
  overflow: hidden;
  color: var(--text-main);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 16px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sim-state {
  flex-shrink: 0;
  min-height: 26px;
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(142, 142, 147, 0.14);
  color: var(--text-sub);
  font-size: 12px;
  font-weight: 600;
  line-height: 20px;
  white-space: nowrap;
}

.sim-state--running {
  background: rgba(0, 122, 255, 0.13);
  color: var(--c-primary);
}

.sim-state--paused {
  background: rgba(255, 149, 0, 0.14);
  color: var(--c-warn);
}

.sim-state--done {
  background: rgba(52, 199, 89, 0.14);
  color: var(--c-success);
}

.sim-state--stopped {
  background: rgba(255, 59, 48, 0.13);
  color: var(--c-danger);
}

.sim-state--replay {
  background: rgba(0, 199, 255, 0.13);
  color: #00a8d4;
}

.sim-head__facts {
  justify-content: center;
  gap: 18px;
  min-width: 0;
  color: var(--text-sub);
  font-size: 12px;
}

.sim-head__facts span {
  min-width: 0;
  white-space: nowrap;
}

.sim-head__facts strong {
  color: var(--text-main);
  font-weight: 600;
}

.sim-head__actions {
  justify-content: flex-end;
  gap: 8px;
}

.sim-head__action {
  min-height: 36px;
  padding: 6px 12px;
  border: 0;
  border-radius: 9px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  cursor: pointer;
  white-space: nowrap;
}

.sim-head__action:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.sim-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(330px, 390px);
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.sim-map {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.sim-map__viewbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 44px;
  padding: 7px 10px 7px 14px;
  border-bottom: 1px solid var(--separator);
  color: var(--text-sub);
  font-size: 12px;
  font-weight: 600;
}

.sim-map__switch {
  display: flex;
  gap: 2px;
  padding: 3px;
  border-radius: 10px;
  background: var(--bg-panel-2);
}

.sim-map__switch button {
  min-width: 66px;
  min-height: 30px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-sub);
  font-size: 11.5px;
  cursor: pointer;
}

.sim-map__switch button:hover,
.sim-map__switch button.is-active {
  background: var(--bg-panel);
  color: var(--c-primary);
}

.sim-map__stage {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
}

.sim-map__loading {
  position: absolute;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  background: color-mix(in srgb, var(--bg-panel) 74%, transparent);
  color: var(--text-sub);
  font-size: 13px;
  backdrop-filter: blur(3px);
}

.sim-map__spinner {
  width: 17px;
  height: 17px;
  border: 2px solid var(--border);
  border-top-color: var(--c-primary);
  border-radius: 50%;
  animation: sim-spin 0.75s linear infinite;
}

@keyframes sim-spin {
  to { transform: rotate(360deg); }
}

.sim-side {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sim-side__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 54px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--separator);
}

.sim-side__head > div {
  min-width: 0;
}

.sim-side__head strong,
.sim-side__head span {
  display: block;
}

.sim-side__head strong {
  font-size: 14px;
}

.sim-side__head > div > span {
  overflow: hidden;
  margin-top: 1px;
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sim-side__sync {
  flex-shrink: 0;
  padding: 3px 8px;
  border-radius: 999px;
  background: var(--bg-panel-2);
  color: var(--text-dim);
  font-size: 10.5px;
}

.sim-side__sync.is-live {
  background: rgba(0, 122, 255, 0.12);
  color: var(--c-primary);
}

.sim-side__sync.is-replay {
  background: rgba(0, 199, 255, 0.12);
  color: #00a8d4;
}

.sim-side__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.sim-section {
  padding: 13px 14px;
  border-bottom: 1px solid var(--separator);
}

/* ---------- 失败原因 ---------- */
.sim-section--error {
  background: rgba(242, 86, 76, 0.09);
  border-left: 3px solid rgba(242, 86, 76, 0.65);
}

.sim-failure__brief {
  margin: 0;
  color: #ffb4ae;
  font-size: 12px;
  line-height: 1.55;
  word-break: break-word;
}

.sim-failure__more {
  margin-top: 8px;
}

.sim-failure__more summary {
  color: var(--text-dim);
  font-size: 11.5px;
  cursor: pointer;
  user-select: none;
}

.sim-failure__raw {
  max-height: 220px;
  margin: 8px 0 0;
  padding: 8px 10px;
  overflow: auto;
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

.sim-section__title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 9px;
  color: var(--text-main);
  font-size: 12.5px;
  font-weight: 600;
}

.sim-section__hint {
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10px;
  font-weight: 400;
}

.sim-kv {
  display: grid;
  grid-template-columns: 78px minmax(0, 1fr);
  gap: 10px;
  padding: 4px 0;
  font-size: 12px;
}

.sim-kv span {
  color: var(--text-sub);
}

.sim-kv strong {
  overflow-wrap: anywhere;
  color: var(--text-main);
  font-weight: 500;
  text-align: right;
}

.sim-timeline {
  display: grid;
  gap: 2px;
}

.sim-timeline__item {
  display: grid;
  grid-template-columns: 13px minmax(72px, 1fr) auto;
  align-items: center;
  gap: 7px;
  min-height: 30px;
  color: var(--text-main);
  font-size: 11.5px;
}

.sim-timeline__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--border-bright);
}

.sim-timeline__dot.is-done {
  background: var(--c-success);
}

.sim-timeline__dot.is-current {
  background: var(--c-primary);
  box-shadow: 0 0 0 4px rgba(0, 122, 255, 0.14);
}

.sim-timeline__item time {
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10px;
  text-align: right;
}

.sim-resource {
  margin-bottom: 12px;
}

.sim-resource__head,
.sim-bin__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 5px;
  color: var(--text-sub);
  font-size: 11.5px;
}

.sim-resource__head strong,
.sim-bin__head strong {
  color: var(--text-main);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 12px;
}

.sim-meter {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--bg-panel-2);
}

.sim-meter i {
  display: block;
  height: 100%;
  min-width: 0;
  border-radius: inherit;
  transition: width 0.28s var(--ease);
}

.sim-meter--small {
  height: 5px;
}

.sim-bins {
  display: grid;
  gap: 9px;
}

.sim-section--stream {
  padding: 0 0 12px;
  border-bottom: 0;
}

.sim-stream__tabs {
  position: sticky;
  top: 0;
  z-index: 2;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  padding: 10px 12px 8px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--separator);
}

.sim-stream__tabs button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 34px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text-sub);
  font-size: 11.5px;
  cursor: pointer;
}

.sim-stream__tabs button:hover,
.sim-stream__tabs button.is-active {
  background: var(--bg-active);
  color: var(--c-primary);
}

.sim-stream__tabs button span {
  min-width: 17px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--bg-panel-2);
  color: inherit;
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 9.5px;
  line-height: 17px;
}

.sim-stream {
  display: grid;
  gap: 1px;
  padding: 8px 12px 0;
}

.sim-stream__row {
  display: grid;
  grid-template-columns: 62px 42px minmax(0, 1fr);
  align-items: start;
  gap: 7px;
  padding: 7px 0;
  border-bottom: 1px solid var(--separator);
}

.sim-stream__row:last-child {
  border-bottom: 0;
}

.sim-stream__row time {
  padding-top: 2px;
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 9.5px;
}

.sim-stream__tag {
  display: inline-block;
  margin-top: 1px;
  padding: 1px 5px;
  border-radius: 5px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  font-size: 9.5px;
  text-align: center;
  white-space: nowrap;
}

.sim-stream__tag.is-navigating,
.sim-stream__tag.is-collecting,
.sim-stream__tag.is-returning,
.sim-stream__tag.is-log-telemetry,
.sim-stream__tag.is-log-progress {
  background: rgba(0, 122, 255, 0.1);
  color: var(--c-primary);
}

.sim-stream__tag.is-done,
.sim-stream__tag.is-log-done,
.sim-stream__tag.is-log-ack {
  background: rgba(52, 199, 89, 0.12);
  color: var(--c-success);
}

.sim-stream__tag.is-log-error {
  background: rgba(255, 59, 48, 0.12);
  color: var(--c-danger);
}

.sim-stream__body {
  min-width: 0;
  display: grid;
  gap: 2px;
  color: var(--text-main);
  font-size: 10.5px;
  line-height: 1.45;
}

.sim-stream__body span,
.sim-stream__body code {
  overflow-wrap: anywhere;
}

.sim-stream__body code {
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 9.5px;
}

.sim-stream__empty {
  padding: 22px 0;
  color: var(--text-dim);
  font-size: 11.5px;
  text-align: center;
}

.sim-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 58px;
  padding: 9px 12px;
  flex-shrink: 0;
}

.sim-controls__primary,
.sim-controls__secondary,
.sim-controls__speeds {
  display: flex;
  align-items: center;
  gap: 6px;
}

.sim-control {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 38px;
  padding: 6px 12px;
  border: 0;
  border-radius: 9px;
  background: var(--bg-panel-2);
  color: var(--text-main);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.sim-control span {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 13px;
}

.sim-control:hover:not(:disabled) {
  background: var(--bg-hover);
}

.sim-control--primary {
  background: var(--c-primary);
  color: #ffffff;
}

.sim-control--primary:hover:not(:disabled) {
  background: var(--c-primary-dim);
}

.sim-control--danger {
  color: var(--c-danger);
  background: rgba(255, 59, 48, 0.09);
}

.sim-control:disabled,
.sim-controls__speeds button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.sim-controls__speeds {
  padding: 3px;
  border-radius: 10px;
  background: var(--bg-panel-2);
}

.sim-controls__speeds button {
  min-width: 44px;
  min-height: 32px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-sub);
  font-size: 11.5px;
  cursor: pointer;
}

.sim-controls__speeds button:hover:not(:disabled),
.sim-controls__speeds button.is-active {
  background: var(--bg-panel);
  color: var(--c-primary);
}

.sim-controls__hint {
  flex: 1;
  min-width: 120px;
  color: var(--text-dim);
  font-size: 11px;
  text-align: center;
}

.sim-controls__secondary {
  margin-left: auto;
}

@media (max-width: 1180px) {
  .sim-head {
    grid-template-columns: minmax(260px, 1fr) auto;
  }

  .sim-head__facts {
    display: none;
  }

  .sim-layout {
    grid-template-columns: minmax(0, 1fr) minmax(310px, 350px);
  }

  .sim-controls__hint {
    display: none;
  }
}

@media (max-width: 900px) {
  .sim-page {
    height: auto;
    min-height: 100%;
  }

  .sim-head {
    grid-template-columns: 1fr;
    gap: 9px;
  }

  .sim-head__identity,
  .sim-head__actions {
    width: 100%;
  }

  .sim-head__actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .sim-head__action {
    width: 100%;
  }

  .sim-layout {
    grid-template-columns: minmax(0, 1fr);
    flex: none;
  }

  .sim-map {
    height: clamp(380px, 58vh, 560px);
  }

  .sim-side {
    max-height: none;
  }

  .sim-side__scroll {
    overflow: visible;
  }

  .sim-stream__tabs {
    position: static;
  }

  .sim-controls {
    position: sticky;
    bottom: 0;
    z-index: 20;
    flex-wrap: wrap;
    gap: 8px;
    padding-bottom: calc(9px + env(safe-area-inset-bottom, 0px));
  }

  .sim-controls__primary {
    flex: 1 1 100%;
  }

  .sim-controls__primary .sim-control {
    flex: 1;
  }
}

@media (max-width: 560px) {
  .sim-head__title h1 {
    font-size: 14px;
  }

  .sim-state {
    margin-left: auto;
  }

  .sim-layout {
    gap: 10px;
  }

  .sim-map {
    height: 420px;
  }

  .sim-controls__speeds {
    flex: 1;
  }

  .sim-controls__speeds button {
    flex: 1;
    min-width: 0;
  }

  .sim-controls__secondary {
    flex: 1;
  }

  .sim-controls__secondary .sim-control {
    flex: 1;
  }

  .sim-stream__row {
    grid-template-columns: 54px 38px minmax(0, 1fr);
    gap: 5px;
  }
}
</style>

<template>
  <div class="page">
    <!-- 状态统计条 -->
    <div class="status-bar">
      <div
        v-for="s in statusSummary"
        :key="s.key"
        class="status-chip"
        :class="{ 'status-chip--on': filters.status === s.key }"
        @click="toggleStatus(s.key)"
      >
        <span class="status-chip__label">{{ s.label }}</span>
        <span class="status-chip__num" :style="{ color: s.color }">{{ s.count }}</span>
      </div>
      <div class="status-bar__spacer"></div>
      <!-- 地图点船跳来的机器人筛选：给出可见、可清除的状态 -->
      <span v-if="robotFilter" class="robot-chip">
        筛选机器人：{{ robotFilter }}
        <em v-if="!boardTotal" class="robot-chip__none">该船暂无工单</em>
        <button class="robot-chip__clear" type="button" title="清除筛选" @click="clearRobotFilter">×</button>
      </span>
      <button v-if="canWriteOps" class="btn" @click="openManual">人工建单</button>
      <button v-if="canWriteOps" class="btn" @click="dispatchPending" :disabled="dispatching">
        {{ dispatching ? '派单中…' : '触发补派' }}
      </button>
      <button class="btn" @click="exportCsv">导出 CSV</button>
      <button class="btn" @click="load">刷新</button>
    </div>

    <!-- 看板列 -->
    <div class="kanban">
      <section v-for="col in columns" :key="col.status" class="kanban__col">
        <div class="kanban__head">
          <span>
            <i class="kanban__dot" :style="{ background: col.color }"></i>
            {{ col.label }}
          </span>
          <span class="kanban__count">{{ boardGrouped[col.status]?.length || 0 }}</span>
        </div>

        <div class="kanban__body">
          <article
            v-for="t in boardGrouped[col.status] || []"
            :key="t.task_id"
            class="tcard"
            :class="{ 'is-highlighted': highlightTaskId === t.task_id }"
            :ref="(el) => setCardRef(t.task_id, el)"
            @click="openDetail(t)"
          >
            <div class="tcard__head">
              <span class="tcard__id">{{ t.task_id }}</span>
              <span class="tcard__pri" :class="`tcard__pri--${t.priority}`">
                P{{ t.priority }}
              </span>
            </div>

            <div class="tcard__row">
              <span class="text-sub">机器人</span>
              <span>{{ t.robot_id || '未分配' }}</span>
            </div>
            <div class="tcard__row">
              <span class="text-sub">目标点</span>
              <span class="num">{{ fmtCoord(t.lng, t.lat) }}</span>
            </div>
            <div class="tcard__row">
              <span class="text-sub">创建</span>
              <span>{{ fmtRelative(t.created_at) }}</span>
            </div>
            <div v-if="t.collected_weight" class="tcard__row">
              <span class="text-sub">打捞量</span>
              <span class="text-primary">{{ Number(t.collected_weight).toFixed(2) }} kg</span>
            </div>

            <!-- 可执行动作 -->
            <div class="tcard__actions" @click.stop>
              <button
                class="tcard__btn tcard__btn--simulation"
                :disabled="!canSimulate(t)"
                :title="simulationHint(t)"
                @click="openSimulation(t)"
              >
                执行仿真 ▸
              </button>
              <template v-if="canWriteOps">
              <button
                v-for="ns in nextStates(t)"
                :key="ns"
                class="tcard__btn"
                :class="ns === 'cancelled' ? 'tcard__btn--danger' : ''"
                @click="changeStatus(t, ns)"
              >
                {{ TASK_STATUS[ns] }}
              </button>
              </template>
            </div>
          </article>

          <div v-if="!boardGrouped[col.status]?.length" class="kanban__empty">暂无工单</div>
        </div>
      </section>
    </div>

    <!-- 详情抽屉 —— 必须 Teleport 到 body。
         抽屉虽然 position:fixed，但它被包在 .layout__main 里，而 .layout__main
         有 z-index:1、.layout 又有 isolation:isolate —— 于是「主内容」整体成了
         一个层叠上下文，抽屉 z-index 再高也只在这个层里比大小，
         永远越不过兄弟节点 .layout__header 的 z-index:300。
         结果：抽屉标题行与右上角「×」被顶栏完整盖住，既看不见也点不到。 -->
    <Teleport to="body">
      <div v-if="detail" class="drawer" @click.self="closeDetail">
        <div class="drawer__panel panel">
          <div class="panel-title">
            <span>工单详情 · {{ detail.task_id }}</span>
            <button class="drawer__close" @click="closeDetail">×</button>
          </div>
          <div class="panel-body drawer__body">
            <div class="kv" v-for="kv in detailRows" :key="kv.label">
              <span class="kv__k">{{ kv.label }}</span>
              <span class="kv__v">{{ kv.value }}</span>
            </div>

            <!-- 时间戳链：工单生命周期的完整证据 -->
            <div class="timeline">
              <div class="timeline__title">生命周期</div>
              <div v-for="step in timeline" :key="step.label" class="timeline__item">
                <span class="timeline__dot" :class="{ 'timeline__dot--done': step.time }"></span>
                <span class="timeline__label">{{ step.label }}</span>
                <span class="timeline__time">{{ step.time ? fmtTime(step.time) : '—' }}</span>
              </div>
            </div>

            <!-- 主操作：进入机械臂作业台（三维作业仿真 + 机械臂执行端控制台；
                 控制台内可输 IP 直连树莓派上的训练作业台） -->
            <div class="drawer__cta">
              <button
                class="btn btn--primary drawer__cta-btn"
                :disabled="!canSimulate(detail)"
                :title="simulationHint(detail)"
                @click="openSimulation(detail)"
              >
                打开机械臂作业台 · 输 IP 接入 ▸
              </button>
              <span v-if="!canSimulate(detail)" class="drawer__cta-hint">
                {{ simulationHint(detail) }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 完成工单录入框：打捞量由操作员如实填写，不复用随机数（同样 Teleport 到 body） -->
    <Teleport to="body">
      <div v-if="completing" class="drawer" @click.self="completing = null">
        <div class="drawer__panel panel">
          <div class="panel-title">
            <span>完成工单 · {{ completing.task_id }}</span>
            <button class="drawer__close" @click="completing = null">×</button>
          </div>
          <div class="panel-body drawer__body">
            <div class="form-row">
              <label class="form-label">打捞重量（kg）</label>
              <input
                v-model="completion.weight"
                class="form-input"
                type="number"
                min="0"
                step="0.01"
                placeholder="如实填写本次清理量，可留空"
              />
              <span class="form-hint">来自人工称重或机器人仓容，留空则暂不记录</span>
            </div>
            <div class="form-row">
              <label class="form-label">复核结果</label>
              <select v-model="completion.review" class="form-input">
                <option value="confirmed">确认清理</option>
                <option value="not_found">到场未发现</option>
                <option value="recheck">需人工复查</option>
              </select>
            </div>
            <div class="drawer__actions">
              <button class="btn" @click="completing = null">取消</button>
              <button class="btn btn--primary" @click="confirmDone">确认完成</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 人工建单：漏检兜底，操作员手动指定位置/机器人创建任务（同样 Teleport 到 body） -->
    <Teleport to="body">
      <div v-if="manual" class="drawer" @click.self="manual = false">
        <div class="drawer__panel panel">
          <div class="panel-title">
            <span>人工建单（漏检兜底）</span>
            <button class="drawer__close" @click="manual = false">×</button>
          </div>
          <div class="panel-body drawer__body">
            <div class="form-row">
              <label class="form-label">目标经度（lng）</label>
              <input
                v-model="manualForm.lng"
                class="form-input"
                type="number"
                step="0.0001"
                placeholder="如 119.6521"
              />
            </div>
            <div class="form-row">
              <label class="form-label">目标纬度（lat）</label>
              <input
                v-model="manualForm.lat"
                class="form-input"
                type="number"
                step="0.0001"
                placeholder="如 26.3864"
              />
            </div>
            <div class="form-row">
              <label class="form-label">执行机器人（空则进入待派单）</label>
              <select v-model="manualForm.robot_id" class="form-input">
                <option value="">不指定（待自动派单）</option>
                <option v-for="r in store.robots" :key="r.robot_id" :value="r.robot_id">
                  {{ r.name || r.robot_id }}（{{ r.status === 'online' ? '在线' : '离线' }}）
                </option>
              </select>
              <!-- 演示便利：换了机器人就能一键把目标点对齐到它的位置 -->
              <button class="btn manual-form__locate" type="button" @click="useRobotPosition">
                用该机器人位置
              </button>
            </div>
            <div class="form-row">
              <label class="form-label">优先级</label>
              <select v-model.number="manualForm.priority" class="form-input">
                <option :value="1">P1 紧急</option>
                <option :value="3">P3 普通</option>
                <option :value="5">P5 一般</option>
              </select>
            </div>
            <div class="drawer__actions">
              <button class="btn" @click="manual = false">取消</button>
              <button class="btn btn--primary" :disabled="creating" @click="submitManual">
                {{ creating ? '创建中…' : '创建任务' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
/**
 * 工单看板 —— 用看板而不是纯表格，是因为工单是有"流动"的：
 * 从待派单一路走到完成，看板能一眼看出一堆积压在哪一列。
 */
import { ref, reactive, computed, onMounted, watch, nextTick, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { tasksApi, downloadBlob } from '@/api'
import { useRealtimeStore } from '@/stores/realtime'
import {
  TASK_STATUS,
  TASK_TRANSITIONS,
  REVIEW_RESULT,
} from '@/utils/constants'
import { fmtCoord, fmtRelative, fmtTime } from '@/utils/format'
import { canWrite } from '@/utils/auth'
import { toast } from '@/utils/toast'

const store = useRealtimeStore()
const router = useRouter()
const route = useRoute()

// viewer / 匿名只读：隐藏写操作（后端 require_operator 才是最终裁决）
const canWriteOps = canWrite()

const tasks = ref([])
const detail = ref(null)
const dispatching = ref(false)
const filters = reactive({ status: '' })
// 来自 ?robot= 的机器人筛选（3D 地图点船无活跃工单时会带过来）
const robotFilter = ref('')
// 深链 ?task= 命中时高亮的那张卡（"一堆工单里到底点的是哪张"）
const highlightTaskId = ref('')
const cardRefs = new Map()
let highlightTimer = null
function setCardRef(taskId, el) {
  if (el) cardRefs.set(taskId, el)
  else cardRefs.delete(taskId)
}
// 完成工单的录入表单：打捞重量必须由操作员如实填写，不再伪造随机数
const completing = ref(null)
const completion = reactive({ weight: '', review: 'confirmed' })
// 人工建单：漏检兜底
const manual = ref(false)
const manualForm = reactive({ lng: '', lat: '', robot_id: '', priority: 5 })
// 建单防重：按钮 :disabled 之外，函数入口再拦一次
const creating = ref(false)

const COLUMNS = [
  { status: 'pending', label: '待派单', color: '#8e8e93' },
  { status: 'assigned', label: '已派单', color: '#5856d6' },
  { status: 'navigating', label: '前往中', color: '#007aff' },
  { status: 'collecting', label: '作业中', color: '#007aff' },
  { status: 'done', label: '已完成', color: '#34c759' },
  // 已取消单独成列：此前不在 COLUMNS 里，取消的工单在**任何视图**都看不到，
  // 数据其实在库里（t_task.status='cancelled'）——「看板少一列」看起来像丢数据。
  { status: 'cancelled', label: '已取消', color: '#ff3b30' },
]

const columns = computed(() =>
  filters.status ? COLUMNS.filter((c) => c.status === filters.status) : COLUMNS,
)

const grouped = computed(() => {
  const g = {}
  for (const c of COLUMNS) g[c.status] = []
  for (const t of tasks.value) {
    if (g[t.status]) g[t.status].push(t)
  }
  return g
})

/** 看板实际渲染的数据：叠加 ?robot= 筛选 */
const boardGrouped = computed(() => {
  if (!robotFilter.value) return grouped.value
  const g = {}
  for (const c of COLUMNS) g[c.status] = []
  for (const t of tasks.value) {
    if (g[t.status] && t.robot_id === robotFilter.value) g[t.status].push(t)
  }
  return g
})

/** 看板当前渲染的工单总数（机器人筛选后为 0 时给出"该船暂无工单"提示） */
const boardTotal = computed(() =>
  Object.values(boardGrouped.value || {}).reduce((n, list) => n + (list?.length || 0), 0),
)

const statusSummary = computed(() =>
  COLUMNS.map((c) => ({
    key: c.status,
    label: c.label,
    color: c.color,
    count: grouped.value[c.status]?.length || 0,
  })),
)

/** 当前状态下允许的下一步（与后端状态机一致，前端只做展示过滤） */
function nextStates(task) {
  const allowed = TASK_TRANSITIONS[task.status] || []
  // 演示用：不暴露全部跳转，只给最常用的两条
  const priority = ['done', 'collecting', 'navigating', 'cancelled']
  return allowed.filter((s) => priority.includes(s)).slice(0, 2)
}

function canSimulate(task) {
  return (
    !!task.robot_id &&
    ['assigned', 'navigating', 'collecting', 'done'].includes(task.status)
  )
}

function simulationHint(task) {
  if (!task.robot_id) return '工单尚未绑定机器人'
  if (task.status === 'pending') return '工单尚未派单'
  if (task.status === 'cancelled') return '已取消工单不可执行仿真'
  if (task.status === 'done') return '查看历史执行轨迹'
  return '打开工单执行仿真'
}

function openSimulation(task) {
  if (!canSimulate(task)) return
  router.push({ name: 'simulation', params: { taskId: task.task_id } })
}

const detailRows = computed(() => {
  if (!detail.value) return []
  const d = detail.value
  return [
    { label: '事件编号', value: d.event_id || '—' },
    { label: '执行机器人', value: d.robot_id || '未分配' },
    { label: '目标坐标', value: fmtCoord(d.lng, d.lat) },
    { label: '状态', value: TASK_STATUS[d.status] || d.status },
    { label: '优先级', value: `P${d.priority}` },
    { label: '打捞重量', value: d.collected_weight ? `${Number(d.collected_weight).toFixed(2)} kg` : '—' },
    { label: '复核结果', value: REVIEW_RESULT[d.review_result] || d.review_result || '—' },
    { label: '备注', value: d.remark || '—' },
  ]
})

const timeline = computed(() => {
  if (!detail.value) return []
  const d = detail.value
  return [
    { label: '创建', time: d.created_at },
    { label: '派单', time: d.assigned_at },
    { label: '确认', time: d.ack_at },
    { label: '开工', time: d.started_at },
    { label: '完成', time: d.finished_at },
  ]
})

async function load() {
  try {
    const res = await tasksApi.list({ page: 1, page_size: 200 })
    tasks.value = res?.items || []
  } catch (err) {
    store.error = err.message
  }
}

async function exportCsv() {
  try {
    const blob = await tasksApi.export({ status: filters.status || undefined })
    downloadBlob(blob, `工单台账_${new Date().toISOString().slice(0, 10)}.csv`)
  } catch (err) {
    store.error = err.message
  }
}

function toggleStatus(key) {
  filters.status = filters.status === key ? '' : key
}

function openDetail(task) {
  detail.value = task
}

/** 关闭抽屉时顺手摘掉 URL 上的 ?task=：
    否则刷新页面会"抽屉自己又弹出来"，像是关不掉 */
function closeDetail() {
  detail.value = null
  if (route.query.task) {
    const rest = { ...route.query }
    delete rest.task
    router.replace({ query: rest }).catch(() => {})
  }
}

async function changeStatus(task, target) {
  // 完成工单需要操作员真实录入打捞量与复核结果，不再伪造随机数
  if (target === 'done') {
    completing.value = task
    completion.weight = ''
    completion.review = 'confirmed'
    return
  }
  await applyStatus(task, target)
}

async function applyStatus(task, target, extra = {}) {
  try {
    await tasksApi.updateStatus(task.task_id, { status: target, ...extra })
    await load()
    if (detail.value?.task_id === task.task_id) {
      detail.value = tasks.value.find((t) => t.task_id === task.task_id) || null
    }
  } catch (err) {
    store.error = err.message
  }
}

async function confirmDone() {
  const weight = Number(completion.weight)
  const payload = { review_result: completion.review }
  // 只有如实填写了合法重量才上报；留空则不打捞量（保持后端 NULL）
  if (completion.weight !== '' && !Number.isNaN(weight) && weight >= 0) {
    payload.collected_weight = weight
  }
  const task = completing.value
  completing.value = null
  if (task) await applyStatus(task, 'done', payload)
}

function openManual() {
  manual.value = true
  manualForm.priority = 5
  // ★ 演示便利：默认选中「机械臂」并把它的坐标预填进目标点。
  //   人工建单原本要手填经纬度 + 从 7 台设备里挑一台，现场容易点错/填错；
  //   预填之后整个动作就是「打开 → 派单」两下。
  //   选不到机械臂时退回第一台在线机器人，再退回第一台。
  const robots = store.robots || []
  const picked =
    robots.find((r) => String(r.chassis || '') === 'arm') ||
    robots.find((r) => r.status === 'online') ||
    robots[0]
  manualForm.robot_id = picked ? picked.robot_id : ''
  const hasCoord = picked && picked.lng != null && picked.lat != null
  manualForm.lng = hasCoord ? String(picked.lng) : ''
  manualForm.lat = hasCoord ? String(picked.lat) : ''
}

/** 把目标点对齐到「当前选中的机器人」所在位置（演示时手边的快捷操作）。 */
function useRobotPosition() {
  const picked = (store.robots || []).find((r) => r.robot_id === manualForm.robot_id)
  if (!picked || picked.lng == null || picked.lat == null) {
    store.error = '该机器人没有可用坐标'
    return
  }
  manualForm.lng = String(picked.lng)
  manualForm.lat = String(picked.lat)
}

async function submitManual() {
  if (creating.value) return
  const lng = Number(manualForm.lng)
  const lat = Number(manualForm.lat)
  if (manualForm.lng === '' || manualForm.lat === '' || Number.isNaN(lng) || Number.isNaN(lat)) {
    store.error = '请输入有效的目标经纬度'
    return
  }
  creating.value = true
  try {
    await tasksApi.create({
      target: { lng, lat },
      robot_id: manualForm.robot_id || undefined,
      priority: manualForm.priority,
    })
    manual.value = false
    await load()
  } catch (err) {
    store.error = err.message
  } finally {
    creating.value = false
  }
}

async function dispatchPending() {
  dispatching.value = true
  try {
    const res = await tasksApi.dispatchPending()
    await load()
    const n = res?.dispatched ?? res?.length ?? 0
    // 成功也要给反馈：此前只在不为 0 时静默改列（列表刷新），补派到底派没派
    // 出去，操作员只能自己数「已派单」列的变化 —— 与「触发补派」的语义不符。
    if (n === 0) {
      toast.warning('当前无待派单对象（可能无可用机器人）')
    } else {
      toast.success(`完成补派 ${n} 个任务`)
    }
  } catch (err) {
    store.error = err.message
  } finally {
    dispatching.value = false
  }
}

/**
 * 处理深链参数（闭环入口）：
 *   ?task=<task_id> —— 从事件详情/通知/智能体跳来，自动展开该工单抽屉；
 *   ?robot=<robot_id> —— 3D 地图点船（无活跃工单时）跳来，筛选该船的工单。
 * 目标在列表外时回退单查接口，避免"跳过来发现列表里没有"的死胡同。
 */
async function applyQuery() {
  const taskId = typeof route.query.task === 'string' ? route.query.task : ''
  const robotId = typeof route.query.robot === 'string' ? route.query.robot : ''
  robotFilter.value = robotId
  if (!taskId) return

  let task = tasks.value.find((t) => t.task_id === taskId)
  if (!task) {
    try {
      task = await tasksApi.detail(taskId)
    } catch (err) {
      store.error = err.message
      return
    }
  }
  if (task) {
    detail.value = task
    // 除了展开抽屉，把对应的卡高亮一下并滚动到视野内 ——
    // 沙盘点船/事件跳过来时，用户要能一眼认出"是这一张"
    highlightTaskId.value = taskId
    clearTimeout(highlightTimer)
    await nextTick()
    cardRefs.get(taskId)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    highlightTimer = setTimeout(() => {
      if (highlightTaskId.value === taskId) highlightTaskId.value = ''
    }, 6000)
  }
}

function clearRobotFilter() {
  robotFilter.value = ''
  const rest = { ...route.query }
  delete rest.robot
  router.replace({ query: rest }).catch(() => {})
}

onMounted(async () => {
  await load()
  await applyQuery()
})

// 同一路由内再次带参数跳入（如从事件详情连点两条工单）也要生效
watch(
  () => [route.query.task, route.query.robot],
  () => applyQuery(),
)

// 有新工单推送时自动刷新列表。
// 不能订阅整个 store：任何字段（stats/devices/error…）变化都会触发 load，
// 高频推送下会把列表刷爆。只盯 taskFeed 队首的时间戳，变了才说明真来了新工单。
watch(
  () => store.taskFeed[0]?._ts,
  (ts, prev) => {
    if (ts && ts !== prev) load()
  },
)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  flex-shrink: 0;
}

.status-chip {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 11px;
  min-height: 34px;
  border-radius: 999px;
  border: 1px solid var(--border);
  cursor: pointer;
  font-size: 13px;
  transition: all 0.15s;
}

.status-chip:hover {
  border-color: var(--border-bright);
}

.status-chip--on {
  border-color: var(--c-primary-dim);
  background: var(--bg-active);
}

.status-chip__label {
  color: var(--text-sub);
}

.status-chip__num {
  font-weight: 600;
  font-family: 'SF Mono', Consolas, monospace;
}

.status-bar__spacer {
  flex: 1;
}

/* ---------- 看板 ---------- */
.kanban {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.kanban__col {
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  min-height: 0;
}

.kanban__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}

.kanban__dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: 1px;
}

.kanban__count {
  font-size: 12px;
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
}

.kanban__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.kanban__empty {
  text-align: center;
  color: var(--text-dim);
  font-size: 12px;
  padding: 20px 0;
}

/* ---------- 工单卡 ---------- */
/* 深链命中的目标卡：发光描边 + 三次呼吸，确保"一堆卡里一眼认出是哪张" */
.tcard.is-highlighted {
  border-color: #18a999 !important;
  box-shadow:
    0 0 0 2px rgba(24, 169, 153, 0.5),
    0 0 20px rgba(24, 169, 153, 0.45);
  animation: tcard-highlight 1.1s ease-in-out 3;
}

@keyframes tcard-highlight {
  0%,
  100% {
    box-shadow:
      0 0 0 2px rgba(24, 169, 153, 0.5),
      0 0 20px rgba(24, 169, 153, 0.45);
  }
  50% {
    box-shadow:
      0 0 0 4px rgba(24, 169, 153, 0.28),
      0 0 30px rgba(24, 169, 153, 0.7);
  }
}

.tcard {
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 10px;
  cursor: pointer;
  transition: border-color 0.15s;
}

.tcard:hover {
  border-color: var(--border-bright);
}

.tcard__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 5px;
}

.tcard__id {
  font-size: 11px;
  color: var(--c-primary);
  font-family: 'SF Mono', Consolas, monospace;
}

.tcard__pri {
  font-size: 10.5px;
  padding: 0 5px;
  border-radius: 8px;
  background: rgba(139, 150, 168, 0.16);
  color: var(--text-sub);
}

.tcard__pri--1 { background: rgba(242, 86, 76, 0.18); color: var(--c-danger); }
.tcard__pri--2 { background: rgba(255, 149, 0, 0.18); color: var(--c-warn); }
.tcard__pri--3 { background: rgba(0, 122, 255, 0.18); color: var(--c-info); }

.tcard__row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
  line-height: 1.75;
}

.tcard__actions {
  display: flex;
  gap: 5px;
  margin-top: 7px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}

.tcard__btn {
  flex: 1;
  min-height: 34px;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--c-primary);
  background: var(--bg-active);
  border: 0;
  border-radius: 9px;
  cursor: pointer;
}

.tcard__btn:hover {
  background: rgba(0, 122, 255, 0.18);
}

.tcard__btn:disabled {
  cursor: not-allowed;
  opacity: 0.42;
}

.tcard__btn--simulation {
  flex: 0 0 auto;
  padding-inline: 10px;
  color: var(--text-main);
  background: var(--bg-hover);
}

.tcard__btn--simulation:not(:disabled):hover {
  color: var(--c-primary);
  background: var(--bg-active);
}

.tcard__btn--danger {
  color: var(--c-danger);
  background: rgba(255, 59, 48, 0.09);
  border-color: transparent;
}

.tcard__btn--danger:hover {
  background: rgba(242, 86, 76, 0.18);
}

/* ---------- 机器人筛选 chip（?robot= 深链） ---------- */
.robot-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--c-primary);
  background: color-mix(in srgb, var(--c-primary) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--c-primary) 35%, transparent);
  border-radius: 999px;
}

.robot-chip__none {
  font-style: normal;
  color: var(--text-sub);
}

.robot-chip__clear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--c-primary);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
}

.robot-chip__clear:hover {
  background: color-mix(in srgb, var(--c-primary) 20%, transparent);
}

/* 900 必须大于 .layout__header 的 300（见 App.vue）。
   这里能生效的前提是模板里用了 <Teleport to="body">：
   只有挂到 body 的抽屉才真正脱离了 .layout__main 的层叠上下文。 */
.drawer {
  position: fixed;
  inset: 0;
  background: rgba(3, 6, 10, 0.6);
  display: flex;
  justify-content: flex-end;
  z-index: 900;
}

.drawer__panel {
  width: 420px;
  max-width: 92vw;
  height: 100%;
  border-radius: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.drawer__close {
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--text-sub);
  font-size: 19px;
  cursor: pointer;
  line-height: 1;
}

.drawer__close:hover {
  color: var(--text-main);
}

.drawer__body {
  flex: 1;
  overflow-y: auto;
}

/* 详情抽屉底部的主操作：一眼可见的「去机械臂执行」入口 */
.drawer__cta {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--separator);
}

/* 人工建单里的「用该机器人位置」：贴着下拉框，不抢视觉重心 */
.manual-form__locate {
  margin-top: 6px;
  font-size: 12px;
  min-height: 28px;
  padding: 0 10px;
}

.drawer__cta-btn {
  width: 100%;
  min-height: 44px;
  font-size: 13.5px;
}

.drawer__cta-btn:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.drawer__cta-hint {
  display: block;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-dim);
  text-align: center;
}

.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 7px 0;
  border-bottom: 1px solid var(--separator);
  font-size: 13px;
}

.kv__k {
  color: var(--text-sub);
  flex-shrink: 0;
}

.kv__v {
  color: var(--text-main);
  text-align: right;
  word-break: break-all;
}

.timeline {
  margin-top: 16px;
}

.timeline__title {
  font-size: 13px;
  color: var(--text-sub);
  margin-bottom: 9px;
}

.timeline__item {
  display: grid;
  grid-template-columns: 16px 1fr auto;
  align-items: center;
  gap: 7px;
  padding: 5px 0;
  font-size: 12.5px;
}

.timeline__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--border-bright);
  margin-left: 3px;
}

.timeline__dot--done {
  background: var(--c-primary);
  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.16);
}

.timeline__label {
  color: var(--text-main);
}

.timeline__time {
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 11.5px;
}

/* ---------- 完成工单录入框（表单控件样式由全局 main.css 提供） ---------- */
.drawer__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

@media (max-width: 1400px) {
  .kanban { grid-template-columns: repeat(3, 1fr); }
}

@media (max-width: 900px) {
  .page {
    height: auto;
    min-height: 100%;
  }

  .status-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    padding: 10px;
  }

  .status-chip {
    justify-content: center;
    min-height: 40px;
  }

  .status-bar__spacer {
    display: none;
  }

  .status-bar > .btn {
    width: 100%;
  }

  .kanban {
    display: flex;
    gap: 10px;
    flex: none;
    overflow-x: auto;
    overflow-y: hidden;
    padding-bottom: 4px;
    scroll-snap-type: x mandatory;
    -webkit-overflow-scrolling: touch;
  }

  .kanban__col {
    flex: 0 0 min(84vw, 340px);
    max-height: 560px;
    scroll-snap-align: start;
  }

  .drawer {
    align-items: flex-end;
  }

  .drawer__panel {
    width: 100%;
    max-width: 100%;
    height: auto;
    max-height: min(88dvh, 720px);
    border-radius: 18px 18px 0 0;
  }

  .drawer__body {
    padding-bottom: calc(16px + env(safe-area-inset-bottom, 0px));
  }

  .drawer__actions .btn {
    flex: 1;
  }
}

@media (max-width: 620px) {
  /* 看板改纵向堆叠：每列全宽、列内限高滚动。
     配合顶部状态 chip 的筛选（点选后 columns 只剩一列），
     就是移动端的「状态 Tab + 单列列表」，替代原先 5 列长横滚。 */
  .kanban {
    display: flex;
    flex-direction: column;
    flex: none;
    overflow: visible;
    scroll-snap-type: none;
  }

  .kanban__col {
    flex: none;
    max-height: none;
    scroll-snap-align: none;
  }

  .kanban__body {
    max-height: 440px;
  }

  .status-chip {
    flex: 1;
    justify-content: center;
  }
}

@media (max-width: 520px) {
  .tcard__row {
    font-size: 12px;
  }

  .timeline__item {
    grid-template-columns: 16px minmax(0, 1fr);
  }

  .timeline__time {
    grid-column: 2;
    font-size: 11px;
  }
}
</style>

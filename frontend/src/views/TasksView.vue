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
          <span class="kanban__count">{{ grouped[col.status]?.length || 0 }}</span>
        </div>

        <div class="kanban__body">
          <article
            v-for="t in grouped[col.status] || []"
            :key="t.task_id"
            class="tcard"
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
                仿真
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

          <div v-if="!grouped[col.status]?.length" class="kanban__empty">暂无工单</div>
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
      <div v-if="detail" class="drawer" @click.self="detail = null">
        <div class="drawer__panel panel">
          <div class="panel-title">
            <span>工单详情 · {{ detail.task_id }}</span>
            <button class="drawer__close" @click="detail = null">×</button>
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
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { tasksApi, downloadBlob } from '@/api'
import { useRealtimeStore } from '@/stores/realtime'
import {
  TASK_STATUS,
  TASK_TRANSITIONS,
  REVIEW_RESULT,
} from '@/utils/constants'
import { fmtCoord, fmtRelative, fmtTime } from '@/utils/format'
import { canWrite } from '@/utils/auth'

const store = useRealtimeStore()
const router = useRouter()

// viewer / 匿名只读：隐藏写操作（后端 require_operator 才是最终裁决）
const canWriteOps = canWrite()

const tasks = ref([])
const detail = ref(null)
const dispatching = ref(false)
const filters = reactive({ status: '' })
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
  manualForm.lng = ''
  manualForm.lat = ''
  manualForm.robot_id = ''
  manualForm.priority = 5
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
    if (n === 0) store.error = '当前无待派单事件（可能无可用机器人）'
  } catch (err) {
    store.error = err.message
  } finally {
    dispatching.value = false
  }
}

onMounted(load)

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
  grid-template-columns: repeat(5, 1fr);
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
  font-size: 10px;
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
  flex: 0 1 64px;
  color: var(--text-main);
  background: var(--bg-hover);
}

.tcard__btn--danger {
  color: var(--c-danger);
  background: rgba(255, 59, 48, 0.09);
  border-color: transparent;
}

.tcard__btn--danger:hover {
  background: rgba(242, 86, 76, 0.18);
}

/* ---------- 抽屉 ---------- */
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

@media (max-width: 520px) {
  .kanban__col {
    flex-basis: 86vw;
  }

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

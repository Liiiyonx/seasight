<template>
  <div class="page">
    <!-- 筛选栏 -->
    <div class="toolbar panel">
      <div class="toolbar__group">
        <label>时间范围</label>
        <select v-model.number="filters.hours" @change="load">
          <option :value="6">近 6 小时</option>
          <option :value="24">近 24 小时</option>
          <option :value="72">近 3 天</option>
          <option :value="168">近 7 天</option>
          <!-- ★ 后端 hours 上限是 720（30 天，见 events.py 的 Query(le=720)）。
               以前这里最大只到 7 天，而演示库的事件往往是更早的种子数据，
               结果就是四个选项全试一遍都是空表、且用户不知道为什么。 -->
          <option :value="720">近 30 天</option>
        </select>
      </div>

      <div class="toolbar__group">
        <label>垃圾类别</label>
        <select v-model="filters.mainClass" @change="load">
          <option value="">全部类别</option>
          <option v-for="c in CLASS_ORDER" :key="c" :value="c">
            {{ classLabel(c) }}
          </option>
        </select>
      </div>

      <div class="toolbar__group">
        <label>处理状态</label>
        <select v-model="filters.status" @change="load">
          <option value="">全部状态</option>
          <option v-for="(label, key) in EVENT_STATUS" :key="key" :value="key">
            {{ label }}
          </option>
        </select>
      </div>

      <div class="toolbar__group">
        <label>设备</label>
        <select v-model="filters.deviceId" @change="load">
          <option value="">全部设备</option>
          <option v-for="d in store.devices" :key="d.device_id" :value="d.device_id">
            {{ d.name }}
          </option>
        </select>
      </div>

      <div class="toolbar__spacer"></div>

      <span class="toolbar__count">共 {{ total }} 条</span>
      <button class="btn" @click="load">刷新</button>
    </div>

    <!-- 列表 -->
    <div class="panel table-panel">
      <table class="data-table">
        <thead>
          <tr>
            <th style="width: 60px">序号</th>
            <th style="width: 130px">发现时间</th>
            <th style="width: 110px">设备</th>
            <th style="width: 100px">类别</th>
            <th style="width: 80px">目标数</th>
            <th style="width: 90px">置信度</th>
            <th style="width: 150px">坐标</th>
            <th style="width: 90px">状态</th>
            <th>关联工单</th>
            <th v-if="canWriteOps" style="width: 80px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(e, i) in items"
            :key="e.event_id"
            :ref="(el) => setRowRef(e.event_id, el)"
            :class="{ 'is-highlighted': highlightEventId === e.event_id }"
          >
            <td class="num text-dim">{{ (page - 1) * pageSize + i + 1 }}</td>
            <td class="num">{{ fmtShortTime(e.event_time) }}</td>
            <td class="text-sub">{{ e.device_id }}</td>
            <td>
              <i class="dot" :style="{ background: classColor(e.main_class) }"></i>
              {{ e.main_class_label || classLabel(e.main_class) }}
            </td>
            <td class="num">{{ e.det_count }}</td>
            <td class="num">{{ fmtConfidence(e.max_confidence) }}</td>
            <td class="num text-sub">{{ fmtCoord(e.lng, e.lat) }}</td>
            <td>
              <span class="badge" :class="`badge-${e.status}`">
                {{ EVENT_STATUS[e.status] || e.status }}
              </span>
            </td>
            <td class="text-dim" style="font-size: 12px">
              {{ taskMap[e.event_id] ? taskMap[e.event_id] : '—' }}
            </td>
            <td v-if="canWriteOps" class="text-dim" style="font-size: 12px">
              <button
                v-if="e.status === 'new'"
                class="link-btn"
                :class="{ 'link-btn--armed': confirmIgnoreId === e.event_id }"
                @click="ignoreEvent(e)"
              >
                {{ confirmIgnoreId === e.event_id ? '确认忽略？' : '忽略' }}
              </button>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="!items.length" class="empty">
        <template v-if="loading">加载中…</template>
        <template v-else>
          <p class="empty__title">没有符合条件的事件</p>
          <!-- 空表最常见的原因不是"真没事件"，而是时间窗太窄（演示库里的
               事件常常是若干天前的种子数据）。这里必须把下一步说清楚，
               否则用户只会以为系统坏了。 -->
          <p class="empty__hint">
            当前时间范围：近{{ hoursLabel }}，共 0 条。
            <template v-if="filters.hours < 720">
              可把上方「时间范围」放宽到「近 30 天」再看。
            </template>
            <template v-else>
              请确认该时段内边缘设备确实有上报。
            </template>
          </p>
        </template>
      </div>

      <!-- 分页 -->
      <div v-if="total > pageSize" class="pager">
        <button class="btn" :disabled="page <= 1" @click="goto(page - 1)">上一页</button>
        <span class="pager__info">
          第 {{ page }} / {{ Math.ceil(total / pageSize) }} 页
        </span>
        <button
          class="btn"
          :disabled="page >= Math.ceil(total / pageSize)"
          @click="goto(page + 1)"
        >
          下一页
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute } from 'vue-router'
import { eventsApi, tasksApi } from '@/api'
import { useRealtimeStore } from '@/stores/realtime'
import { CLASS_ORDER, EVENT_STATUS, classLabel, classColor } from '@/utils/constants'
import { canWrite } from '@/utils/auth'
import { fmtShortTime, fmtConfidence, fmtCoord } from '@/utils/format'

const store = useRealtimeStore()
const route = useRoute()

// viewer / 匿名只读：隐藏「忽略」操作（后端 require_operator 才是最终裁决）
const canWriteOps = canWrite()

const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(30)
const loading = ref(false)
const taskMap = ref({})
const highlightEventId = ref('')
const rowRefs = new Map()
let highlightTimer = null

// 「忽略」两段确认：记住已进入确认态的事件，3 秒无操作自动撤回
const confirmIgnoreId = ref('')
let confirmIgnoreTimer = null

const filters = reactive({
  hours: 24,
  mainClass: '',
  status: '',
  deviceId: '',
})

// 空态提示里要回显"当前到底是多大的时间窗"，否则用户不知道还能往哪调
const HOURS_LABEL = { 6: '6 小时', 24: '24 小时', 72: '3 天', 168: '7 天', 720: '30 天' }
const hoursLabel = computed(() => HOURS_LABEL[filters.hours] || `${filters.hours} 小时`)

async function load() {
  loading.value = true
  try {
    const res = await eventsApi.list({
      hours: filters.hours,
      main_class: filters.mainClass || undefined,
      status: filters.status || undefined,
      device_id: filters.deviceId || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = res?.items || []
    total.value = res?.meta?.total ?? items.value.length
    await buildTaskMap()
  } catch (err) {
    store.error = err.message
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

/** 把事件与工单关联起来，列表里直接能看出"这条有没有被处理" */
async function buildTaskMap() {
  try {
    const res = await tasksApi.list({ page: 1, page_size: 200 })
    const tasks = res?.items || []
    const map = {}
    for (const t of tasks) {
      if (t.event_id) map[t.event_id] = t.task_id
    }
    taskMap.value = map
  } catch {
    /* 关联失败不影响主列表 */
  }
}

function goto(p) {
  page.value = p
  load()
}

async function ignoreEvent(e) {
  // ★ 两段确认。忽略是不可逆的：后端 events.py 只允许 new → ignored/resolved，
  //   没有任何反向入口，点错了只能进库改。以前单击即写库，
  //   与「智能助手」里删除会话的两段确认也不一致 —— 这里对齐。
  if (confirmIgnoreId.value !== e.event_id) {
    confirmIgnoreId.value = e.event_id
    clearTimeout(confirmIgnoreTimer)
    confirmIgnoreTimer = setTimeout(() => {
      if (confirmIgnoreId.value === e.event_id) confirmIgnoreId.value = ''
    }, 3000)
    return
  }

  clearTimeout(confirmIgnoreTimer)
  confirmIgnoreId.value = ''
  try {
    await eventsApi.updateStatus(e.event_id, { status: 'ignored' })
    await load()
  } catch (err) {
    store.error = err.message
  }
}

function setRowRef(eventId, element) {
  if (element) rowRefs.set(eventId, element)
  else rowRefs.delete(eventId)
}

async function focusQueryEvent() {
  const eventId = typeof route.query.event === 'string' ? route.query.event : ''
  clearTimeout(highlightTimer)
  highlightEventId.value = ''
  if (!eventId) return

  let matched = items.value.some((item) => item.event_id === eventId)
  if (!matched) {
    try {
      const detail = await eventsApi.detail(eventId)
      if (detail?.event_id) {
        items.value = [detail, ...items.value]
        total.value = Math.max(total.value, items.value.length)
        matched = true
      }
    } catch (err) {
      store.error = err.message
      return
    }
  }
  if (!matched) return

  await nextTick()
  rowRefs.get(eventId)?.scrollIntoView({
    behavior: 'smooth',
    block: 'center',
  })
  highlightEventId.value = eventId
  highlightTimer = setTimeout(() => {
    if (highlightEventId.value === eventId) highlightEventId.value = ''
  }, 4000)
}

onMounted(async () => {
  await load()
  await focusQueryEvent()
})

watch(
  () => route.query.event,
  async () => {
    await focusQueryEvent()
  },
)

onUnmounted(() => {
  clearTimeout(highlightTimer)
  clearTimeout(confirmIgnoreTimer)
})
</script>

<style scoped>
/* 空态要竖排：全局 .empty 是 flex 居中（横排），
   多行文案会挤在一行。 */
.empty {
  flex-direction: column;
  gap: 6px;
}

.empty__title {
  margin: 0;
  color: var(--text-sub);
  font-size: 13px;
}

.empty__hint {
  max-width: 520px;
  margin: 0;
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1.6;
}

.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 9px 14px;
  flex-shrink: 0;
}

.toolbar__group {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.toolbar__group label {
  color: var(--text-sub);
}

.toolbar__spacer {
  flex: 1;
}

.toolbar__count {
  font-size: 12px;
  color: var(--text-sub);
}

.table-panel {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0;
}

:deep(tr.is-highlighted td) {
  background: rgba(255, 149, 0, 0.14);
}

:deep(tr.is-highlighted td:first-child) {
  box-shadow: inset 3px 0 0 var(--c-warn);
}

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 12px;
  border-top: 1px solid var(--border);
}

.pager__info {
  font-size: 13px;
  color: var(--text-sub);
}

.link-btn {
  padding: 1px 8px;
  font-size: 12px;
  color: var(--c-danger);
  background: transparent;
  border: 1px solid rgba(242, 86, 76, 0.3);
  border-radius: 3px;
  cursor: pointer;
}

.link-btn:hover {
  background: rgba(242, 86, 76, 0.12);
}

/* 进入「确认忽略？」状态：实心告警色，跟普通态在视觉上必须拉开，
   否则用户点完一下看不出自己已经在确认流程里。 */
.link-btn--armed {
  color: #ffffff;
  background: var(--c-danger);
  border-color: var(--c-danger);
  white-space: nowrap;
}

.link-btn--armed:hover {
  background: var(--c-danger);
  filter: brightness(1.08);
}

@media (max-width: 900px) {
  .page {
    height: auto;
    min-height: 100%;
  }

  .toolbar {
    flex-wrap: wrap;
    gap: 10px;
    padding: 12px;
  }

  .toolbar__group {
    flex: 1 1 calc(50% - 5px);
    min-width: 150px;
    align-items: stretch;
    flex-direction: column;
    gap: 5px;
  }

  .toolbar__group select {
    width: 100%;
  }

  .toolbar__spacer {
    display: none;
  }

  .toolbar__count {
    margin-left: auto;
  }

  .table-panel {
    flex: none;
    min-height: 420px;
    max-width: 100%;
    overflow: auto;
    -webkit-overflow-scrolling: touch;
  }

  .table-panel .data-table {
    min-width: 980px;
  }
}

@media (max-width: 520px) {
  .toolbar__group {
    flex-basis: 100%;
  }

  .toolbar > .btn {
    flex: 1 1 0;
  }

  .pager {
    gap: 8px;
    padding: 10px;
  }
}
</style>

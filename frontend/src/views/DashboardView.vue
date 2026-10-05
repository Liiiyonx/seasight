<template>
  <div class="dashboard">
    <!-- 指标卡 -->
    <StatCards :stats="store.stats" class="dashboard__stats" />

    <!-- 主区：左（视频宫格） 中（地图） 右（实时事件） -->
    <div class="dashboard__grid">
      <!-- 视频宫格 -->
      <section class="panel col-video">
        <div class="panel-title">
          <span>现场视频</span>
          <div class="video-switch">
            <button
              v-for="n in [1, 4]"
              :key="n"
              :class="{ on: gridSize === n }"
              @click="gridSize = n"
            >
              {{ n === 1 ? '单画面' : '四宫格' }}
            </button>
          </div>
        </div>
        <div class="video-grid" :class="`video-grid--${gridSize}`">
          <VideoPlayer
            v-for="cam in visibleCameras"
            :key="cam.device_id"
            :device-id="cam.device_id"
            :name="cam.name"
            :stream-url="cam.stream_url || ''"
            :detections="detectionsFor(cam.device_id)"
            class="video-grid__cell"
          />
          <div v-if="!visibleCameras.length" class="empty">暂无可用摄像头</div>
        </div>
      </section>

      <!-- 地图 -->
      <MapPanel
        class="col-map"
        :heatmap="store.heatmap"
        :devices="store.devices"
        :robots="mapRobots"
        :events="mapEvents"
        :decision="agentDecision"
      />

      <!-- 实时事件流 -->
      <section class="panel col-feed">
        <div class="panel-title">
          <span>实时告警</span>
          <span class="text-dim" style="font-size: 12px; font-weight: 400">
            {{ store.recentEvents.length }} 条
          </span>
        </div>
        <div class="feed">
          <div
            v-for="evt in store.recentEvents.slice(0, 30)"
            :key="evt.event_id"
            class="feed__item"
            :class="{ 'feed__item--new': evt._isNew }"
          >
            <div class="feed__head">
              <span class="feed__class">
                <i class="dot" :class="'dot-' + evt.main_class"></i>
                {{ classLabel(evt.main_class) }}
              </span>
              <span class="feed__time">{{ fmtRelative(evt.event_time) }}</span>
            </div>
            <div class="feed__meta">
              <span>{{ evt.device_id }}</span>
              <span>{{ evt.det_count || 1 }} 个目标</span>
              <span class="text-primary">{{ fmtConfidence(evt.max_confidence) }}</span>
            </div>
          </div>
          <div v-if="!store.recentEvents.length" class="empty">暂无告警事件</div>
        </div>
      </section>
    </div>

    <!-- 底部：趋势 + 类别分布 + 机器人状态 -->
    <div class="dashboard__bottom">
      <ChartPanel title="24 小时事件趋势" :option="trendOption" :height="180" class="bottom-chart" />
      <ChartPanel title="垃圾类别分布" :option="classOption" :height="180" class="bottom-chart" />

      <section class="panel robot-panel">
        <div class="panel-title">
          <span>机器人状态</span>
          <span class="text-dim" style="font-size: 12px; font-weight: 400">
            {{ store.onlineRobots.length }}/{{ store.robots.length }} 在线
          </span>
        </div>
        <div class="robot-list">
          <div v-for="r in store.robots" :key="r.robot_id" class="robot-item">
            <div class="robot-item__top">
              <span class="robot-item__name">{{ r.name || r.robot_id }}</span>
              <span class="badge" :class="badgeClass(r.status)">
                {{ STATUS_LABEL[r.status] || r.status }}
              </span>
            </div>
            <div class="robot-item__bars">
              <div class="bar">
                <span class="bar__label">电量</span>
                <div class="bar__track">
                  <div
                    class="bar__fill"
                    :style="{
                      width: `${r.battery ?? 0}%`,
                      background: batteryColor(r.battery),
                    }"
                  ></div>
                </div>
                <span class="bar__val">{{ r.battery ?? '—' }}%</span>
              </div>
              <div class="bar" v-for="(v, k) in (r.bins || {})" :key="k">
                <span class="bar__label">{{ BIN_LABEL[k] || k }}</span>
                <div class="bar__track">
                  <div
                    class="bar__fill bar__fill--bin"
                    :style="{ width: `${(Number(v) || 0) * 100}%` }"
                  ></div>
                </div>
                <span class="bar__val">{{ fmtUsage(v) }}</span>
              </div>
            </div>
          </div>
          <div v-if="!store.robots.length" class="empty">暂无机器人接入</div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
/**
 * 监测大屏 —— 平台的门面，也是评委会第一眼看的东西。
 *
 * 布局取舍：视频与地图是"现场感"来源，必须占最大面积；
 * 告警流放右侧是因为它变化最快，眼睛扫一眼就能读到；
 * 趋势/类别/机器人放底部，属于"态势"而非"现场"。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import StatCards from '@/components/StatCards.vue'
import VideoPlayer from '@/components/VideoPlayer.vue'
import MapPanel from '@/components/MapPanelPrecision.vue'
import ChartPanel from '@/components/ChartPanel.vue'
import { useRealtimeStore } from '@/stores/realtime'
import { statsApi, agentsApi } from '@/api'
import {
  classLabel,
  CLASS_ORDER,
  WASTE_CLASSES,
} from '@/utils/constants'
import { palette, classThemeColor } from '@/utils/palette'
import { fmtRelative, fmtConfidence, fmtUsage, batteryColor } from '@/utils/format'

const store = useRealtimeStore()
const gridSize = ref(4)

// 传给地图的事件必须是稳定引用：模板里直接 slice() 每次渲染都产生新数组，
// 会触发 MapPanel 的 watch 重画覆盖物（哪怕内容一个字没变）
const mapEvents = computed(() => store.recentEvents.slice(0, 60))

// ---------- 派单决策联动 ----------
// 取最近一条"带决策快照"的 agent run，把候选/选中/理由标到地图上。
// 两条纪律：
//  1) 失败静默：大屏的主线是实时态势，不能因为 agent 接口不可用就空掉地图；
//  2) 不造点：只有决策里出现的机器人在 store 的设备列表里真实存在时才上色，
//     否则宁可不标 —— 地图上多一个来路不明的点比少标一个更糟。
const agentDecision = ref(null)
let decisionTimer = null

const mapRobots = computed(() => {
  const base = store.robots || []
  const decision = agentDecision.value
  if (!decision?.selected_robot_id) return base

  const candidates = Array.isArray(decision.candidates) ? decision.candidates : []
  const overlays = new Map()
  for (const candidate of candidates) {
    if (candidate?.robot_id) overlays.set(candidate.robot_id, candidate)
  }
  const selectedId = decision.selected_robot_id
  if (!overlays.has(selectedId)) {
    overlays.set(selectedId, { robot_id: selectedId })
  }

  return base.map((robot) => {
    const overlay = overlays.get(robot.robot_id)
    if (!overlay) return robot
    const isSelected = robot.robot_id === selectedId
    return {
      ...robot,
      decision_role: isSelected ? 'selected' : 'candidate',
      decision_reason: isSelected ? decision.reason || '' : '',
      decision_distance_m: isSelected
        ? decision.distance_m ?? overlay.distance_m
        : overlay.distance_m,
      decision_same_category: !!overlay.same_category_active,
    }
  })
})

async function loadAgentDecision() {
  try {
    const page = await agentsApi.listRuns({ page: 1, page_size: 20 })
    const items = page?.items || []
    const found = items.find((item) => item?.decision?.selected_robot_id)
    agentDecision.value = found?.decision || null
  } catch {
    /* 决策联动是增强项：取不到就退回纯态势地图，不打断大屏 */
  }
}

const STATUS_LABEL = {
  online: '在线',
  offline: '离线',
  idle: '待命',
  navigating: '前往中',
  collecting: '作业中',
  assigned: '已派单',
}

const BIN_LABEL = { foam: '泡沫仓', plastic: '塑胶仓', mixed: '混合仓' }

function badgeClass(status) {
  const map = {
    online: 'badge-online',
    idle: 'badge-online',
    offline: 'badge-offline',
    navigating: 'badge-navigating',
    collecting: 'badge-collecting',
    assigned: 'badge-assigned',
  }
  return map[status] || 'badge-offline'
}

// 只把设备类型是摄像头的放进宫格（无人机没有 RTSP 常驻流）
const cameras = computed(() =>
  store.devices.filter((d) => d.device_type === 'shore_camera'),
)
const visibleCameras = computed(() =>
  gridSize.value === 1 ? cameras.value.slice(0, 1) : cameras.value.slice(0, 4),
)

/** 某设备最近一条事件里的检测框（用于画面叠加） */
function detectionsFor(deviceId) {
  const evt = store.recentEvents.find((e) => e.device_id === deviceId)
  return evt?.detections || []
}

// ---------- 图表 ----------
const trend = ref([])
const classes = ref([])

async function loadCharts() {
  try {
    const [t, c] = await Promise.all([statsApi.trend(24), statsApi.classes(24)])
    trend.value = t || []
    classes.value = c || []
  } catch {
    /* 图表失败不阻断大屏 */
  }
}

const trendOption = computed(() => {
  const p = palette()
  return {
    grid: { left: 42, right: 14, top: 20, bottom: 26 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: trend.value.map((d) => {
        const dt = new Date(d.time)
        return `${String(dt.getHours()).padStart(2, '0')}:00`
      }),
      axisLine: { lineStyle: { color: p.axisLine } },
      axisLabel: { color: p.axisLabel, fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: p.splitLine } },
      axisLabel: { color: p.axisLabel, fontSize: 11 },
    },
    series: [
      {
        type: 'line',
        smooth: true,
        symbol: 'none',
        data: trend.value.map((d) => d.count),
        lineStyle: {
          width: 2,
          color: p.primary,
        },
        areaStyle: {
          color: p.primaryGlow,
        },
      },
    ],
  }
})

const classOption = computed(() => {
  const p = palette()
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} 条 ({d}%)' },
    legend: {
      orient: 'vertical',
      right: 6,
      top: 'center',
      textStyle: { color: p.axisLabel, fontSize: 11 },
      itemWidth: 9,
      itemHeight: 9,
    },
    series: [
      {
        type: 'pie',
        radius: ['48%', '72%'],
        center: ['36%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: p.pieBorder, borderWidth: 1 },
        label: { show: false },
        data: CLASS_ORDER.map((k) => {
          const found = classes.value.find((c) => c.main_class === k)
          return {
            name: WASTE_CLASSES[k].label,
            value: found?.count || 0,
            itemStyle: { color: classThemeColor(k) },
          }
        }).filter((d) => d.value > 0),
      },
    ],
  }
})

// ---------- 定时 ----------
let chartTimer = null

onMounted(async () => {
  await loadCharts()
  chartTimer = setInterval(loadCharts, 60000)
  // 派单决策 20 秒一刷：比图表快，因为它是"刚刚发生了什么"；
  // 比实时告警慢，因为它是叠加在态势上的标注，不需要毫秒级跟随。
  await loadAgentDecision()
  decisionTimer = setInterval(loadAgentDecision, 20000)
})

onUnmounted(() => {
  clearInterval(chartTimer)
  if (decisionTimer) clearInterval(decisionTimer)
  decisionTimer = null
})
</script>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.dashboard__stats {
  flex-shrink: 0;
}

/* 主区三列：视频 3 / 地图 4 / 告警 2.4 */
.dashboard__grid {
  display: grid;
  grid-template-columns: 3fr 4fr 2.4fr;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.col-video,
.col-map,
.col-feed {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

.video-switch {
  display: flex;
  gap: 4px;
}

.video-switch button {
  min-height: 32px;
  padding: 5px 11px;
  font-size: 12px;
  color: var(--text-sub);
  background: var(--bg-panel-2);
  border: 0;
  border-radius: 9px;
  cursor: pointer;
}

.video-switch button.on {
  color: var(--c-primary);
  background: var(--bg-active);
}

.video-grid {
  flex: 1;
  min-height: 0;
  padding: 8px;
  display: grid;
  gap: 6px;
}

.video-grid--1 { grid-template-columns: 1fr; }
.video-grid--4 { grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }

.video-grid__cell {
  min-height: 0;
}

/* 告警流 */
.feed {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px;
}

.feed__item {
  padding: 7px 9px;
  border-radius: 9px;
  border-left: 2px solid transparent;
  transition: background 0.15s;
}

.feed__item:hover {
  background: var(--bg-hover);
}

.feed__item--new {
  border-left-color: var(--c-warn);
  background: color-mix(in srgb, var(--c-warn) 10%, transparent);
}

.feed__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.feed__class {
  font-size: 13px;
  color: var(--text-main);
}

.feed__time {
  font-size: 11px;
  color: var(--text-dim);
}

.feed__meta {
  display: flex;
  gap: 10px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
}

/* 底部一行 */
.dashboard__bottom {
  display: grid;
  grid-template-columns: 1fr 1fr 1.1fr;
  gap: 12px;
  flex-shrink: 0;
}

.bottom-chart {
  min-height: 0;
}

.robot-panel {
  display: flex;
  flex-direction: column;
  max-height: 230px;
}

.robot-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 10px;
}

.robot-item {
  padding: 7px 0;
  border-bottom: 1px solid var(--separator);
}

.robot-item:last-child {
  border-bottom: none;
}

.robot-item__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 5px;
}

.robot-item__name {
  font-size: 13px;
  color: var(--text-main);
}

.robot-item__bars {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.bar {
  display: grid;
  grid-template-columns: 48px 1fr 40px;
  align-items: center;
  gap: 7px;
  font-size: 11px;
}

.bar__label {
  color: var(--text-sub);
}

.bar__track {
  height: 4px;
  background: var(--bg-panel-2);
  border-radius: 2px;
  overflow: hidden;
}

.bar__fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.4s;
}

.bar__fill--bin {
  background: var(--c-info);
}

.bar__val {
  color: var(--text-sub);
  text-align: right;
  font-family: 'SF Mono', Consolas, monospace;
}

@media (max-width: 1180px) {
  .dashboard {
    height: auto;
    min-height: 100%;
  }

  .dashboard__grid { grid-template-columns: 1fr 1.3fr; }
  .col-map { min-height: 368px; }
  .col-feed { grid-column: span 2; max-height: 220px; }
  .robot-panel { max-height: 230px; }
}

@media (max-width: 900px) {
  .dashboard {
    height: auto;
    min-height: 100%;
  }

  .dashboard__grid {
    display: flex;
    flex-direction: column;
  }

  .col-video,
  .col-map,
  .col-feed {
    overflow: visible;
  }

  .video-grid {
    flex: none;
    min-height: 250px;
    padding: 10px;
  }

  .video-grid--4 {
    min-height: 400px;
    grid-template-rows: repeat(2, minmax(180px, 1fr));
  }

  .col-map {
    min-height: 360px;
  }

  .col-feed {
    max-height: 340px;
    overflow: hidden;
  }

  .dashboard__bottom {
    grid-template-columns: 1fr;
  }

  .robot-panel {
    max-height: 360px;
  }
}

@media (max-width: 560px) {
  .video-switch button {
    min-height: 36px;
    padding: 7px 10px;
  }

  .video-grid--4 {
    min-height: 340px;
    grid-template-rows: repeat(2, minmax(150px, 1fr));
  }

  .col-map {
    min-height: 330px;
  }

  .bar {
    grid-template-columns: 44px minmax(0, 1fr) 36px;
  }
}
</style>

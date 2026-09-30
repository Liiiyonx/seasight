<template>
  <div class="map-panel panel">
    <div class="panel-title">
      <span>垃圾分布热力与设备态势</span>
      <div class="map-legend">
        <span v-for="c in legend" :key="c.key" class="map-legend__item">
          <i class="dot" :style="{ background: c.color }"></i>{{ c.label }}
        </span>
      </div>
    </div>

    <!-- 本次派单决策：把"选了谁、为什么"直接写在图上方，不用去翻轨迹 -->
    <div v-if="decision && decision.selected_robot_id" class="map-decision">
      <span class="map-decision__tag">本次派单决策</span>
      <span class="map-decision__text">
        {{ decision.selected_robot_name || decision.selected_robot_id }}
        <template v-if="decision.distance_m != null">（{{ Math.round(Number(decision.distance_m)) }} m）</template>
        — {{ decision.reason || '按策略择优' }}
      </span>
      <span v-if="decision.candidates?.length" class="map-decision__count">
        候选 {{ decision.candidates.length }}
      </span>
    </div>

    <div class="map-panel__body">
      <div
        id="seasight-map"
        ref="mapEl"
        :class="{ 'seasight-map--hidden': mapMode === 'offline' }"
      ></div>

      <svg
        v-if="mapMode === 'offline'"
        class="offline-map"
        viewBox="0 0 1000 700"
        role="img"
        aria-label="连江县海区离线态势图"
      >
        <defs>
          <linearGradient id="precision-water" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#dcecf5" />
            <stop offset="1" stop-color="#c9deeb" />
          </linearGradient>
          <linearGradient id="precision-land" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#fbfcfa" />
            <stop offset="1" stop-color="#edf1ed" />
          </linearGradient>
          <filter id="precision-shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="5" stdDeviation="6" flood-color="#526b7d" flood-opacity="0.18" />
          </filter>
        </defs>

        <rect width="1000" height="700" fill="url(#precision-water)" />
        <path
          d="M-30 112C120 58 258 84 374 146S650 252 1030 172"
          fill="none"
          stroke="#ffffff"
          stroke-opacity="0.22"
          stroke-width="1.2"
        />
        <path
          d="M-34 196C116 142 250 170 370 228S664 332 1038 246"
          fill="none"
          stroke="#9fc5dd"
          stroke-opacity="0.16"
        />

        <path class="offline-map__coast" :d="lianjiangOutline" />
        <path
          class="offline-map__land"
          :d="lianjiangOutline"
          filter="url(#precision-shadow)"
        />

        <g v-if="layerEnabled('heat')" class="offline-map__heat">
          <circle
            v-for="cell in localHeat"
            :key="cell.key"
            :cx="cell.x"
            :cy="cell.y"
            :r="cell.radius"
            :fill="cell.color"
            :fill-opacity="cell.opacity"
          />
        </g>

        <g v-if="layerEnabled('cameras')">
          <circle
            v-for="marker in localCameras"
            :key="marker.id"
            :cx="marker.x"
            :cy="marker.y"
            r="5.5"
            :fill="marker.color"
            stroke="#ffffff"
            stroke-width="1.6"
          />
        </g>

        <g v-if="layerEnabled('robots')">
          <circle
            v-for="marker in localRobots"
            :key="marker.id"
            :cx="marker.x"
            :cy="marker.y"
            r="7"
            :fill="marker.color"
            stroke="#ffffff"
            stroke-width="1.6"
          />
        </g>

        <g v-if="layerEnabled('events')">
          <circle
            v-for="marker in localEvents"
            :key="marker.id"
            :cx="marker.x"
            :cy="marker.y"
            r="4.5"
            :fill="marker.color"
            stroke="#ffffff"
            stroke-width="1.5"
          />
        </g>

        <text class="offline-map__water-label" x="700" y="560">黄岐湾</text>
        <text class="offline-map__ocean-label" x="865" y="625">东 海</text>
      </svg>

      <div v-if="mapMode !== 'loading'" class="map-tools">
        <div class="map-tools__base" role="group" aria-label="底图类型">
          <button
            type="button"
            :class="{ 'is-active': baseMode === 'satellite' }"
            @click="setBaseMode('satellite')"
          >
            高清
          </button>
          <button
            type="button"
            :class="{ 'is-active': baseMode === 'earth' }"
            @click="setBaseMode('earth')"
          >
            影像
          </button>
          <button
            type="button"
            :class="{ 'is-active': baseMode === 'street' }"
            @click="setBaseMode('street')"
          >
            街道
          </button>
        </div>
        <label v-for="layer in layers" :key="layer.key" class="map-tools__item">
          <input type="checkbox" v-model="layer.on" @change="refreshOverlays()" />
          <span>{{ layer.label }}</span>
        </label>
      </div>

      <div v-if="mapMode === 'offline'" class="map-controls" aria-label="离线地图缩放控制">
        <button type="button" title="放大" aria-label="放大地图" @click="changeOfflineZoom(0.2)">+</button>
        <button type="button" title="缩小" aria-label="缩小地图" @click="changeOfflineZoom(-0.2)">−</button>
        <button type="button" title="复位" aria-label="复位地图" @click="resetOfflineView">⊙</button>
      </div>

      <div v-if="mapMode === 'precision'" class="map-source-badge">
        <span class="map-source-badge__dot"></span>
        <span>{{ sourceBadge.title }}</span>
        <small>{{ sourceBadge.subtitle }}</small>
      </div>

      <div v-if="mapMode === 'offline'" class="map-source-badge map-source-badge--offline" :title="offlineReason">
        <span class="map-source-badge__dot"></span>
        <span>连江县本地海区态势图</span>
        <small>{{ offlineReason }}</small>
      </div>

      <div v-if="selectedMarker" class="offline-info" role="dialog" aria-label="点位详情">
        <button type="button" aria-label="关闭点位详情" @click="selectedMarker = null">×</button>
        <strong>{{ selectedMarker.title }}</strong>
        <span v-for="row in selectedMarker.rows" :key="row.label">
          {{ row.label }}：{{ row.value }}
        </span>
      </div>

      <div v-if="mapMode === 'loading'" class="map-loading">
        <span class="map-loading__spinner" aria-hidden="true"></span>
        <span>正在加载高清影像</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { classColor, classLabel, CLASS_ORDER } from '@/utils/constants'
import lianjiangGeo from '@/assets/lianjiang-350122.json'

const props = defineProps({
  heatmap: { type: Array, default: () => [] },
  devices: { type: Array, default: () => [] },
  robots: { type: Array, default: () => [] },
  events: { type: Array, default: () => [] },
  center: { type: Object, default: () => ({ lng: 119.86, lat: 26.35 }) },
  zoom: { type: Number, default: 11 },
  // 最近一次派单决策快照（AgentRunOut.decision）：用于给机器人 marker 上色、
  // 在气泡里补理由、并在图上方写一句"选了谁、为什么"。为 null 时地图回到
  // 纯态势展示，不显示任何决策痕迹。
  decision: { type: Object, default: null },
})

const AMAP_SATELLITE_URL =
  'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}'
const AMAP_STREET_URL =
  'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}'
const ESRI_EARTH_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
const ESRI_LABELS_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}'

const GCJ_AXIS = 6378245
const GCJ_EE = 0.00669342162296594323
const PI = Math.PI

const mapEl = ref(null)
const map = shallowRef(null)
const mapMode = ref('loading')
const baseMode = ref('satellite')
const offlineReason = ref('高清影像暂不可用')
const selectedMarker = ref(null)
const localZoom = ref(1)
const localPan = reactive({ x: 0, y: 0 })

let satelliteLayer = null
let earthLayer = null
let earthLabelsLayer = null
let streetLayer = null
let overlayLayer = null
// 覆盖物登记表：`${类型}:${业务id}` → Leaflet layer。
// WS 高频推送下不再 clearLayers 全量重建（重建会打断开着的 popup、marker 闪烁），
// refreshOverlays 对照新数据做增/改/删。
const overlayMarkers = new Map()
let resizeObserver = null
let tileWatchdog = null
let tileLoadCount = 0
let tileErrorCount = 0

const layers = reactive([
  { key: 'heat', label: '热力分布', on: true },
  { key: 'cameras', label: '摄像头', on: true },
  { key: 'robots', label: '机器人', on: true },
  { key: 'events', label: '事件点', on: true },
])

const legend = computed(() => {
  const items = CLASS_ORDER.map((key) => ({
    key,
    label: classLabel(key),
    color: classColor(key),
  }))
  // 有派单决策叠加时，把决策色也写进图例 —— 图上出现了新颜色的点，
  // 图例不说明就成了"看不懂的彩点"。
  const decisionRoles = new Set((props.robots || []).map((r) => r.decision_role).filter(Boolean))
  for (const [role, tone] of Object.entries(DECISION_STYLE)) {
    if (decisionRoles.has(role)) {
      items.push({ key: `decision-${role}`, label: tone.label, color: tone.color })
    }
  }
  return items
})

const usesGcj02 = computed(() => baseMode.value !== 'earth')

const sourceBadge = computed(() => {
  if (baseMode.value === 'earth') {
    return {
      title: 'Esri 全球卫星影像',
      subtitle: 'World Imagery · 19 级原生细节',
    }
  }
  if (baseMode.value === 'street') {
    return {
      title: '高德中文街道底图',
      subtitle: '道路与行政区注记 · 18 级原生细节',
    }
  }
  return {
    title: '高德高清卫星影像',
    subtitle: '高清卫星底图 · 18 级原生细节',
  }
})

const MAP_WIDTH = 1000
const MAP_HEIGHT = 700
const MAP_PADDING = 44

function collectCoordinates(geometry, result = []) {
  if (!geometry) return result
  if (geometry.type === 'Polygon') {
    geometry.coordinates.flat().forEach((coordinate) => result.push(coordinate))
  } else if (geometry.type === 'MultiPolygon') {
    geometry.coordinates.flat(2).forEach((coordinate) => result.push(coordinate))
  }
  return result
}

const geoCoordinates = lianjiangGeo.features.flatMap((feature) =>
  collectCoordinates(feature.geometry),
)
const GEO_BOUNDS = {
  minLng: Math.min(...geoCoordinates.map(([lng]) => lng)),
  maxLng: Math.max(...geoCoordinates.map(([lng]) => lng)),
  minLat: Math.min(...geoCoordinates.map(([, lat]) => lat)),
  maxLat: Math.max(...geoCoordinates.map(([, lat]) => lat)),
}

function projectLngLat(lng, lat) {
  const width = MAP_WIDTH - MAP_PADDING * 2
  const height = MAP_HEIGHT - MAP_PADDING * 2
  return {
    x: MAP_PADDING + ((lng - GEO_BOUNDS.minLng) / (GEO_BOUNDS.maxLng - GEO_BOUNDS.minLng)) * width,
    y: MAP_PADDING + ((GEO_BOUNDS.maxLat - lat) / (GEO_BOUNDS.maxLat - GEO_BOUNDS.minLat)) * height,
  }
}

function ringToPath(ring) {
  return `${ring
    .map(([lng, lat], index) => {
      const point = projectLngLat(lng, lat)
      return `${index ? 'L' : 'M'}${point.x.toFixed(2)} ${point.y.toFixed(2)}`
    })
    .join(' ')} Z`
}

function geometryToPath(geometry) {
  if (!geometry) return ''
  if (geometry.type === 'Polygon') {
    return geometry.coordinates.map(ringToPath).join(' ')
  }
  if (geometry.type === 'MultiPolygon') {
    return geometry.coordinates.flatMap((polygon) => polygon.map(ringToPath)).join(' ')
  }
  return ''
}

const lianjiangOutline = lianjiangGeo.features
  .map((feature) => geometryToPath(feature.geometry))
  .join(' ')

const maxHeatDensity = computed(() =>
  Math.max(...(props.heatmap || []).map((cell) => Number(cell.density) || 0), 1e-9),
)

const localHeat = computed(() =>
  (props.heatmap || []).map((cell, index) => {
    const ratio = Math.min(1, (Number(cell.density) || 0) / maxHeatDensity.value)
    return {
      key: `${cell.lng}-${cell.lat}-${index}`,
      ...projectLngLat(Number(cell.lng), Number(cell.lat)),
      radius: 10 + ratio * 28,
      color: classColor('foam'),
      opacity: 0.12 + ratio * 0.46,
    }
  }),
)

function markerRows(type, item) {
  if (type === 'camera') {
    return [
      { label: '类型', value: '摄像头' },
      { label: '编号', value: item.device_id },
      { label: '状态', value: item.status === 'online' ? '在线' : '离线' },
    ]
  }
  if (type === 'robot') {
    const rows = [
      { label: '类型', value: '治理机器人' },
      { label: '编号', value: item.robot_id },
      { label: '状态', value: item.status || '未知' },
      { label: '电量', value: item.battery == null ? '未知' : `${item.battery}%` },
    ]
    // 决策联动：气泡里补上"它在这次决策里是什么角色、为什么选它"。
    // 没有决策信息时不加这几行，普通设备气泡保持原样。
    const tone = decisionTone(item)
    if (tone) rows.push({ label: '决策', value: tone.label })
    if (item.decision_reason) rows.push({ label: '理由', value: item.decision_reason })
    if (item.decision_distance_m != null) {
      rows.push({ label: '距事件', value: `${Math.round(Number(item.decision_distance_m))} m` })
    }
    if (item.decision_same_category) rows.push({ label: '顺路', value: '有同类在跑，可顺路' })
    return rows
  }
  return [
    { label: '设备', value: item.device_id || '未关联' },
    { label: '数量', value: item.det_count || 1 },
    {
      label: '置信度',
      value: item.max_confidence
        ? `${(Number(item.max_confidence) * 100).toFixed(0)}%`
        : '未知',
    },
  ]
}

const localCameras = computed(() =>
  (props.devices || [])
    .filter((device) => device.device_type !== 'robot')
    .map((device) => ({
      id: device.device_id,
      ...projectLngLat(Number(device.lng), Number(device.lat)),
      color: device.status === 'online' ? '#18a999' : '#7a8798',
      title: device.name || device.device_id,
      rows: markerRows('camera', device),
    })),
)

const localRobots = computed(() =>
  (props.robots || []).map((robot) => ({
    id: robot.robot_id,
    ...projectLngLat(Number(robot.lng), Number(robot.lat)),
    // 与在线图层共用同一套配色（含派单决策高亮），离线态不能是另一套口径
    color: robotColor(robot),
    title: robot.name || robot.robot_id,
    rows: markerRows('robot', robot),
  })),
)

const localEvents = computed(() =>
  (props.events || []).map((event, index) => ({
    id: event.event_id || `event-${index}`,
    ...projectLngLat(Number(event.lng), Number(event.lat)),
    color: classColor(event.main_class),
    title: `${classLabel(event.main_class)} ${event.det_count || 1} 个`,
    rows: markerRows('event', event),
  })),
)

const offlineTransform = computed(() => {
  const centerX = MAP_WIDTH / 2
  const centerY = MAP_HEIGHT / 2
  return [
    `translate(${localPan.x} ${localPan.y})`,
    `translate(${centerX} ${centerY})`,
    `scale(${localZoom.value})`,
    `translate(${-centerX} ${-centerY})`,
  ].join(' ')
})

function layerEnabled(key) {
  return layers.find((layer) => layer.key === key)?.on !== false
}

function validLatLng(item) {
  const lng = Number(item.lng)
  const lat = Number(item.lat)
  return Number.isFinite(lng) && Number.isFinite(lat) && lng >= -180 && lng <= 180 && lat >= -90 && lat <= 90
}

function outsideChina(lng, lat) {
  return lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271
}

function transformLat(lng, lat) {
  let ret =
    -100 +
    2 * lng +
    3 * lat +
    0.2 * lat * lat +
    0.1 * lng * lat +
    0.2 * Math.sqrt(Math.abs(lng))
  ret += ((20 * Math.sin(6 * lng * PI) + 20 * Math.sin(2 * lng * PI)) * 2) / 3
  ret += ((20 * Math.sin(lat * PI) + 40 * Math.sin((lat / 3) * PI)) * 2) / 3
  ret +=
    ((160 * Math.sin((lat / 12) * PI) + 320 * Math.sin((lat * PI) / 30)) * 2) /
    3
  return ret
}

function transformLng(lng, lat) {
  let ret =
    300 +
    lng +
    2 * lat +
    0.1 * lng * lng +
    0.1 * lng * lat +
    0.1 * Math.sqrt(Math.abs(lng))
  ret += ((20 * Math.sin(6 * lng * PI) + 20 * Math.sin(2 * lng * PI)) * 2) / 3
  ret += ((20 * Math.sin(lng * PI) + 40 * Math.sin((lng / 3) * PI)) * 2) / 3
  ret +=
    ((150 * Math.sin((lng / 12) * PI) + 300 * Math.sin((lng / 30) * PI)) * 2) /
    3
  return ret
}

function wgs84ToGcj02(lng, lat) {
  if (outsideChina(lng, lat)) return [lng, lat]
  const dLat = transformLat(lng - 105, lat - 35)
  const dLng = transformLng(lng - 105, lat - 35)
  const radLat = (lat / 180) * PI
  let magic = Math.sin(radLat)
  magic = 1 - GCJ_EE * magic * magic
  const sqrtMagic = Math.sqrt(magic)
  const outLat =
    lat +
    (dLat * 180) /
      (((GCJ_AXIS * (1 - GCJ_EE)) / (magic * sqrtMagic)) * PI)
  const outLng =
    lng +
    (dLng * 180) /
      ((GCJ_AXIS / sqrtMagic) * Math.cos(radLat) * PI)
  return [outLng, outLat]
}

function displayLatLng(item) {
  const lng = Number(item.lng)
  const lat = Number(item.lat)
  const [shownLng, shownLat] = usesGcj02.value
    ? wgs84ToGcj02(lng, lat)
    : [lng, lat]
  return [shownLat, shownLng]
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function popupContent(title, rows) {
  return `
    <div class="seasight-popup">
      <strong>${escapeHtml(title)}</strong>
      ${rows
        .map(
          (row) =>
            `<span><b>${escapeHtml(row.label)}</b>${escapeHtml(row.value)}</span>`,
        )
        .join('')}
    </div>
  `
}

function vectorIcon(type, color, ring = false) {
  // ring：派单决策选中的机器人加一圈虚线光环，静态截图里也能一眼认出来
  // （只靠颜色的话，投影仪偏色时"橙"和"蓝"会难分）。
  const robotRing = ring
    ? `<circle cx="16" cy="16" r="15.2" fill="none" stroke="${color}" stroke-width="1.5" stroke-dasharray="3.4 2.6"/>`
    : ''
  const html =
    type === 'robot'
      ? `<svg viewBox="0 0 32 32" aria-hidden="true">
           ${robotRing}
           <circle cx="16" cy="16" r="14" fill="${color}" opacity=".2"/>
           <path d="M7 18h18l-3.5 5H10.5L7 18Z" fill="${color}" stroke="#fff" stroke-width="1.5"/>
           <path d="M10.5 17h11l-2.2-6H12.7l-2.2 6Z" fill="${color}" stroke="#fff" stroke-width="1.5"/>
         </svg>`
      : `<svg viewBox="0 0 32 38" aria-hidden="true">
           <path d="M16 2C9.4 2 4 7.4 4 14c0 8.8 12 22 12 22s12-13.2 12-22C28 7.4 22.6 2 16 2Z"
                 fill="${color}" stroke="#fff" stroke-width="1.6"/>
           <circle cx="16" cy="14" r="4.1" fill="#122330" opacity=".7"/>
         </svg>`

  return L.divIcon({
    className: `seasight-marker seasight-marker--${type}`,
    html,
    iconSize: type === 'robot' ? [34, 34] : [30, 36],
    iconAnchor: type === 'robot' ? [17, 17] : [15, 34],
    popupAnchor: type === 'robot' ? [0, -16] : [0, -31],
  })
}

function createBaseLayers() {
  satelliteLayer = L.tileLayer(AMAP_SATELLITE_URL, {
    subdomains: ['1', '2', '3', '4'],
    minZoom: 3,
    maxNativeZoom: 18,
    maxZoom: 20,
    detectRetina: true,
    crossOrigin: true,
    attribution: 'Imagery &copy; <a href="https://www.amap.com/">高德地图</a>',
    className: 'seasight-satellite-tiles',
  })

  earthLayer = L.tileLayer(ESRI_EARTH_URL, {
    minZoom: 3,
    maxNativeZoom: 19,
    maxZoom: 20,
    detectRetina: true,
    crossOrigin: true,
    attribution:
      'Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community',
    className: 'seasight-satellite-tiles',
  })

  earthLabelsLayer = L.tileLayer(ESRI_LABELS_URL, {
    pane: 'seasight-labels',
    minZoom: 3,
    maxNativeZoom: 19,
    maxZoom: 20,
    detectRetina: true,
    crossOrigin: true,
    opacity: 0.9,
    className: 'seasight-label-tiles',
  })

  streetLayer = L.tileLayer(AMAP_STREET_URL, {
    subdomains: ['1', '2', '3', '4'],
    minZoom: 3,
    maxNativeZoom: 18,
    maxZoom: 20,
    detectRetina: true,
    crossOrigin: true,
    attribution: 'Map &copy; <a href="https://www.amap.com/">高德地图</a>',
    className: 'seasight-street-tiles',
  })

  ;[satelliteLayer, earthLayer, earthLabelsLayer, streetLayer].forEach((layer) => {
    layer.on('tileload', () => {
      tileLoadCount += 1
    })
    layer.on('tileerror', () => {
      tileErrorCount += 1
    })
  })
}

function installBaseLayer() {
  if (!map.value) return
  ;[satelliteLayer, earthLayer, earthLabelsLayer, streetLayer].forEach((layer) => {
    if (layer && map.value.hasLayer(layer)) map.value.removeLayer(layer)
  })

  if (baseMode.value === 'street') {
    if (!streetLayer) createBaseLayers()
    streetLayer.addTo(map.value)
    return
  }

  if (baseMode.value === 'earth') {
    if (!earthLayer) createBaseLayers()
    earthLayer.addTo(map.value)
    earthLabelsLayer.addTo(map.value)
    return
  }

  if (!satelliteLayer) createBaseLayers()
  satelliteLayer.addTo(map.value)
}

function setBaseMode(mode) {
  if (baseMode.value === mode || !map.value) return
  baseMode.value = mode
  tileLoadCount = 0
  tileErrorCount = 0
  installBaseLayer()
  // 底图换了坐标系（GCJ-02 ↔ WGS84），所有点位必须重算，走全量重建
  refreshOverlays(true)
  if (validLatLng(props.center)) {
    map.value.setView(displayLatLng(props.center), Number(props.zoom) || map.value.getZoom(), {
      animate: true,
    })
  }
}

/**
 * 某一类覆盖物的增量同步：对照 overlayMarkers 做增/改/删。
 * keyOf 给每条数据一个稳定业务 id；create 新建 layer；update 原地改已有 layer。
 */
function syncOverlayKind(kind, items, keyOf, create, update) {
  if (!overlayLayer) return
  const seen = new Set()
  for (const item of items) {
    const key = `${kind}:${keyOf(item)}`
    seen.add(key)
    const existing = overlayMarkers.get(key)
    if (existing) {
      update(existing, item)
    } else {
      const layer = create(item)
      layer.addTo(overlayLayer)
      overlayMarkers.set(key, layer)
    }
  }
  for (const [key, layer] of overlayMarkers) {
    if (key.startsWith(`${kind}:`) && !seen.has(key)) {
      overlayLayer.removeLayer(layer)
      overlayMarkers.delete(key)
    }
  }
}

function clearOverlayKind(kind) {
  if (!overlayLayer) return
  for (const [key, layer] of overlayMarkers) {
    if (key.startsWith(`${kind}:`)) {
      overlayLayer.removeLayer(layer)
      overlayMarkers.delete(key)
    }
  }
}

function robotColor(robot) {
  const tone = decisionTone(robot)
  if (tone) return tone.color
  const busy = ['navigating', 'collecting', 'assigned'].includes(robot.status)
  const online = robot.status === 'online' || busy
  return busy ? '#18a999' : online ? '#3f8ee8' : '#7a8798'
}

// ---------- 派单决策联动 ----------
// 大屏上的机器人 marker 本来就在画。给它们多带一个 decision_role 字段，
// 就能把"这次派单选了谁、还有哪些候选"直接标在图上 —— 换来的是
// 评委不用读表格轨迹，抬眼就能看出决策落点。色系沿用 Agent 控制台：
// 选中＝橙、候选＝紫（与研判 Agent 同色系）。
const DECISION_STYLE = {
  selected: { color: '#ff9500', label: '本次派单选中' },
  candidate: { color: '#7a5af8', label: '本次派单候选' },
}

function decisionTone(robot) {
  return DECISION_STYLE[robot?.decision_role] || null
}

function syncHeat() {
  const cells = (props.heatmap || []).filter(validLatLng)
  const maxDensity = Math.max(...cells.map((cell) => Number(cell.density) || 0), 1e-9)
  const styleOf = (cell) => {
    const ratio = Math.min(1, (Number(cell.density) || 0) / maxDensity)
    return { radius: 180 + ratio * 540, fillOpacity: 0.1 + ratio * 0.38 }
  }
  syncOverlayKind(
    'heat',
    cells,
    (cell) => `${cell.lng}-${cell.lat}`,
    (cell) =>
      L.circle(displayLatLng(cell), {
        pane: 'seasight-overlays',
        stroke: false,
        fillColor: classColor('foam'),
        interactive: false,
        className: 'seasight-leaflet-heat',
        ...styleOf(cell),
      }),
    (layer, cell) => {
      const style = styleOf(cell)
      layer.setLatLng(displayLatLng(cell))
      layer.setRadius(style.radius)
      layer.setStyle({ fillOpacity: style.fillOpacity })
    },
  )
}

function cameraPopup(device) {
  return popupContent(device.name || device.device_id, markerRows('camera', device))
}

function cameraIcon(device) {
  return vectorIcon('camera', device.status === 'online' ? '#18a999' : '#7a8798')
}

function syncCameras() {
  syncOverlayKind(
    'camera',
    (props.devices || []).filter((device) => device.device_type !== 'robot' && validLatLng(device)),
    (device) => device.device_id,
    (device) => {
      const marker = L.marker(displayLatLng(device), {
        icon: cameraIcon(device),
        title: device.name || device.device_id,
        riseOnHover: true,
      })
      marker.bindPopup(cameraPopup(device), {
        className: 'seasight-popup-shell',
        closeButton: true,
        offset: [0, 4],
      })
      return marker
    },
    (marker, device) => {
      marker.setLatLng(displayLatLng(device))
      marker.setIcon(cameraIcon(device))
      marker.setPopupContent(cameraPopup(device))
    },
  )
}

function robotPopup(robot) {
  return popupContent(robot.name || robot.robot_id, markerRows('robot', robot))
}

function syncRobots() {
  syncOverlayKind(
    'robot',
    (props.robots || []).filter(validLatLng),
    (robot) => robot.robot_id,
    (robot) => {
      const marker = L.marker(displayLatLng(robot), {
        icon: vectorIcon('robot', robotColor(robot), robot.decision_role === 'selected'),
        title: robot.name || robot.robot_id,
        riseOnHover: true,
      })
      marker.bindPopup(robotPopup(robot), {
        className: 'seasight-popup-shell',
        closeButton: true,
        offset: [0, 2],
      })
      return marker
    },
    (marker, robot) => {
      marker.setLatLng(displayLatLng(robot))
      marker.setIcon(vectorIcon('robot', robotColor(robot), robot.decision_role === 'selected'))
      marker.setPopupContent(robotPopup(robot))
    },
  )
}

function eventPopup(event) {
  return popupContent(
    `${classLabel(event.main_class)} ${event.det_count || 1} 个`,
    markerRows('event', event),
  )
}

function syncEvents() {
  syncOverlayKind(
    'event',
    (props.events || []).filter(validLatLng),
    // 事件必有 event_id；坐标+类别兜底只是防御
    (event) => event.event_id || `${event.lng}-${event.lat}-${event.main_class}`,
    (event) => {
      const marker = L.circleMarker(displayLatLng(event), {
        pane: 'seasight-overlays',
        radius: 6.5,
        color: '#ffffff',
        weight: 1.8,
        fillColor: classColor(event.main_class),
        fillOpacity: 0.96,
        className: 'seasight-event-marker',
      })
      marker.bindPopup(eventPopup(event), {
        className: 'seasight-popup-shell',
        closeButton: true,
        offset: [0, -2],
      })
      return marker
    },
    (marker, event) => {
      marker.setLatLng(displayLatLng(event))
      marker.setStyle({ fillColor: classColor(event.main_class) })
      marker.setPopupContent(eventPopup(event))
    },
  )
}

/**
 * rebuild=true 用于底图坐标系切换（GCJ-02 ↔ WGS84）：所有点位都要重算，
 * 增量更新没有意义，直接清空重建。图层开关变化走增量：关掉的类整类移除。
 */
function refreshOverlays(rebuild = false) {
  if (!map.value) return
  if (!overlayLayer) {
    overlayLayer = L.layerGroup().addTo(map.value)
  }
  if (rebuild) {
    overlayLayer.clearLayers()
    overlayMarkers.clear()
  }

  if (layerEnabled('heat')) syncHeat()
  else clearOverlayKind('heat')
  if (layerEnabled('cameras')) syncCameras()
  else clearOverlayKind('camera')
  if (layerEnabled('robots')) syncRobots()
  else clearOverlayKind('robot')
  if (layerEnabled('events')) syncEvents()
  else clearOverlayKind('event')
}

function initializeOffline(reason) {
  offlineReason.value = reason
  mapMode.value = 'offline'
  if (map.value) {
    map.value.remove()
    map.value = null
  }
}

function initializePrecisionMap() {
  if (!mapEl.value || map.value) return
  mapMode.value = 'loading'

  const instance = L.map(mapEl.value, {
    center: displayLatLng(props.center),
    zoom: Number(props.zoom) || 11,
    minZoom: 3,
    maxZoom: 20,
    zoomControl: false,
    attributionControl: true,
    preferCanvas: true,
    wheelPxPerZoomLevel: 110,
    zoomSnap: 0.5,
    zoomDelta: 1,
  })

  L.control.zoom({ position: 'topleft' }).addTo(instance)
  instance.createPane('seasight-labels')
  instance.getPane('seasight-labels').style.zIndex = 410
  instance.getPane('seasight-labels').style.pointerEvents = 'none'
  instance.createPane('seasight-overlays')
  instance.getPane('seasight-overlays').style.zIndex = 430

  map.value = instance
  createBaseLayers()
  installBaseLayer()
  refreshOverlays()
  mapMode.value = 'precision'

  requestAnimationFrame(() => instance.invalidateSize())
  tileWatchdog = window.setTimeout(() => {
    if (tileLoadCount === 0 && tileErrorCount > 2) {
      initializeOffline('高清影像瓦片加载失败，已切换本地态势图')
    }
  }, 12000)
}

function clampLocalZoom(value) {
  return Math.min(2.8, Math.max(0.8, value))
}

function changeOfflineZoom(delta) {
  localZoom.value = clampLocalZoom(localZoom.value + delta)
}

function resetOfflineView() {
  localZoom.value = 1
  localPan.x = 0
  localPan.y = 0
  selectedMarker.value = null
}

// 覆盖物签名：只拼"会影响画面"的字段（业务 id + 坐标 + 状态/数值）。
// store 每次 WS 推送都可能整体替换数组引用，盯引用等于每次都全量重画；
// 盯签名则内容没变就不动手，变了也交给增量同步去改。
const overlaySignature = computed(() =>
  [
    (props.heatmap || []).map((c) => `${c.lng},${c.lat},${c.density}`).join('|'),
    (props.devices || []).map((d) => `${d.device_id},${d.lng},${d.lat},${d.status}`).join('|'),
    (props.robots || [])
      .map((r) => `${r.robot_id},${r.lng},${r.lat},${r.status},${r.battery},${r.decision_role || ''}`)
      .join('|'),
    (props.events || [])
      .map((e) => `${e.event_id},${e.lng},${e.lat},${e.main_class},${e.det_count},${e.max_confidence}`)
      .join('|'),
  ].join('#'),
)

watch(overlaySignature, () => refreshOverlays())

watch(
  () => [props.center?.lng, props.center?.lat, props.zoom],
  () => {
    if (!map.value) return
    map.value.setView(
      displayLatLng(props.center),
      Number(props.zoom) || map.value.getZoom(),
      { animate: true },
    )
  },
)

onMounted(() => {
  initializePrecisionMap()
  if (mapEl.value && window.ResizeObserver) {
    resizeObserver = new ResizeObserver(() => map.value?.invalidateSize())
    resizeObserver.observe(mapEl.value)
  }
})

onUnmounted(() => {
  window.clearTimeout(tileWatchdog)
  resizeObserver?.disconnect()
  overlayLayer = null
  overlayMarkers.clear()
  if (map.value) {
    map.value.remove()
    map.value = null
  }
})
</script>

<style scoped>
.map-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.map-panel__body {
  position: relative;
  flex: 1;
  min-height: 360px;
  overflow: hidden;
  border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  background: #d7e4ec;
}

#seasight-map {
  width: 100%;
  height: 100%;
  min-height: 360px;
  background: #d7e4ec;
}

.seasight-map--hidden {
  visibility: hidden;
}

.offline-map {
  position: absolute;
  inset: 0;
  z-index: 1;
  width: 100%;
  height: 100%;
  background: #dceaf5;
}

.offline-map__land {
  fill: #f9fbf8;
}

.offline-map__coast {
  fill: #eef3ef;
  stroke: #b7c8d3;
  stroke-width: 1.2;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
}

.offline-map__heat {
  pointer-events: none;
}

.offline-map__water-label,
.offline-map__ocean-label {
  fill: #729fbe;
  stroke: rgba(229, 240, 248, 0.74);
  stroke-width: 2.4;
  paint-order: stroke;
}

.offline-map__water-label {
  font-size: 11px;
  letter-spacing: 2.4px;
}

.offline-map__ocean-label {
  fill: #83a8c1;
  font-size: 15px;
  letter-spacing: 8px;
  opacity: 0.72;
}

.map-controls {
  position: absolute;
  z-index: 1000;
  top: 56px;
  left: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.78);
  border-radius: 13px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow:
    0 1px 2px rgba(24, 44, 58, 0.08),
    0 8px 24px rgba(24, 44, 58, 0.14);
  backdrop-filter: blur(18px) saturate(1.25);
  -webkit-backdrop-filter: blur(18px) saturate(1.25);
}

.map-controls button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #3f4a51;
  font-size: 18px;
  cursor: pointer;
}

.map-controls button + button {
  border-top: 1px solid rgba(60, 60, 67, 0.13);
}

.map-controls button:hover {
  background: rgba(255, 255, 255, 0.9);
  color: #007aff;
}

.map-source-badge {
  position: absolute;
  z-index: 1000;
  bottom: 24px;
  left: 10px;
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 0 7px;
  padding: 8px 11px;
  border: 1px solid rgba(255, 255, 255, 0.82);
  border-radius: 11px;
  background: rgba(252, 253, 254, 0.84);
  color: #35434b;
  box-shadow:
    0 1px 2px rgba(37, 51, 61, 0.08),
    0 7px 22px rgba(37, 51, 61, 0.12);
  backdrop-filter: blur(18px) saturate(1.25);
  -webkit-backdrop-filter: blur(18px) saturate(1.25);
  pointer-events: none;
}

.map-source-badge__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #34c759;
  box-shadow: 0 0 0 4px rgba(52, 199, 89, 0.13);
}

.map-source-badge > span:nth-child(2) {
  font-size: 11px;
  font-weight: 600;
}

.map-source-badge small {
  grid-column: 2;
  color: #71808a;
  font-size: 9px;
  line-height: 1.3;
}

.map-source-badge--offline .map-source-badge__dot {
  background: #ff9500;
  box-shadow: 0 0 0 4px rgba(255, 149, 0, 0.13);
}

.offline-info {
  position: absolute;
  z-index: 1100;
  top: 10px;
  left: 54px;
  display: flex;
  flex-direction: column;
  min-width: 190px;
  max-width: min(280px, calc(100% - 190px));
  gap: 4px;
  padding: 11px 34px 11px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--bg-panel) 96%, transparent);
  box-shadow: 0 12px 34px rgba(16, 49, 65, 0.18);
  color: var(--text-sub);
  font-size: 11.5px;
  line-height: 1.35;
  backdrop-filter: blur(16px) saturate(1.2);
  -webkit-backdrop-filter: blur(16px) saturate(1.2);
}

.offline-info strong {
  color: var(--text-main);
  font-size: 13px;
}

.offline-info > button {
  position: absolute;
  top: 5px;
  right: 5px;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-dim);
  font-size: 18px;
  cursor: pointer;
}

.map-loading {
  position: absolute;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  background: linear-gradient(145deg, #dfeaf1, #d1e0e9);
  color: #53636d;
  font-size: 12px;
}

.map-loading__spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(83, 99, 109, 0.2);
  border-top-color: #007aff;
  border-radius: 50%;
  animation: map-spin 0.8s linear infinite;
}

@keyframes map-spin {
  to { transform: rotate(360deg); }
}

.map-legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
  color: var(--text-sub);
  font-size: 12px;
  font-weight: 400;
}

.map-legend__item {
  display: flex;
  align-items: center;
  white-space: nowrap;
}

/* ---------- 本次派单决策（图上方一行） ---------- */
.map-decision {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 12px 8px;
  padding: 7px 11px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, #ff9500 34%, transparent);
  background: color-mix(in srgb, #ff9500 9%, transparent);
  font-size: 12px;
  line-height: 1.5;
}

.map-decision__tag {
  flex: 0 0 auto;
  padding: 1px 8px;
  border-radius: 999px;
  background: #ff9500;
  color: #ffffff;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.map-decision__text { color: var(--text-main); min-width: 0; }

.map-decision__count {
  margin-left: auto;
  color: var(--text-dim);
  font-size: 11.5px;
  white-space: nowrap;
}

.map-tools {
  position: absolute;
  z-index: 1000;
  top: 10px;
  right: 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 9px 10px;
  border: 1px solid rgba(255, 255, 255, 0.82);
  border-radius: 13px;
  background: rgba(252, 253, 254, 0.85);
  box-shadow:
    0 1px 2px rgba(24, 44, 58, 0.07),
    0 8px 24px rgba(24, 44, 58, 0.13);
  color: #4f5c63;
  font-size: 12px;
  backdrop-filter: blur(18px) saturate(1.25);
  -webkit-backdrop-filter: blur(18px) saturate(1.25);
}

.map-tools__base {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 3px;
  padding: 3px;
  border-radius: 9px;
  background: rgba(118, 128, 138, 0.13);
}

.map-tools__base button {
  min-height: 28px;
  padding: 4px 9px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #5c6971;
  font-size: 11px;
  font-weight: 550;
  cursor: pointer;
}

.map-tools__base button.is-active {
  background: #ffffff;
  box-shadow: 0 1px 4px rgba(24, 44, 58, 0.14);
  color: #007aff;
}

.map-tools__item {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}

.map-tools__item input {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: #007aff;
  cursor: pointer;
}

:deep(.leaflet-container) {
  background: #d6e3eb;
  color: var(--text-main);
  font-family: inherit;
}

:deep(.leaflet-tile-pane) {
  filter: saturate(1.03) contrast(1.04);
}

:deep(.seasight-label-tiles) {
  filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.35));
}

:deep(.leaflet-control-zoom) {
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.8) !important;
  border-radius: 13px !important;
  box-shadow:
    0 1px 2px rgba(24, 44, 58, 0.08),
    0 8px 24px rgba(24, 44, 58, 0.14) !important;
}

:deep(.leaflet-control-zoom a) {
  width: 40px !important;
  height: 40px !important;
  border: 0 !important;
  background: rgba(255, 255, 255, 0.84) !important;
  color: #3f4a51 !important;
  font: 20px/40px -apple-system, BlinkMacSystemFont, sans-serif !important;
  backdrop-filter: blur(18px) saturate(1.25);
  -webkit-backdrop-filter: blur(18px) saturate(1.25);
}

:deep(.leaflet-control-zoom a + a) {
  border-top: 1px solid rgba(60, 60, 67, 0.13) !important;
}

:deep(.leaflet-control-zoom a:hover) {
  background: rgba(255, 255, 255, 0.98) !important;
  color: #007aff !important;
}

:deep(.leaflet-control-attribution) {
  padding: 3px 6px;
  border-radius: 7px 0 0 0;
  background: rgba(255, 255, 255, 0.72);
  color: #63717a;
  font-size: 9px;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
}

:deep(.leaflet-control-attribution a) {
  color: #466879;
}

:deep(.seasight-marker) {
  border: 0;
  background: transparent;
}

:deep(.seasight-marker svg) {
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
  filter: drop-shadow(0 3px 5px rgba(6, 24, 35, 0.28));
}

:deep(.seasight-event-marker) {
  filter: drop-shadow(0 2px 4px rgba(6, 24, 35, 0.3));
}

:deep(.seasight-leaflet-heat) {
  filter: blur(1.3px);
}

:deep(.seasight-popup-shell .leaflet-popup-content-wrapper) {
  border: 1px solid rgba(60, 60, 67, 0.12);
  border-radius: 13px;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: 0 14px 38px rgba(16, 49, 65, 0.2);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
}

:deep(.seasight-popup-shell .leaflet-popup-content) {
  margin: 11px 13px;
}

:deep(.seasight-popup-shell .leaflet-popup-tip) {
  background: rgba(255, 255, 255, 0.96);
}

:deep(.seasight-popup) {
  display: flex;
  min-width: 160px;
  flex-direction: column;
  gap: 3px;
  color: #5f6b73;
  font-size: 11px;
  line-height: 1.45;
}

:deep(.seasight-popup strong) {
  margin-bottom: 2px;
  color: #26333b;
  font-size: 13px;
}

:deep(.seasight-popup b) {
  display: inline-block;
  min-width: 42px;
  color: #879198;
  font-weight: 500;
}

@media (max-width: 760px) {
  .map-panel .panel-title {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .map-legend {
    width: 100%;
    justify-content: flex-start;
    flex-wrap: nowrap;
    gap: 12px;
    padding-bottom: 2px;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .map-legend::-webkit-scrollbar {
    display: none;
  }

  .map-panel__body,
  #seasight-map {
    min-height: 300px;
  }

  .map-tools {
    top: 8px;
    right: 8px;
    display: grid;
    grid-template-columns: repeat(2, auto);
    gap: 7px 10px;
    padding: 7px 8px;
  }

  .map-tools__base {
    grid-column: 1 / -1;
  }

  .map-source-badge {
    bottom: 22px;
  }

  .offline-info {
    top: auto;
    right: 10px;
    bottom: 78px;
    left: 82px;
    min-width: 0;
    max-width: none;
  }
}

@media (max-width: 380px) {
  .map-panel__body,
  #seasight-map {
    min-height: 270px;
  }

  .map-tools {
    font-size: 11px;
  }

  .map-tools__item input {
    width: 15px;
    height: 15px;
  }

  .map-source-badge small {
    display: none;
  }
}
</style>

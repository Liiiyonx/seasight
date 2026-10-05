<template>
  <div class="sim-map">
    <div ref="mapEl" class="sim-map__canvas"></div>

    <div class="sim-map__legend" aria-label="仿真图层图例">
      <span><i class="is-robot"></i>机器人</span>
      <span><i class="is-target"></i>目标点</span>
      <span><i class="is-track"></i>已走轨迹</span>
      <span><i class="is-route"></i>剩余航线</span>
    </div>

    <div class="sim-map__tools">
      <div class="sim-map__bases" role="group" aria-label="底图类型">
        <button
          v-for="item in BASE_MODES"
          :key="item.key"
          type="button"
          :class="{ 'is-active': baseMode === item.key }"
          @click="setBaseMode(item.key)"
        >
          {{ item.label }}
        </button>
      </div>
      <button class="sim-map__fit" type="button" title="回到全局视野" @click="fitToData(true)">
        全局视野
      </button>
    </div>

    <div class="sim-map__phase">
      <span class="sim-map__phase-dot" :class="`is-${phaseTone}`"></span>
      <strong>{{ phaseLabel }}</strong>
      <small>{{ coordinateLabel }}</small>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

const props = defineProps({
  position: { type: Object, default: () => ({ lng: 119.86, lat: 26.35 }) },
  heading: { type: Number, default: 0 },
  home: { type: Object, default: () => ({ lng: 119.86, lat: 26.35 }) },
  target: { type: Object, default: null },
  route: { type: Array, default: () => [] },
  remaining: { type: Array, default: () => [] },
  phase: { type: String, default: 'ack' },
  state: { type: String, default: 'running' },
})

const MAP_CENTER = { lng: 119.86, lat: 26.35 }
const AMAP_SATELLITE_URL =
  'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}'
const AMAP_STREET_URL =
  'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}'
const ESRI_EARTH_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'

const BASE_MODES = [
  { key: 'satellite', label: '高清' },
  { key: 'earth', label: '影像' },
  { key: 'street', label: '街道' },
]

const GCJ_AXIS = 6378245
const GCJ_EE = 0.00669342162296594323
const PI = Math.PI

const mapEl = ref(null)
const map = shallowRef(null)
const baseMode = ref('satellite')

let tileLayer = null
let routeLayer = null
let remainingLayer = null
let homeMarker = null
let targetMarker = null
let robotMarker = null
let resizeObserver = null
let fitted = false

const PHASE_LABELS = {
  ack: '等待机器人确认',
  navigating: '机器人前往目标点',
  collecting: '机器人正在清理',
  returning: '机器人返航中',
  done: '作业闭环完成',
}

const phaseLabel = computed(() => PHASE_LABELS[props.phase] || '仿真运行中')
const phaseTone = computed(() => {
  if (['done', 'history'].includes(props.state) || props.phase === 'done') return 'done'
  if (props.state === 'paused') return 'paused'
  if (['stopped', 'error'].includes(props.state)) return 'stopped'
  return 'running'
})
const coordinateLabel = computed(() => {
  const lng = Number(props.position?.lng)
  const lat = Number(props.position?.lat)
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return '坐标等待上报'
  return `${lng.toFixed(5)}, ${lat.toFixed(5)}`
})

function validPoint(point) {
  const lng = Number(point?.lng)
  const lat = Number(point?.lat)
  return (
    Number.isFinite(lng) &&
    Number.isFinite(lat) &&
    lng >= -180 &&
    lng <= 180 &&
    lat >= -90 &&
    lat <= 90
  )
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

function displayLatLng(point) {
  if (!validPoint(point)) return null
  const lng = Number(point.lng)
  const lat = Number(point.lat)
  const [shownLng, shownLat] =
    baseMode.value === 'earth' ? [lng, lat] : wgs84ToGcj02(lng, lat)
  return [shownLat, shownLng]
}

function createTileLayer() {
  tileLayer?.remove()
  if (baseMode.value === 'earth') {
    tileLayer = L.tileLayer(ESRI_EARTH_URL, {
      minZoom: 3,
      maxNativeZoom: 19,
      maxZoom: 20,
      detectRetina: true,
      crossOrigin: true,
      attribution:
        'Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community',
    })
  } else {
    tileLayer = L.tileLayer(
      baseMode.value === 'street' ? AMAP_STREET_URL : AMAP_SATELLITE_URL,
      {
        subdomains: ['1', '2', '3', '4'],
        minZoom: 3,
        maxNativeZoom: 18,
        maxZoom: 20,
        detectRetina: true,
        crossOrigin: true,
        attribution:
          'Imagery &copy; <a href="https://www.amap.com/">高德地图</a>',
      },
    )
  }
  tileLayer.addTo(map.value)
}

function createLayers() {
  routeLayer = L.polyline([], {
    pane: 'simulation-lines',
    color: '#18a999',
    weight: 5,
    opacity: 0.96,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(map.value)

  remainingLayer = L.polyline([], {
    pane: 'simulation-lines',
    color: '#ffffff',
    weight: 3,
    opacity: 0.92,
    dashArray: '8 9',
    lineCap: 'round',
  }).addTo(map.value)

  homeMarker = L.circleMarker([0, 0], {
    pane: 'simulation-overlays',
    radius: 7,
    color: '#ffffff',
    weight: 2,
    fillColor: '#5f6f7d',
    fillOpacity: 1,
  })
  homeMarker.bindTooltip('机器人起始点', { direction: 'top', offset: [0, -6] })
  homeMarker.addTo(map.value)

  targetMarker = L.marker([0, 0], {
    pane: 'simulation-overlays',
    icon: L.divIcon({
      className: 'simulation-target-icon',
      html: '<span></span>',
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    }),
  })
  targetMarker.bindTooltip('作业目标点', { direction: 'top', offset: [0, -12] })
  targetMarker.addTo(map.value)

  robotMarker = L.marker([0, 0], {
    pane: 'simulation-overlays',
    icon: robotIcon(),
    zIndexOffset: 600,
  })
  robotMarker.bindTooltip('治理机器人', { direction: 'top', offset: [0, -18] })
  robotMarker.addTo(map.value)
}

function robotIcon() {
  const heading = Number(props.heading) || 0
  return L.divIcon({
    className: 'simulation-robot-icon',
    html: `
      <span class="simulation-robot-icon__pulse"></span>
      <span class="simulation-robot-icon__arrow" style="transform: rotate(${heading}deg)">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 3 19 19 12 15.7 5 19 12 3Z" />
        </svg>
      </span>
    `,
    iconSize: [44, 44],
    iconAnchor: [22, 22],
  })
}

function latLngs(points) {
  return (points || []).map(displayLatLng).filter(Boolean)
}

function updateOverlays() {
  if (!map.value || !routeLayer) return

  const route = latLngs(props.route)
  const remaining = latLngs(props.remaining)
  routeLayer.setLatLngs(route)
  remainingLayer.setLatLngs(remaining)

  const home = displayLatLng(props.home)
  if (home) homeMarker.setLatLng(home)

  const target = displayLatLng(props.target)
  if (target) targetMarker.setLatLng(target)

  const position = displayLatLng(props.position)
  if (position) robotMarker.setLatLng(position)
  robotMarker.setIcon(robotIcon())

  if (!fitted && (route.length || remaining.length || position)) fitToData(false)
}

function fitToData(animate = true) {
  if (!map.value) return
  const points = [
    ...latLngs(props.route),
    ...latLngs(props.remaining),
    displayLatLng(props.home),
    displayLatLng(props.target),
    displayLatLng(props.position),
  ].filter(Boolean)

  if (!points.length) {
    map.value.setView(displayLatLng(MAP_CENTER) || [26.35, 119.86], 12, { animate })
    return
  }
  if (points.length === 1) {
    map.value.setView(points[0], 15, { animate })
    return
  }
  fitted = true
  map.value.fitBounds(L.latLngBounds(points), {
    animate,
    padding: [48, 48],
    maxZoom: 16,
  })
}

function setBaseMode(mode) {
  if (mode === baseMode.value || !map.value) return
  baseMode.value = mode
  createTileLayer()
  updateOverlays()
}

onMounted(() => {
  if (!mapEl.value) return
  map.value = L.map(mapEl.value, {
    center: displayLatLng(MAP_CENTER),
    zoom: 12,
    minZoom: 3,
    maxZoom: 20,
    zoomControl: false,
    preferCanvas: true,
    zoomSnap: 0.5,
  })
  L.control.zoom({ position: 'topleft' }).addTo(map.value)
  map.value.createPane('simulation-lines')
  map.value.getPane('simulation-lines').style.zIndex = 420
  map.value.createPane('simulation-overlays')
  map.value.getPane('simulation-overlays').style.zIndex = 440
  createTileLayer()
  createLayers()
  updateOverlays()

  requestAnimationFrame(() => map.value?.invalidateSize())
  if (window.ResizeObserver) {
    resizeObserver = new ResizeObserver(() => map.value?.invalidateSize())
    resizeObserver.observe(mapEl.value)
  }
})

const overlaySignature = computed(() =>
  [
    props.position?.lng,
    props.position?.lat,
    props.heading,
    props.home?.lng,
    props.home?.lat,
    props.target?.lng,
    props.target?.lat,
    props.route?.length,
    props.route?.at(-1)?.lng,
    props.route?.at(-1)?.lat,
    props.remaining?.length,
    props.remaining?.at(-1)?.lng,
    props.remaining?.at(-1)?.lat,
  ].join('|'),
)

watch(overlaySignature, updateOverlays)
watch(() => props.position, updateOverlays, { deep: true })
watch(() => props.route, updateOverlays, { deep: true })
watch(() => props.remaining, updateOverlays, { deep: true })

onUnmounted(() => {
  resizeObserver?.disconnect()
  if (map.value) {
    map.value.remove()
    map.value = null
  }
  tileLayer = null
  routeLayer = null
  remainingLayer = null
  homeMarker = null
  targetMarker = null
  robotMarker = null
})

defineExpose({ fitToData })
</script>

<style scoped>
.sim-map {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 420px;
  overflow: hidden;
  border-radius: inherit;
  background:
    radial-gradient(circle at 72% 28%, rgba(77, 160, 195, 0.24), transparent 34%),
    linear-gradient(145deg, #dce9ef, #bad3df);
}

.sim-map__canvas {
  width: 100%;
  height: 100%;
}

.sim-map__legend,
.sim-map__tools,
.sim-map__phase {
  position: absolute;
  z-index: 1000;
  border: 1px solid color-mix(in srgb, var(--panel-border) 82%, transparent);
  background: color-mix(in srgb, var(--bg-panel) 90%, transparent);
  box-shadow: 0 10px 30px rgba(13, 39, 51, 0.14);
  backdrop-filter: blur(16px) saturate(1.2);
  -webkit-backdrop-filter: blur(16px) saturate(1.2);
}

.sim-map__legend {
  top: 12px;
  left: 54px;
  display: flex;
  flex-wrap: wrap;
  gap: 9px 13px;
  max-width: calc(100% - 260px);
  padding: 8px 11px;
  border-radius: 10px;
  color: var(--text-sub);
  font-size: 11px;
}

.sim-map__legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}

.sim-map__legend i {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.sim-map__legend .is-robot {
  background: #18a999;
  box-shadow: 0 0 0 3px rgba(24, 169, 153, 0.16);
}

.sim-map__legend .is-target {
  background: #ff9500;
  box-shadow: 0 0 0 3px rgba(255, 149, 0, 0.16);
}

.sim-map__legend .is-track {
  height: 3px;
  border-radius: 2px;
  background: #18a999;
}

.sim-map__legend .is-route {
  height: 3px;
  border-radius: 2px;
  background: repeating-linear-gradient(
    90deg,
    #6d7d88 0,
    #6d7d88 5px,
    transparent 5px,
    transparent 9px
  );
}

.sim-map__tools {
  top: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px;
  border-radius: 11px;
}

.sim-map__bases {
  display: flex;
  gap: 2px;
}

.sim-map__tools button {
  min-height: 32px;
  padding: 5px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-sub);
  font-size: 11.5px;
  font-weight: 550;
  cursor: pointer;
}

.sim-map__tools button:hover,
.sim-map__tools button.is-active {
  color: var(--c-primary);
  background: var(--bg-active);
}

.sim-map__fit {
  border-left: 1px solid var(--separator) !important;
  border-radius: 0 !important;
}

.sim-map__phase {
  bottom: 18px;
  left: 12px;
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 2px 8px;
  min-width: 188px;
  padding: 9px 12px;
  border-radius: 11px;
}

.sim-map__phase-dot {
  grid-row: 1 / span 2;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--c-primary);
  box-shadow: 0 0 0 5px color-mix(in srgb, var(--c-primary) 15%, transparent);
}

.sim-map__phase-dot.is-paused {
  background: #ff9500;
  box-shadow: 0 0 0 5px rgba(255, 149, 0, 0.15);
}

.sim-map__phase-dot.is-done {
  background: #34c759;
  box-shadow: 0 0 0 5px rgba(52, 199, 89, 0.15);
}

.sim-map__phase-dot.is-stopped {
  background: #ff3b30;
  box-shadow: 0 0 0 5px rgba(255, 59, 48, 0.14);
}

.sim-map__phase strong {
  color: var(--text-main);
  font-size: 12.5px;
}

.sim-map__phase small {
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
}

:deep(.leaflet-container) {
  background: transparent;
  color: var(--text-main);
  font-family: inherit;
}

:deep(.leaflet-tile-pane) {
  filter: saturate(1.04) contrast(1.04);
}

:deep(.leaflet-control-zoom) {
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--panel-border) 82%, transparent) !important;
  border-radius: 11px !important;
  box-shadow: 0 8px 24px rgba(13, 39, 51, 0.14) !important;
}

:deep(.leaflet-control-zoom a) {
  width: 38px !important;
  height: 38px !important;
  border: 0 !important;
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent) !important;
  color: var(--text-main) !important;
  font: 20px/38px -apple-system, BlinkMacSystemFont, sans-serif !important;
}

:deep(.leaflet-control-zoom a + a) {
  border-top: 1px solid var(--separator) !important;
}

:deep(.leaflet-control-attribution) {
  padding: 3px 6px;
  background: rgba(255, 255, 255, 0.72);
  color: #63717a;
  font-size: 10px;
}

:deep(.simulation-robot-icon),
:deep(.simulation-target-icon) {
  border: 0;
  background: transparent;
}

:deep(.simulation-robot-icon__pulse) {
  position: absolute;
  inset: 3px;
  border: 2px solid rgba(24, 169, 153, 0.46);
  border-radius: 50%;
  animation: sim-pulse 1.8s ease-out infinite;
}

:deep(.simulation-robot-icon__arrow) {
  position: absolute;
  inset: 7px;
  display: block;
  filter: drop-shadow(0 4px 7px rgba(4, 32, 40, 0.36));
}

:deep(.simulation-robot-icon__arrow svg) {
  display: block;
  width: 100%;
  height: 100%;
}

:deep(.simulation-robot-icon__arrow path) {
  fill: #18a999;
  stroke: #ffffff;
  stroke-width: 1.4;
  stroke-linejoin: round;
}

:deep(.simulation-target-icon span) {
  display: block;
  width: 14px;
  height: 14px;
  margin: 7px;
  border: 3px solid #ffffff;
  border-radius: 50%;
  background: #ff9500;
  box-shadow:
    0 0 0 4px rgba(255, 149, 0, 0.18),
    0 4px 9px rgba(4, 32, 40, 0.28);
}

@keyframes sim-pulse {
  0% {
    opacity: 0.8;
    transform: scale(0.7);
  }
  100% {
    opacity: 0;
    transform: scale(1.35);
  }
}

@media (max-width: 760px) {
  .sim-map {
    min-height: 340px;
  }

  .sim-map__legend {
    top: 56px;
    left: 10px;
    max-width: calc(100% - 20px);
  }

  .sim-map__tools {
    top: 8px;
    right: 8px;
  }

  .sim-map__phase {
    bottom: 22px;
  }
}
</style>

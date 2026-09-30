<template>
  <div class="sim-scene" :class="{ 'is-dark': isDark }">
    <canvas ref="canvasEl" class="sim-scene__canvas"></canvas>

    <div v-if="failed" class="sim-scene__fallback">
      <strong>3D 场景不可用</strong>
      <span>{{ failed }}</span>
    </div>

    <div class="sim-scene__hud">
      <div class="sim-scene__status">
        <span class="sim-scene__dot" :class="[`is-${tone}`, { 'is-replay': replayActive }]"></span>
        <div>
          <strong>{{ replayActive ? '历史轨迹回放' : phaseLabel }}</strong>
          <span>{{ replayActive ? `${replayPhaseLabel} · ${replayMetaText}` : coordinateText }}</span>
        </div>
      </div>

      <div class="sim-scene__joints" aria-label="机械臂关节角">
        <div v-for="joint in joints" :key="joint.label" class="sim-scene__joint">
          <span>{{ joint.label }}</span>
          <code>{{ joint.value }}°</code>
        </div>
      </div>

      <div class="sim-scene__actions">
        <button
          type="button"
          class="sim-scene__action"
          :class="{ 'is-active': following }"
          @click="toggleFollow"
        >
          <span aria-hidden="true">◎</span>
          跟随
        </button>
        <button type="button" class="sim-scene__action" @click="resetView">
          <span aria-hidden="true">⌖</span>
          复位视角
        </button>
      </div>

      <div v-if="replayActive" class="sim-scene__replay">
        <div class="sim-scene__replay-bar">
          <button type="button" class="sim-scene__replay-btn" @click="toggleReplay">
            <span aria-hidden="true">{{ replayPlaying ? 'Ⅱ' : '▶' }}</span>
            {{ replayPlaying ? '暂停' : '播放' }}
          </button>
          <button type="button" class="sim-scene__replay-btn" @click="restartReplay">
            <span aria-hidden="true">↺</span>
            重播
          </button>
          <div class="sim-scene__replay-track">
            <i :style="{ width: `${replayProgress * 100}%` }"></i>
          </div>
          <code>{{ Math.round(replayProgress * 100) }}%</code>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { getTheme } from '@/utils/theme'
import { createRobotArmScene } from '@/utils/robotArmScene'

const props = defineProps({
  position: { type: Object, default: () => ({ lng: 119.86, lat: 26.35 }) },
  heading: { type: Number, default: 0 },
  home: { type: Object, default: () => ({ lng: 119.86, lat: 26.35 }) },
  target: { type: Object, default: null },
  route: { type: Array, default: () => [] },
  remaining: { type: Array, default: () => [] },
  phase: { type: String, default: 'ack' },
  state: { type: String, default: 'running' },
  speed: { type: Number, default: 1 },
  progress: { type: Number, default: 0 },
  battery: { type: Number, default: 100 },
  bins: { type: Object, default: () => ({}) },
})

const canvasEl = ref(null)
const scene = shallowRef(null)
const failed = ref('')
const jointAngles = ref(Array(6).fill(0))
const following = ref(true)
const isDark = ref(false)

let resizeObserver = null
let themeObserver = null

const PHASE_LABELS = {
  ack: '等待机器人确认',
  navigating: '机器人前往目标点',
  collecting: '清理作业中',
  returning: '机器人返航中',
  done: '作业闭环完成',
}

const phaseLabel = computed(() => PHASE_LABELS[props.phase] || '仿真运行中')
const replayActive = ref(false)
const replayPlaying = ref(false)
const replayProgress = ref(0)
const replayPhase = ref('navigating')
const tone = computed(() => {
  if (['done', 'history'].includes(props.state) || props.phase === 'done') return 'done'
  if (props.state === 'paused') return 'paused'
  if (['stopped', 'error'].includes(props.state)) return 'stopped'
  return 'running'
})
const coordinateText = computed(() => {
  const lng = Number(props.position?.lng)
  const lat = Number(props.position?.lat)
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return '坐标等待上报'
  return `${lng.toFixed(5)}, ${lat.toFixed(5)}`
})
const replayMetaText = computed(() => {
  return `${props.route.length} 个轨迹点`
})
const replayPhaseLabel = computed(() => PHASE_LABELS[replayPhase.value] || '航行中')
const joints = computed(() =>
  jointAngles.value.map((value, index) => ({
    label: ['J1', 'J2', 'J3', 'J4', 'J5', 'J6'][index] || `J${index + 1}`,
    value: Number.isFinite(value) ? value.toFixed(0) : '—',
  })),
)

function telemetryPayload() {
  return {
    home: props.home,
    target: props.target,
    position: props.position,
    heading: props.heading,
    phase: props.phase,
    state: props.state,
    speed: props.speed,
    progress: props.progress,
    battery: props.battery,
    bins: props.bins,
  }
}

function resetView() {
  scene.value?.resetView()
}

function toggleFollow() {
  following.value = !following.value
  scene.value?.setFollow(following.value)
}

function toggleReplay() {
  if (replayPlaying.value) scene.value?.pauseReplay()
  else scene.value?.resumeReplay()
}

function restartReplay() {
  scene.value?.restartReplay()
}

function syncReplayState() {
  const sceneState = scene.value?.replay
  replayActive.value = Boolean(sceneState?.active)
  replayPlaying.value = Boolean(sceneState?.playing)
  replayProgress.value = Number(sceneState?.progress || 0)
  replayPhase.value = sceneState?.phase || props.phase
}

let replayPollTimer = null

function setupReplayPolling() {
  clearInterval(replayPollTimer)
  if (!replayActive.value) return
  replayPollTimer = setInterval(syncReplayState, 250)
}

function syncTheme() {
  isDark.value = getTheme() === 'dark'
  scene.value?.setTheme(isDark.value)
}

onMounted(() => {
  if (!canvasEl.value) return
  scene.value = createRobotArmScene({
    canvas: canvasEl.value,
    onJoints: (angles) => {
      jointAngles.value = angles
    },
    onFail: (error) => {
      failed.value = error?.message || '浏览器不支持 WebGL'
    },
  })
  if (!scene.value) return

  scene.value.applyTelemetry(telemetryPayload())
  syncTheme()
  maybeStartReplay()

  resizeObserver = new ResizeObserver(() => scene.value?.resize())
  resizeObserver.observe(canvasEl.value)
  themeObserver = new MutationObserver(syncTheme)
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  })
})

function maybeStartReplay() {
  const terminal = ['done', 'history', 'stopped', 'error'].includes(props.state)
  if (!terminal || props.route.length < 2) return
  scene.value?.startReplay({
    route: props.route,
    speed: props.speed,
    home: props.home,
    target: props.target,
  })
  syncReplayState()
  setupReplayPolling()
}

const telemetrySignature = computed(() =>
  [
    props.position?.lng,
    props.position?.lat,
    props.heading,
    props.home?.lng,
    props.home?.lat,
    props.target?.lng,
    props.target?.lat,
    props.phase,
    props.state,
    props.speed,
    props.progress,
    props.battery,
    props.bins?.foam,
    props.bins?.plastic,
    props.bins?.mixed,
  ].join('|'),
)

watch(telemetrySignature, () => {
  const terminal = ['done', 'history', 'stopped', 'error'].includes(props.state)
  if (terminal && props.route.length > 1) {
    if (!replayActive.value) maybeStartReplay()
    scene.value?.setReplaySpeed(props.speed)
  } else {
    if (replayActive.value) {
      scene.value?.stopReplay()
      replayActive.value = false
      replayPlaying.value = false
      replayProgress.value = 0
      clearInterval(replayPollTimer)
    }
    scene.value?.applyTelemetry(telemetryPayload())
  }
})

watch(
  () => props.position,
  () => {
    if (replayActive.value) return
    scene.value?.applyTelemetry(telemetryPayload())
  },
  { deep: true },
)

onUnmounted(() => {
  resizeObserver?.disconnect()
  themeObserver?.disconnect()
  clearInterval(replayPollTimer)
  scene.value?.dispose()
  scene.value = null
})

defineExpose({
  resetView,
  toggleFollow,
  toggleReplay,
  restartReplay,
  get replayState() {
    return {
      active: Boolean(scene.value?.replay?.active),
      playing: Boolean(scene.value?.replay?.playing),
      progress: Number(scene.value?.replay?.progress || 0),
      phase: scene.value?.replay?.phase || props.phase,
      battery: Number(scene.value?.replay?.battery ?? props.battery),
      bins: scene.value?.replay?.bins || props.bins,
    }
  },
})
</script>

<style scoped>
.sim-scene {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 420px;
  overflow: hidden;
  border-radius: inherit;
  background:
    radial-gradient(circle at 72% 28%, rgba(77, 160, 195, 0.2), transparent 36%),
    linear-gradient(160deg, #dbeaf1, #b7d4e0);
}

.sim-scene.is-dark {
  background:
    radial-gradient(circle at 72% 28%, rgba(31, 116, 139, 0.24), transparent 38%),
    linear-gradient(160deg, #0b2631, #061720);
}

.sim-scene__canvas {
  display: block;
  width: 100%;
  height: 100%;
}

.sim-scene__fallback {
  position: absolute;
  inset: 0;
  z-index: 6;
  display: grid;
  place-content: center;
  gap: 6px;
  padding: 24px;
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
  color: var(--text-sub);
  text-align: center;
}

.sim-scene__fallback strong {
  color: var(--text-main);
  font-size: 14px;
}

.sim-scene__fallback span {
  max-width: 420px;
  font-size: 11.5px;
  overflow-wrap: anywhere;
}

.sim-scene__hud {
  position: absolute;
  inset: 0;
  z-index: 4;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 12px;
  pointer-events: none;
}

.sim-scene__status,
.sim-scene__joints,
.sim-scene__actions {
  border: 1px solid color-mix(in srgb, var(--panel-border) 82%, transparent);
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
  box-shadow: 0 10px 30px rgba(13, 39, 51, 0.16);
  backdrop-filter: blur(16px) saturate(1.2);
  -webkit-backdrop-filter: blur(16px) saturate(1.2);
}

.sim-scene__status {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 3px 9px;
  min-width: 188px;
  padding: 9px 12px;
  border-radius: 11px;
  pointer-events: auto;
}

.sim-scene__dot {
  grid-row: 1 / span 2;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--c-primary);
  box-shadow: 0 0 0 5px color-mix(in srgb, var(--c-primary) 15%, transparent);
}

.sim-scene__dot.is-paused {
  background: #ff9500;
  box-shadow: 0 0 0 5px rgba(255, 149, 0, 0.15);
}

.sim-scene__dot.is-done {
  background: #34c759;
  box-shadow: 0 0 0 5px rgba(52, 199, 89, 0.15);
}

.sim-scene__dot.is-stopped {
  background: #ff3b30;
  box-shadow: 0 0 0 5px rgba(255, 59, 48, 0.14);
}

.sim-scene__dot.is-replay {
  background: #00c7ff;
  box-shadow: 0 0 0 5px rgba(0, 199, 255, 0.16);
}

.sim-scene__status strong {
  color: var(--text-main);
  font-size: 12.5px;
}

.sim-scene__status span {
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
}

.sim-scene__joints {
  display: grid;
  grid-template-columns: repeat(6, auto);
  gap: 2px;
  padding: 5px;
  border-radius: 10px;
}

.sim-scene__joint {
  display: grid;
  gap: 1px;
  min-width: 46px;
  padding: 5px 7px;
  border-radius: 7px;
  background: var(--bg-panel-2);
  text-align: center;
}

.sim-scene__joint span {
  color: var(--text-dim);
  font-size: 9px;
}

.sim-scene__joint code {
  color: var(--text-main);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
}

.sim-scene__actions {
  display: flex;
  gap: 4px;
  padding: 4px;
  border-radius: 10px;
  pointer-events: auto;
}

.sim-scene__replay {
  position: absolute;
  left: 50%;
  bottom: 14px;
  z-index: 5;
  transform: translateX(-50%);
  width: min(520px, calc(100% - 24px));
  border: 1px solid color-mix(in srgb, var(--panel-border) 82%, transparent);
  background: color-mix(in srgb, var(--bg-panel) 90%, transparent);
  box-shadow: 0 12px 34px rgba(13, 39, 51, 0.22);
  backdrop-filter: blur(16px) saturate(1.2);
  -webkit-backdrop-filter: blur(16px) saturate(1.2);
  border-radius: 11px;
  pointer-events: auto;
}

.sim-scene__replay-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
}

.sim-scene__replay-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  padding: 4px 9px;
  border: 0;
  border-radius: 7px;
  background: var(--bg-panel-2);
  color: var(--text-main);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}

.sim-scene__replay-btn:hover {
  background: var(--bg-hover);
  color: var(--c-primary);
}

.sim-scene__replay-btn span {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 11px;
}

.sim-scene__replay-track {
  flex: 1;
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--bg-panel-2);
}

.sim-scene__replay-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--c-primary), #00c7ff);
}

.sim-scene__replay-bar code {
  min-width: 36px;
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 10.5px;
  text-align: right;
}

.sim-scene__action {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 32px;
  padding: 5px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-sub);
  font-size: 11.5px;
  cursor: pointer;
  white-space: nowrap;
}

.sim-scene__action:hover,
.sim-scene__action.is-active {
  background: var(--bg-active);
  color: var(--c-primary);
}

.sim-scene__action span {
  font-size: 13px;
  line-height: 1;
}

@media (max-width: 900px) {
  .sim-scene {
    min-height: 0;
  }

  .sim-scene__hud {
    flex-wrap: wrap;
  }

  .sim-scene__joints {
    order: 3;
    width: 100%;
    grid-template-columns: repeat(6, 1fr);
  }
}

@media (max-width: 560px) {
  .sim-scene__hud {
    padding: 9px;
  }

  .sim-scene__status {
    min-width: 0;
    flex: 1;
  }

  .sim-scene__joint {
    min-width: 0;
    padding: 4px 2px;
  }

  .sim-scene__action {
    padding: 5px 7px;
  }
}
</style>

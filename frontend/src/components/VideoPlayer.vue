<template>
  <div class="player" ref="rootEl" :class="{ 'player--offline': !playing }">
    <!-- 视频层 -->
    <video
      ref="videoEl"
      class="player__video"
      muted
      autoplay
      playsinline
      @playing="playing = true"
      @pause="playing = false"
      @waiting="buffering = true"
      @canplay="buffering = false"
      @loadedmetadata="onLoadedMetadata"
    ></video>

    <!-- 未播放时的占位 -->
    <div v-if="!playing" class="player__mask">
      <div class="player__mask-icon">◉</div>
      <div class="player__mask-text">{{ statusText }}</div>
      <button v-if="streamUrl" class="player__retry" @click="play">重新连接</button>
    </div>

    <!-- 顶部信息条 -->
    <div class="player__top">
      <span class="player__name">{{ name }}</span>
      <span class="player__live" v-if="playing">
        <i class="dot-live"></i>LIVE
      </span>
    </div>

    <!-- 底部状态 -->
    <div class="player__bottom">
      <span class="player__id">{{ deviceId }}</span>
      <span v-if="buffering && playing" class="player__buffering">缓冲中…</span>
      <span v-if="detectionCount > 0" class="player__det">
        <i class="dot-det"></i>{{ detectionCount }} 个目标
      </span>
    </div>

    <!-- 检测框叠加（模拟演示用；真实部署由边缘盒推坐标，或前端不做叠加） -->
    <div v-if="playing && boxes.length" class="player__boxes">
      <div
        v-for="(box, i) in boxes"
        :key="i"
        class="player__box"
        :style="boxStyle(box)"
      >
        <span class="player__box-label">{{ boxLabel(box) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 视频播放器。
 *
 * ★ 用 mpegts.js 而不是 flv.js
 *   flv.js 已停止维护（原作者归档），在新版浏览器上有兼容问题。
 *   mpegts.js 是它的活跃继任者，API 几乎一致，直接换。
 *
 * 播放路径：摄像头 RTSP → go2rtc 转 HTTP-FLV → 本组件播放。
 * 选 HTTP-FLV 而不是 HLS：HLS 延迟 6~30 秒，大屏看告警会有明显滞后；
 * FLV 延迟可压到 1~2 秒。
 */
import { ref, reactive, watch, onMounted, onUnmounted, computed } from 'vue'
import mpegts from 'mpegts.js'
import { classColor, classLabel } from '@/utils/constants'

// Stream failures are expected when a camera or go2rtc is offline. Keep them
// in the player state, but do not let the library turn an expected retry path
// into console.error noise that looks like an application crash.
mpegts.LoggingControl.applyConfig({
  ...mpegts.LoggingControl.getConfig(),
  enableError: false,
})

const props = defineProps({
  deviceId: { type: String, required: true },
  name: { type: String, default: '' },
  /** HTTP-FLV 地址，来自 /devices/{id}/stream */
  streamUrl: { type: String, default: '' },
  /** 该点位的近期检测结果（用于画框） */
  detections: { type: Array, default: () => [] },
})

const videoEl = ref(null)
const rootEl = ref(null)
const playing = ref(false)
const buffering = ref(false)
const errorMsg = ref('')
// 检测框归一化需要的两个尺寸：视频原始分辨率（loadedmetadata 拿到）
// 与容器实际大小（ResizeObserver 跟踪）；不再假设流固定 1920×1080
const videoSize = reactive({ w: 0, h: 0 })
const boxArea = reactive({ w: 0, h: 0 })

let player = null
let resizeObserver = null

const boxes = computed(() => props.detections || [])
const detectionCount = computed(() => boxes.value.length)

const statusText = computed(() => {
  if (errorMsg.value) return errorMsg.value
  if (!props.streamUrl) return '未配置视频流'
  return '等待视频流…'
})

function onLoadedMetadata() {
  const v = videoEl.value
  if (!v) return
  videoSize.w = v.videoWidth || 0
  videoSize.h = v.videoHeight || 0
}

function boxStyle(box) {
  // bbox 是视频原始像素坐标；object-fit: contain 下要先算出视频在容器里的
  // 实际绘制区（含 letterbox 黑边偏移），再把框等比映射过去
  const [x1, y1, x2, y2] = box.bbox || [0, 0, 0, 0]
  const color = classColor(box.class)
  const { w: vw, h: vh } = videoSize
  const { w: cw, h: ch } = boxArea
  if (!vw || !vh || !cw || !ch) return { display: 'none', borderColor: color }
  const scale = Math.min(cw / vw, ch / vh)
  const offX = (cw - vw * scale) / 2
  const offY = (ch - vh * scale) / 2
  return {
    left: `${offX + x1 * scale}px`,
    top: `${offY + y1 * scale}px`,
    width: `${Math.max(0, (x2 - x1) * scale)}px`,
    height: `${Math.max(0, (y2 - y1) * scale)}px`,
    borderColor: color,
  }
}

function boxLabel(box) {
  const conf = box.confidence != null ? ` ${(box.confidence * 100).toFixed(0)}%` : ''
  return `${classLabel(box.class)}${conf}`
}

function destroy() {
  if (player) {
    try {
      player.pause()
      player.unload()
      player.detachMediaElement()
      player.destroy()
    } catch {
      /* 忽略清理异常 */
    }
    player = null
  }
  playing.value = false
  buffering.value = false
}

function play() {
  destroy()
  errorMsg.value = ''

  if (!props.streamUrl || !videoEl.value) return

  // 浏览器不支持 MSE 时明确提示，而不是静默黑屏
  if (!mpegts.isSupported()) {
    errorMsg.value = '浏览器不支持 MSE 播放'
    return
  }

  try {
    player = mpegts.createPlayer(
      {
        type: 'flv',
        isLive: true, // ★ 直播流必须置 true，否则不会追帧
        url: props.streamUrl,
      },
      {
        enableWorker: false, // 浏览器兼容性优先，边缘演示机性能参差
        enableStashBuffer: false, // 关掉缓冲堆积，降低延迟
        liveBufferLatencyChasing: true, // 追帧：把延迟拉回低水位
        liveBufferLatencyMaxLatency: 3.0,
        liveBufferLatencyMinRemain: 0.5,
        lazyLoad: false,
        stashInitialSize: 128,
      },
    )

    // mpegts.js emits network/demux failures as an "error" event. EventEmitter
    // treats an unhandled "error" event as fatal, so subscribe before load().
    player.on(mpegts.Events.ERROR, (errorType, errorDetail) => {
      const detail = [errorType, errorDetail].filter(Boolean).join(' / ')
      errorMsg.value = detail ? `视频流连接失败：${detail}` : '视频流连接失败'
      playing.value = false
      buffering.value = false
    })

    player.attachMediaElement(videoEl.value)
    player.load()

    const p = player.play()
    if (p && typeof p.catch === 'function') {
      p.catch((err) => {
        // 自动播放被拦截是常见情况，给出可点按的提示
        errorMsg.value = '点击画面开始播放'
        playing.value = false
      })
    }
  } catch (err) {
    errorMsg.value = `播放失败：${err.message || err}`
    destroy()
  }
}

// 流地址或设备任一变化都要重连；合并成一个 watcher 避免两处各挂一份
watch([() => props.streamUrl, () => props.deviceId], play)

onMounted(() => {
  play()
  // 容器尺寸决定检测框落位：宫格切换、侧栏开合、窗口缩放都会改它
  if (rootEl.value && typeof ResizeObserver !== 'undefined') {
    boxArea.w = rootEl.value.clientWidth || 0
    boxArea.h = rootEl.value.clientHeight || 0
    resizeObserver = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (!rect) return
      boxArea.w = rect.width
      boxArea.h = rect.height
    })
    resizeObserver.observe(rootEl.value)
  }
})

onUnmounted(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  destroy()
})

defineExpose({ play, destroy })
</script>

<style scoped>
.player {
  position: relative;
  width: 100%;
  height: 100%;
  background: #05090e;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}

.player__video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.player__mask {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: rgba(5, 9, 14, 0.86);
  padding: 16px;
  text-align: center;
}

.player__mask-icon {
  font-size: 26px;
  color: var(--text-dim);
}

.player__mask-text {
  font-size: 12px;
  color: var(--text-sub);
  max-width: 100%;
  overflow-wrap: anywhere;
}

.player__retry {
  margin-top: 4px;
  min-height: 40px;
  padding: 8px 14px;
  font-size: 13px;
  color: var(--c-primary);
  background: rgba(0, 122, 255, 0.1);
  border: 0;
  border-radius: 10px;
  cursor: pointer;
}

.player__retry:hover {
  background: rgba(0, 122, 255, 0.18);
}

.player__top,
.player__bottom {
  position: absolute;
  left: 0;
  right: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 9px;
  font-size: 12px;
  pointer-events: none;
}

.player__top {
  top: 0;
  background: linear-gradient(180deg, rgba(5, 9, 14, 0.85), transparent);
}

.player__bottom {
  bottom: 0;
  background: linear-gradient(0deg, rgba(5, 9, 14, 0.85), transparent);
}

.player__name {
  min-width: 0;
  overflow: hidden;
  color: var(--text-main);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.player__id {
  min-width: 0;
  overflow: hidden;
  color: var(--text-dim);
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.player__live {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #f2564c;
  font-weight: 600;
  font-size: 11px;
}

.dot-live {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #f2564c;
  animation: blink 1.4s infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}

.player__buffering {
  color: var(--c-warn);
}

.player__det {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--c-primary);
}

.dot-det {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--c-primary);
}

.player__boxes {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.player__box {
  position: absolute;
  border: 1.5px solid;
  box-shadow: 0 0 6px rgba(0, 0, 0, 0.5);
}

.player__box-label {
  position: absolute;
  top: -16px;
  left: -1px;
  font-size: 10.5px;
  padding: 0 4px;
  background: inherit;
  color: #fff;
  white-space: nowrap;
  background: rgba(5, 9, 14, 0.8);
}

@media (max-width: 480px) {
  .player__top,
  .player__bottom {
    padding-inline: 7px;
  }

  .player__name {
    max-width: 54%;
  }

  .player__id {
    max-width: 58%;
  }
}
</style>

<script setup>
/**
 * 检测框画布 —— 在图片上叠加 OpenCV 检测框。
 *
 * 从 AnalyzeView 搬来时一并修掉的 3 处问题：
 * 1. 标签/配色统一走 @/utils/constants 的 classLabel/classColor，
 *    不再另起一套类名表（避免列表与画布两种叫法、两种颜色）；
 * 2. img.onload 回调里复判 canvas 仍挂载（组件卸载后 onload 才触发会空指针）；
 * 3. 只画一次：图加载完成且检测数据齐全后一次性绘制，不做
 *   「先画原图、分析返回后再画框」的双 draw（竞态下先到的会把后到的覆盖）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { classColor, classLabel } from '@/utils/constants'

const props = defineProps({
  /** 图片地址（dataURL 或可访问的 URL） */
  src: { type: String, required: true },
  /** OpenCV 检测框：[{ class, confidence, bbox: [x1, y1, x2, y2] }] */
  detections: { type: Array, default: () => [] },
})

const canvas = ref(null)
let img = null

function draw() {
  const c = canvas.value
  // 坑 2：onload 是异步回调，组件可能已卸载，canvas 为 null
  if (!c || !img || !img.complete || !img.naturalWidth) return
  const w = img.naturalWidth
  const h = img.naturalHeight
  c.width = w
  c.height = h
  const ctx = c.getContext('2d')
  ctx.drawImage(img, 0, 0, w, h)

  const scale = Math.max(1, Math.min(3, w / 1200))
  for (const det of props.detections) {
    const bbox = det?.bbox
    if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some((v) => typeof v !== 'number')) {
      continue
    }
    const [x1, y1, x2, y2] = bbox
    // 坑 1：与列表同一套 classColor/classLabel，颜色不再各画各的
    const color = classColor(det.class)
    ctx.strokeStyle = color
    ctx.lineWidth = Math.max(2, Math.round(2 * scale))
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1)

    const label = `${classLabel(det.class)} ${(Number(det.confidence || 0) * 100).toFixed(0)}%`
    const fontSize = Math.round(13 * scale)
    ctx.font = `${fontSize}px "PingFang SC", "Microsoft YaHei", sans-serif`
    const textW = ctx.measureText(label).width
    const tagH = Math.round(18 * scale)
    const pad = Math.round(4 * scale)
    // 标签底贴框上缘；框已贴图顶时画到框内，避免被裁掉
    const tagY = y1 - tagH >= 0 ? y1 - tagH : y1
    ctx.fillStyle = color
    ctx.fillRect(x1, tagY, textW + pad * 2, tagH)
    ctx.fillStyle = '#ffffff'
    ctx.fillText(label, x1 + pad, tagY + tagH - Math.round(5 * scale))
  }
}

onMounted(() => {
  // 坑 3：唯一绘制入口 —— 图片 onload 触发 draw，检测数据已在 props 里
  img = new Image()
  img.onload = draw
  img.src = props.src
})

// src / detections 变化时重画（消息气泡内通常不变，防御性处理）
watch(
  () => props.src,
  (src) => {
    if (img && src && img.src !== src) img.src = src
    else draw()
  },
)
watch(
  () => props.detections,
  () => draw(),
  { deep: true },
)

onBeforeUnmount(() => {
  img = null
})
</script>

<template>
  <canvas ref="canvas" class="det-canvas"></canvas>
</template>

<style scoped>
.det-canvas {
  display: block;
  max-width: 100%;
  height: auto;
  border-radius: var(--radius);
  border: 1px solid var(--panel-border);
  background: var(--bg-panel-2);
}
</style>

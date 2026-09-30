<template>
  <div class="vcompare">
    <div class="vcompare__head">
      <h2 class="vcompare__title">双通道对照</h2>
      <p class="vcompare__sub">
        传统视觉（OpenCV）与开放词汇（YOLO-World）各自在什么条件下有效、在同一张图上各看到什么。
        <b>这是定性对照，不是"谁更准"的对决。</b>
      </p>
      <div class="vcompare__samples">
        <span>实测样例</span>
        <div class="vcompare__sample-buttons">
          <button
            v-for="sample in VISION_SAMPLES"
            :key="sample.id"
            type="button"
            :class="{ 'vcompare__sample--active': activeSampleId === sample.id }"
            @click="loadSample(sample)"
          >
            {{ sample.category }}
          </button>
        </div>
      </div>
      <p v-if="sampleError" class="vcompare__error">{{ sampleError }}</p>
    </div>

    <div class="panel vcompare__body">
      <div class="vcompare__body-inner">
        <div v-if="loadError" class="empty text-danger">{{ loadError }}</div>
        <div v-else-if="!activeSampleId" class="empty">请先在上方选择一个实测样例</div>
        <div v-else-if="!compareEntry" class="empty">该样例暂无对照数据</div>
        <template v-else>
          <p class="vcompare__lead">
            两条通道回答的是<b>不同的问题</b>：左列靠<b>帧间差异</b>（固定机位 + 连续帧），
            右列靠<b>语义</b>（单帧零样本）。两者的置信度<b>尺度不同，不可横向比较</b>。
          </p>

          <div class="vcompare__columns">
            <section v-for="col in compareColumns" :key="col.key" class="vcompare__column">
              <header class="vcompare__column-head">
                <strong>{{ col.label }}</strong>
                <span class="vcompare__column-engine">{{ col.engine }}</span>
              </header>
              <div class="vcompare__column-stage">
                <canvas :ref="(el) => setCompareCanvas(col.key, el)" class="vcompare__column-canvas"></canvas>
              </div>
              <dl class="vcompare__column-stats">
                <div><dt>本图检出</dt><dd>{{ col.count }} 条</dd></div>
                <div><dt>门限</dt><dd>{{ col.thresholdLabel }}</dd></div>
                <div><dt>单帧耗时</dt><dd>{{ col.latency }}</dd></div>
              </dl>
              <p v-if="col.count === 0" class="vcompare__column-note">
                0 条是<b>适用范围</b>问题，不是失效：该通道需要连续帧建立背景模型，
                单张照片没有背景可用。它在连续视频帧上正常工作（见下方"连续帧"一栏）。
              </p>
            </section>
          </div>

          <div class="vcompare__regime">
            <header class="vcompare__regime-head">
              <strong>连续帧（合成海面，{{ videoRegime?.frames }} 帧）</strong>
              <span class="vcompare__column-engine">程序化真值 · 口径 E1/E2 · 不得外推为真实海域</span>
            </header>
            <dl class="vcompare__regime-grid">
              <div>
                <dt>传统视觉</dt>
                <dd>
                  {{ videoRegime?.cv?.detections_total }} 条 /
                  {{ videoRegime?.cv?.frames_with_detection }} 帧有检出
                  <span v-if="videoRegime?.cv?.gt_hit_rate != null" class="text-dim">
                    · 合成真值命中 {{ videoRegime?.cv?.gt_targets_hit }}/{{ videoRegime?.cv?.gt_targets_total }}
                    （{{ (videoRegime.cv.gt_hit_rate * 100).toFixed(1) }}%）
                  </span>
                </dd>
              </div>
              <div>
                <dt>开放词汇</dt>
                <dd>
                  {{ videoRegime?.world?.detections_total }} 条 /
                  {{ videoRegime?.world?.frames_with_detection }} 帧有检出
                </dd>
              </div>
            </dl>
            <p class="vcompare__regime-note">
              合成帧是程序化绘制的纯色圆盘，缺少开放词汇模型赖以判别的外观纹理 ——
              这里的 0 条说明<b>这批合成真值不适合考察该通道</b>，
              不说明该通道无效；它的表现请看上方单张照片一栏。
            </p>
          </div>

          <ul class="vcompare__caveats">
            <li v-for="c in compareCaveats" :key="c">{{ c }}</li>
          </ul>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 双通道对照页（定性）。
 *
 * ★ 为什么是独立一页、而不是塞进「智能助手」对话：
 *   助手页里检测结果是以"一条消息"的形态出现的，没有地方放
 *   "同一张图两条通道并排 + 各自适用条件 + 不可横向比较"这组说明。
 *   而这组说明恰恰是本页的全部价值 —— 只摆一列，读的人会把
 *   "不适用"读成"更差"（两列都各有 0 的一栏，见下方两个 regime）。
 *
 * ★ 这里刻意不发明任何数字：检出、门限、时延全部来自
 *   scripts/export_channel_compare.py 的实测产物
 *   （demo/vision-channel-compare.json）。改了检测器请重新导出。
 *
 * ★ 不接告警主链路：本页是纯展示，不发任何写请求，也不影响
 *   detector.backend 的现网默认值（cv）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  VISION_SAMPLES,
  visionAssetUrl,
} from '@/utils/visionSamples'
import { classLabel, classColor } from '@/utils/constants'

const route = useRoute()

const activeSampleId = ref('')
const preview = ref('')
const sampleError = ref('')
const loadError = ref('')
const compareData = ref(null)
const compareCanvases = {}

// 与助手页同一套配色/译名（cv 的输出是契约四类；world 的输出是提示词原文）
const VISION_CLASSES = {
  'plastic bottle': { label: '塑料瓶', color: '#007aff' },
  shoe: { label: '鞋类', color: '#5856d6' },
  'fishing net': { label: '渔网', color: '#ff3b30' },
  'fishing gear': { label: '渔具', color: '#ff3b30' },
  'polystyrene cup': { label: '聚苯乙烯杯', color: '#ff9500' },
  'foam cup': { label: '泡沫杯', color: '#ff9500' },
}

const compareEntry = computed(() => {
  const images = compareData.value?.regimes?.still_image?.images
  if (!Array.isArray(images) || !activeSampleId.value) return null
  return images.find((item) => item.sample_id === activeSampleId.value) || null
})

const videoRegime = computed(() => compareData.value?.regimes?.video_stream || null)

const compareCaveats = computed(() => compareData.value?.caveats || [])

const compareColumns = computed(() => {
  const entry = compareEntry.value
  if (!entry) return []
  const channels = compareData.value?.channels || {}
  return ['cv', 'world'].map((key) => {
    const ch = channels[key] || {}
    const block = entry[key] || {}
    return {
      key,
      label: ch.label || key,
      engine: ch.engine || '',
      detections: Array.isArray(block.detections) ? block.detections : [],
      count: block.count ?? 0,
      thresholdLabel:
        key === 'cv'
          ? `时序 ${ch.min_confidence ?? '—'}`
          : `模型 ${block.conf ?? ch.min_confidence ?? '—'}`,
      latency:
        block.latency_ms != null ? `${Number(block.latency_ms).toFixed(1)} ms` : '—',
    }
  })
})

function displayClassLabel(value) {
  return VISION_CLASSES[value]?.label || classLabel(value)
}

function displayClassColor(value) {
  return VISION_CLASSES[value]?.color || classColor(value)
}

/** 对照页只需要图：检出来自实测产物，不再去请求检测服务 */
function loadSample(sample) {
  sampleError.value = ''
  activeSampleId.value = sample.id
  preview.value = visionAssetUrl(sample.image)
}

async function ensureCompareData() {
  try {
    const res = await fetch(visionAssetUrl('demo/vision-channel-compare.json'), {
      cache: 'no-store',
    })
    if (!res.ok) throw new Error(`对照数据加载失败（HTTP ${res.status}）`)
    compareData.value = await res.json()
  } catch (err) {
    loadError.value = err.message || '对照数据加载失败'
  }
}

function setCompareCanvas(key, el) {
  compareCanvases[key] = el
  if (el) void drawCompareColumn(key)
}

/** 把指定通道的检测框画到对照列的画布上 */
function drawCompareColumn(key) {
  const col = compareColumns.value.find((item) => item.key === key)
  const el = compareCanvases[key]
  if (!col || !el || !preview.value) return
  drawDetections(el, preview.value, col.detections)
}

/** 把一张图 + 一组检测框画到指定画布 */
function drawDetections(canvasEl, src, detections) {
  if (!canvasEl || !src) return
  const img = new Image()
  img.onload = () => {
    canvasEl.width = img.naturalWidth
    canvasEl.height = img.naturalHeight
    const ctx = canvasEl.getContext('2d')
    ctx.drawImage(img, 0, 0)
    const scale = Math.max(1, Math.min(3, img.naturalWidth / 1200))
    for (const d of detections || []) {
      const [x1, y1, x2, y2] = d.bbox
      const color = displayClassColor(d.class)
      ctx.strokeStyle = color
      ctx.lineWidth = 2 * scale
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1)
      ctx.fillStyle = color
      ctx.font = `${Math.round(13 * scale)}px sans-serif`
      const label = `${displayClassLabel(d.class)} ${(d.confidence * 100).toFixed(1)}%`
      const tw = ctx.measureText(label).width
      const labelHeight = Math.round(22 * scale)
      ctx.fillRect(x1, Math.max(0, y1 - labelHeight), tw + 8 * scale, labelHeight)
      ctx.fillStyle = '#fff'
      ctx.fillText(label, x1 + 4 * scale, Math.max(labelHeight - 6 * scale, y1 - 6 * scale))
    }
  }
  img.onerror = () => {
    sampleError.value = '样例图加载失败'
  }
  img.src = src
}

onMounted(() => {
  void ensureCompareData()
  const sampleId = typeof route.query.sample === 'string' ? route.query.sample : ''
  const sample = VISION_SAMPLES.find((item) => item.id === sampleId)
  if (sample) loadSample(sample)
})

// 样例切换后重画两列
watch([compareEntry, preview], () => {
  for (const key of ['cv', 'world']) void drawCompareColumn(key)
})

// ?sample= 直达变化（同一路由内再点另一张样本图，组件不会重新挂载）。
// 与 ChatView 同一个坑：只在 onMounted 里读 query，第二次换样例就不会生效。
watch(
  () => route.query.sample,
  (sampleId) => {
    if (typeof sampleId !== 'string' || !sampleId || sampleId === activeSampleId.value) return
    const sample = VISION_SAMPLES.find((item) => item.id === sampleId)
    if (sample) loadSample(sample)
  },
)
</script>

<style scoped>
.vcompare {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  min-height: 0;
}

.vcompare__head {
  flex-shrink: 0;
}

.vcompare__title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-main);
}

.vcompare__sub {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-sub);
  line-height: 1.6;
}

.vcompare__sub b {
  color: var(--text-main);
}

.vcompare__samples {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.vcompare__samples > span {
  flex-shrink: 0;
  color: var(--text-dim);
  font-size: 11.5px;
}

.vcompare__sample-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.vcompare__sample-buttons button {
  min-height: 29px;
  padding: 4px 10px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: var(--bg-panel);
  color: var(--text-sub);
  font-size: 11.5px;
  cursor: pointer;
}

.vcompare__sample-buttons button:hover {
  border-color: var(--c-primary-dim);
  color: var(--c-primary);
}

.vcompare__sample-buttons button.vcompare__sample--active {
  border-color: rgba(0, 122, 255, 0.3);
  background: var(--bg-active);
  color: var(--c-primary);
  font-weight: 600;
}

.vcompare__error {
  margin-top: 6px;
  color: var(--c-danger);
  font-size: 12px;
}

.vcompare__body {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

.vcompare__body-inner {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
}

.vcompare__lead {
  margin: 0 0 12px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  font-size: 12px;
  line-height: 1.65;
}

.vcompare__lead b {
  color: var(--text-main);
}

.vcompare__columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.vcompare__column {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 1px solid var(--panel-border);
  border-radius: 10px;
  overflow: hidden;
}

.vcompare__column-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--separator);
  font-size: 12.5px;
  color: var(--text-main);
}

.vcompare__column-engine {
  color: var(--text-dim);
  font-size: 10.5px;
  text-align: right;
}

.vcompare__column-stage {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 180px;
  padding: 10px;
  background: var(--bg-panel-2);
}

.vcompare__column-canvas {
  max-width: 100%;
  max-height: 340px;
  border-radius: 8px;
}

.vcompare__column-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 0;
  border-bottom: 1px solid var(--separator);
}

.vcompare__column-stats > div {
  padding: 7px 10px;
  border-right: 1px solid var(--separator);
}

.vcompare__column-stats > div:last-child {
  border-right: 0;
}

.vcompare__column-stats dt {
  color: var(--text-dim);
  font-size: 10.5px;
}

.vcompare__column-stats dd {
  margin: 2px 0 0;
  color: var(--text-main);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.vcompare__column-note {
  margin: 0;
  padding: 9px 10px;
  color: var(--text-sub);
  font-size: 11.5px;
  line-height: 1.6;
}

.vcompare__column-note b {
  color: var(--text-main);
}

.vcompare__regime {
  margin-top: 14px;
  border: 1px solid var(--panel-border);
  border-radius: 10px;
  overflow: hidden;
}

.vcompare__regime-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 10px;
  border-bottom: 1px solid var(--separator);
  font-size: 12.5px;
  color: var(--text-main);
}

.vcompare__regime-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin: 0;
  border-bottom: 1px solid var(--separator);
}

.vcompare__regime-grid > div {
  padding: 7px 10px;
  border-right: 1px solid var(--separator);
}

.vcompare__regime-grid > div:last-child {
  border-right: 0;
}

.vcompare__regime-grid dt {
  color: var(--text-dim);
  font-size: 10.5px;
}

.vcompare__regime-grid dd {
  margin: 2px 0 0;
  color: var(--text-main);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.vcompare__regime-note {
  margin: 0;
  padding: 9px 10px;
  color: var(--text-sub);
  font-size: 11.5px;
  line-height: 1.6;
}

.vcompare__regime-note b {
  color: var(--text-main);
}

.vcompare__caveats {
  margin: 12px 0 0;
  padding: 10px 10px 10px 26px;
  border-radius: 8px;
  background: var(--bg-panel-2);
  color: var(--text-dim);
  font-size: 11.5px;
  line-height: 1.65;
}

@media (max-width: 900px) {
  .vcompare {
    height: auto;
    min-height: 100%;
  }

  .vcompare__columns {
    grid-template-columns: 1fr;
  }

  .vcompare__body {
    flex: none;
  }

  .vcompare__body-inner {
    padding: 12px 14px;
  }
}

@media (max-width: 520px) {
  .vcompare__samples {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }

  .vcompare__regime-grid {
    grid-template-columns: 1fr;
  }

  .vcompare__regime-grid > div,
  .vcompare__regime-grid > div:last-child {
    border-right: 0;
    border-bottom: 1px solid var(--separator);
  }

  .vcompare__regime-grid > div:last-child {
    border-bottom: 0;
  }

  .vcompare__column-stats {
    grid-template-columns: 1fr;
  }

  .vcompare__column-stats > div,
  .vcompare__column-stats > div:last-child {
    border-right: 0;
    border-bottom: 1px solid var(--separator);
  }

  .vcompare__column-stats > div:last-child {
    border-bottom: 0;
  }
}
</style>

<template>
  <div class="chart-panel" :class="{ panel: !bare }">
    <div v-if="title" class="panel-title">
      <span>{{ title }}</span>
      <slot name="action"></slot>
    </div>
    <div ref="chartEl" class="chart-panel__body" :style="{ height: `${height}px` }"></div>
  </div>
</template>

<script setup>
/**
 * ECharts 通用封装。
 *
 * 关键点：
 * 1. resize 监听要防抖 —— 大屏拖拽窗口时 ECharts 重绘很吃性能
 * 2. 组件卸载必须 dispose —— 否则切页会把实例泄漏在内存里
 * 3. option 变化用 setOption(option, true) 而不是重建实例
 * 4. bare=true 时不再套一层 .panel 卡片：用于嵌在已有面板内部的图表
 *    （如 Agent 评测摘要里嵌的质量雷达），避免面板套面板的双层边框。
 */
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  title: { type: String, default: '' },
  option: { type: Object, required: true },
  height: { type: Number, default: 240 },
  bare: { type: Boolean, default: false },
})

const chartEl = ref(null)
let chart = null
let resizeTimer = null
let resizeObserver = null

function render() {
  if (!chartEl.value) return
  if (!chart) {
    chart = echarts.init(chartEl.value, null, { renderer: 'canvas' })
  }
  chart.setOption(props.option, true)
}

function onResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => chart?.resize(), 150)
}

onMounted(async () => {
  await nextTick()
  render()
  window.addEventListener('resize', onResize)
  // window.resize 只在窗口尺寸变时触发；侧栏开合、面板折叠、grid 重排
  // 这类"容器变了但窗口没变"的情况要靠 ResizeObserver 才能感知到
  if (chartEl.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(onResize)
    resizeObserver.observe(chartEl.value)
  }
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (resizeTimer) clearTimeout(resizeTimer)
  chart?.dispose()
  chart = null
})

watch(() => props.option, render, { deep: true })
// height prop 改了容器的像素高度，但 ECharts 画布不会自动跟随，要主动 resize
watch(() => props.height, onResize)
</script>

<style scoped>
.chart-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.chart-panel__body {
  width: 100%;
  flex: 1;
  min-height: 0;
}
</style>

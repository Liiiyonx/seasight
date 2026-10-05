<template>
  <!--
    轻量内联图标。用途：替代 Unicode 字形（↗ ✓ ⧉ ↻ 等）。

    为什么不用图标库：这个仓库此前不引入任何图标依赖，机械臂控制台
    只需要 4 个图标。为此装unplugin-icons + iconify-json（数十 MB
    node_modules）不划算，而且构建期插件对开发机环境有额外要求。
    内联 SVG 零依赖、体积可控，深浅色跟随 currentColor。

    ★ SVG 必须显式给 width/height —— font-size 对 SVG 无效。
    -->
  <svg
    class="ui-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <path v-for="(d, i) in paths" :key="i" :d="d" />
  </svg>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 15 },
  strokeWidth: { type: [Number, String], default: 2 },
})

/** lucide 24×24 网格下的等价路径 */
const PATHS = {
  layers: [
    'M12 2 3 7l9 5 9-5-9-5z',
    'M3 12l9 5 9-5',
    'M3 17l9 5 9-5',
  ],
  'external-link': ['M15 3h6v6', 'M10 14 21 3', 'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'],
  'loader-circle': ['M21 12a9 9 0 1 1-6.2-8.6'],
  check: ['M20 6 9 17l-5-5'],
  copy: [
    'M9 9h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2z',
    'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
  ],
  // 心电波形：用于「舵机遥测」这类实时读数
  activity: ['M22 12h-4l-3 9L9 3l-3 9H2'],
}

const paths = computed(() => PATHS[props.name] || PATHS.check)
</script>

<style scoped>
.ui-icon {
  display: inline-block;
  flex-shrink: 0;
  vertical-align: -0.15em;
}
</style>

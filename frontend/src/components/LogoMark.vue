<template>
  <svg
    class="logo-mark"
    viewBox="0 0 48 48"
    :width="size"
    :height="size"
    aria-hidden="true"
  >
    <defs>
      <linearGradient :id="backgroundId" x1="7" y1="4" x2="41" y2="44" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#0877e8" />
        <stop offset="1" stop-color="#00a6c8" />
      </linearGradient>
      <linearGradient :id="lensId" x1="30" y1="15" x2="39" y2="25" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#e8fdff" />
        <stop offset="0.48" stop-color="#71ebff" />
        <stop offset="1" stop-color="#22b9dc" />
      </linearGradient>
    </defs>

    <!-- 应用图标底座：保证 Logo 在顶栏、浏览器标签和移动端保持同一识别 -->
    <rect x="2" y="2" width="44" height="44" rx="13" :fill="`url(#${backgroundId})`" />
    <rect
      x="2.75"
      y="2.75"
      width="42.5"
      height="42.5"
      rx="12.25"
      fill="none"
      stroke="#ffffff"
      stroke-opacity="0.2"
      stroke-width="1.5"
    />

    <!-- 探海望远镜 -->
    <g transform="rotate(-20 24 20)">
      <rect x="8.6" y="17.1" width="5.8" height="5.8" rx="2.9" fill="#ffffff" opacity="0.8" />
      <rect x="12" y="15.3" width="22.6" height="9.4" rx="4.7" fill="#ffffff" />
      <rect x="16.8" y="15.3" width="4.2" height="9.4" fill="#b9effb" opacity="0.92" />
      <rect x="27.1" y="14.4" width="2.8" height="11.2" rx="1.4" fill="#d6f8ff" opacity="0.96" />
      <circle
        cx="34.5"
        cy="20"
        r="6.3"
        fill="#07324f"
        stroke="#ffffff"
        stroke-width="2.55"
      />
      <circle cx="34.5" cy="20" r="3.55" :fill="`url(#${lensId})`" />
      <path
        d="M32.7 18.2c0.8-0.9 1.9-1.35 3.1-1.2"
        fill="none"
        stroke="#ffffff"
        stroke-width="1.25"
        stroke-linecap="round"
        opacity="0.9"
      />
    </g>

    <!-- 探海波浪 -->
    <path
      d="M7.5 32.2c2.75-2.15 5.5-2.15 8.25 0s5.5 2.15 8.25 0 5.5-2.15 8.25 0 5.5 2.15 8.25 0"
      fill="none"
      stroke="#ffffff"
      stroke-width="2.15"
      stroke-linecap="round"
    />
    <path
      d="M13.5 38.2c1.75-1.45 3.5-1.45 5.25 0s3.5 1.45 5.25 0 3.5-1.45 5.25 0 3.5 1.45 5.25 0"
      fill="none"
      stroke="#c9f8ff"
      stroke-opacity="0.72"
      stroke-width="1.7"
      stroke-linecap="round"
    />
  </svg>
</template>

<script setup>
import { useId } from 'vue'

defineProps({
  size: { type: Number, default: 30 },
})

const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '')
const backgroundId = `seasight-logo-bg-${uid}`
const lensId = `seasight-logo-lens-${uid}`
</script>

<style scoped>
.logo-mark {
  display: block;
  flex: 0 0 auto;
  filter: drop-shadow(0 5px 12px rgba(0, 92, 190, 0.2));
}
</style>

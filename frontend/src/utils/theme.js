/**
 * 主题管理：浅色（默认）与深色切换，localStorage 持久化。
 *
 * 实现：给 <html> 设 data-theme 属性（'dark' / 'light'），
 * CSS 默认变量即浅色；`html[data-theme='dark']` 覆盖深色变量。
 */

import { ref } from 'vue'

const THEME_KEY = 'seasight_theme'

/**
 * 响应式主题状态。
 * 为什么需要：ECharts/canvas 等画在 JS 里的颜色拿不到 CSS 变量，
 * 需要在 computed 里依赖此 ref，主题切换时才能触发重算重绘
 * （见 utils/palette.js）。
 */
export const currentTheme = ref(localStorage.getItem(THEME_KEY) || 'light')

export function getTheme() {
  return currentTheme.value
}

export function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', t)
  localStorage.setItem(THEME_KEY, t)
  // 先改 DOM 属性再更新 ref：依赖 currentTheme 的 computed 重算时，
  // getComputedStyle 读到的已是新主题的变量值
  currentTheme.value = t
  return t
}

export function toggleTheme() {
  return applyTheme(currentTheme.value === 'light' ? 'dark' : 'light')
}

export function initTheme() {
  applyTheme(currentTheme.value)
}

/**
 * 主题管理：浅色（默认）与深色切换，localStorage 持久化。
 *
 * 实现：给 <html> 设 data-theme 属性（'dark' / 'light'），
 * CSS 默认变量即浅色；`html[data-theme='dark']` 覆盖深色变量。
 */

const THEME_KEY = 'seasight_theme'

export function getTheme() {
  return localStorage.getItem(THEME_KEY) || 'light'
}

export function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', t)
  localStorage.setItem(THEME_KEY, t)
  return t
}

export function toggleTheme() {
  return applyTheme(getTheme() === 'light' ? 'dark' : 'light')
}

export function initTheme() {
  applyTheme(getTheme())
}

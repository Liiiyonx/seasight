/**
 * CSS 变量 → JS 取色桥：ECharts / canvas 配色统一走这里。
 *
 * 为什么需要：图表 option 在 JS 里拼，拿不到 CSS 变量；此前各视图
 * 硬编码 #007aff / #8b96a8 等浅色主题色值，切到深色主题后
 * 轴标签发灰、主色对比不足（漏色）。
 *
 * 用法：在 computed 里调用 palette() —— 函数内部依赖 currentTheme，
 * 主题切换会让依赖它的 computed 重算，ChartPanel watch option 后重绘。
 * 注意：不要在模块顶层调用（那时 DOM 还没挂、样式变量读不到）。
 */

import { currentTheme } from './theme'
import { WASTE_CLASSES } from './constants'

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name)
  return v.trim() || fallback
}

/** hex → rgba（ECharts canvas 只认真实颜色值，不支持 CSS color-mix 字符串） */
function withAlpha(color, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(String(color).trim())
  if (!m) return color
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

/** 图表通用配色（跟随亮/暗主题） */
export function palette() {
  void currentTheme.value // 依赖主题：切换时触发上层 computed 重算
  const dim = cssVar('--text-dim', '#8e8e93')
  return {
    primary: cssVar('--c-primary', '#007aff'),
    info: cssVar('--c-info', '#007aff'),
    success: cssVar('--c-success', '#34c759'),
    warn: cssVar('--c-warn', '#ff9500'),
    danger: cssVar('--c-danger', '#ff3b30'),
    purple: cssVar('--c-assigned', '#5856d6'),
    // 轴/网格线：用分隔线变量，浅色灰阶、深色白阶，两套主题都够淡
    axisLabel: dim,
    axisLine: cssVar('--separator', 'rgba(60, 60, 67, 0.12)'),
    splitLine: cssVar('--separator', 'rgba(60, 60, 67, 0.12)'),
    pieBorder: cssVar('--panel-border', 'rgba(60, 60, 67, 0.12)'),
    // 面积/光晕（带透明的主色）
    primaryGlow: cssVar('--c-primary-glow', 'rgba(0, 122, 255, 0.16)'),
    primarySoft: cssVar('--grad-primary-soft', 'rgba(0, 122, 255, 0.1)'),
    // 雷达图分层底色（深浅主题各取自 text-dim 的低透明度）
    splitArea: [withAlpha(dim, 0.04), withAlpha(dim, 0.02)],
  }
}

/** 垃圾类别色：与 main.css 的 --c-foam/--c-plastic/... 同源，跟随主题 */
export function classThemeColor(code) {
  void currentTheme.value
  const map = {
    foam: '--c-foam',
    plastic: '--c-plastic',
    fishing_gear: '--c-fishing',
    other: '--c-other',
  }
  const name = map[code]
  return name ? cssVar(name, WASTE_CLASSES[code]?.color || '#8e8e93') : WASTE_CLASSES[code]?.color || '#8e8e93'
}

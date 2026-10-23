/**
 * 演示口径字幕的开关状态（全局单例）。
 *
 * 为什么单独抽一个模块：字幕条的显隐状态要独立于组件生命周期，
 * 放模块级 ref 最省事，也不会被组件的挂载顺序影响。
 *
 * 入口约定（顶栏不再常驻开关按钮，保持界面干净）：
 *   - 打开：访问任意页面带 ?demo=1（演示录屏入口），或 localStorage
 *     手动置 seasight_demo_caption=1；
 *   - 关闭：字幕条自带的 × 按钮（hideCaption 落库记住选择）。
 */
import { ref } from 'vue'

const STORAGE_KEY = 'seasight_demo_caption'

function readStored() {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    // 隐私模式 / 禁用存储：当作"没选过"
    return null
  }
}

function writeStored(value) {
  try {
    window.localStorage.setItem(STORAGE_KEY, value)
  } catch {
    /* 同上：静默降级为"仅本次会话有效" */
  }
}

function initialVisible() {
  const stored = readStored()
  if (stored === '1') return true
  if (stored === '0') return false
  // 没显式选过时：只有走 ?demo=1 演示入口才默认打开，
  // 日常使用不往界面右下角挂一条常驻说明条。
  try {
    return new URLSearchParams(window.location.search).get('demo') === '1'
  } catch {
    return false
  }
}

export const captionVisible = ref(initialVisible())

export function hideCaption() {
  captionVisible.value = false
  writeStored('0')
}

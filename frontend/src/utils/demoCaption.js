/**
 * 演示口径字幕的开关状态（全局单例）。
 *
 * 为什么单独抽一个模块：顶栏要放切换按钮，画面右下角要放字幕条，
 * 两处在不同的组件里。用 provide/inject 或父子回调都能做，但都会把
 * App.vue 和字幕组件耦上；这里只是一份"开/关 + 落 localStorage"的
 * 极简状态，放模块级 ref 最省事，也不会被组件的挂载顺序影响。
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

export function toggleCaption() {
  captionVisible.value = !captionVisible.value
  writeStored(captionVisible.value ? '1' : '0')
}

export function hideCaption() {
  captionVisible.value = false
  writeStored('0')
}

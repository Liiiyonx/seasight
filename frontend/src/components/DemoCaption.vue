<template>
  <div v-if="captionVisible" class="demo-caption" role="note" aria-live="polite">
    <span class="demo-caption__dot" :class="{ 'demo-caption__dot--ok': verified }"></span>
    <span class="demo-caption__text">{{ text }}</span>
    <button
      class="demo-caption__close"
      type="button"
      aria-label="关闭口径字幕"
      title="关闭口径字幕（演示录屏用的说明条）"
      @click="hideCaption()"
    >
      ×
    </button>
  </div>
</template>

<script setup>
/**
 * 口径字幕（演示录屏常驻角标）。
 *
 * 为什么要有它：本项目有一条贯穿始终的纪律 —— 不伪造状态。大屏上的"在线"
 * 徽标、Agent 控制台的"规则模式"都取自真实 API，但画面之外的人（评委、
 * 转发视频的人）看不到这些上下文，容易把"合成事件 + 规则兜底"误读成
 * "接了真实海域数据、跑了大模型"。这条常驻字幕就是把口径钉在画面上。
 *
 * 诚实性要求：字幕里的模式不是写死的文案 —— 它会静默拉一次
 * /agents/runtime/status 做自检：
 *   自检通过 → 绿点 + "已自检"；
 *   自检不了（未登录 / 后端不可达）→ 保留文案但明确标"未自检"，
 *   而不是假装验证过，也不弹全局错误条（这是装饰性信息）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { agentsApi } from '@/api'
import { isLoggedIn } from '@/utils/auth'
import { captionVisible, hideCaption } from '@/utils/demoCaption'

const route = useRoute()
const verified = ref(false)
const ruleMode = ref(null) // null = 还没自检过

/**
 * 对照画面的口径追加段。
 *
 * 为什么按路由判断而不是传 props：这条字幕是全局常驻角标，
 * 挂在 App.vue 最外层；把"当前是不是对照画面"用 props 一层层传下来
 * 会把 App 和视图耦上。路由 name 已经是现成的、且会同步的状态，
 * 直接读它最省事。
 */
const compareNote = computed(() =>
  route.name === 'vision-compare'
    ? ' · 双通道对照为定性：两通道口径不同、置信度不可横向比较'
    : '',
)

const text = computed(() => {
  const mode = ruleMode.value === false ? '模型增强模式' : '规则模式'
  const check = verified.value ? '已自检' : '未自检'
  return `合成事件 · ${mode} · 无模型依赖当场可复现 · ${check}${compareNote.value}`
})

let timerState = null

async function selfCheck() {
  if (!isLoggedIn()) {
    verified.value = false
    return
  }
  try {
    const status = await agentsApi.runtimeStatus()
    ruleMode.value = status?.rule_mode === true
    verified.value = true
  } catch {
    verified.value = false
  }
}

onMounted(() => {
  if (captionVisible.value) selfCheck()
  timerState = setInterval(() => {
    if (captionVisible.value) selfCheck()
  }, 60000)
})

watch(captionVisible, (on) => {
  if (on) selfCheck()
})

onBeforeUnmount(() => {
  if (timerState) clearInterval(timerState)
  timerState = null
})
</script>

<style scoped>
.demo-caption {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 260;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: min(560px, calc(100vw - 32px));
  padding: 7px 10px 7px 12px;
  border-radius: 999px;
  border: 1px solid var(--panel-border, var(--border));
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent);
  backdrop-filter: blur(14px) saturate(1.2);
  -webkit-backdrop-filter: blur(14px) saturate(1.2);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.14);
  font-size: 12px;
  color: var(--text-main);
  line-height: 1.4;
}

.demo-caption__dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--c-warn, #ff9500);
}

.demo-caption__dot--ok {
  background: var(--c-success, #34c759);
}

.demo-caption__text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.demo-caption__close {
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--text-dim);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}

.demo-caption__close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

@media (max-width: 620px) {
  .demo-caption {
    right: 12px;
    /* 上移避开移动端底部 Tab 栏（见 App.vue .tabbar） */
    bottom: calc(84px + env(safe-area-inset-bottom, 0px));
    font-size: 11.5px;
  }
}
</style>

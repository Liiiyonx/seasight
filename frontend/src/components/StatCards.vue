<template>
  <div class="stat-grid">
    <div
      v-for="(card, i) in cards"
      :key="card.key"
      class="stat-card"
      :class="{ 'stat-card--link': card.hint }"
      :style="{ '--card-color': card.color, animationDelay: `${i * 50}ms` }"
    >
      <div class="stat-card__glow"></div>
      <div class="stat-card__label">{{ card.label }}</div>
      <div class="stat-card__value">
        <span class="stat-card__num grad-text">
          {{ card.text || fmtNumber(display[card.key] ?? card.numeric, card.digits) }}
        </span>
        <span v-if="card.unit" class="stat-card__unit">{{ card.unit }}</span>
      </div>
      <div v-if="card.sub" class="stat-card__sub">{{ card.sub }}</div>
    </div>
  </div>
</template>

<script setup>
/**
 * 指标卡 —— 大屏的第一眼。
 *
 * 数字用 requestAnimationFrame 从旧值滚动到新值（count-up），
 * 避免大屏刷新时数字「跳变」——流畅的数字过渡比静态刷新更有「活」的感觉。
 */
import { computed, reactive, watch, onUnmounted } from 'vue'
import { fmtNumber } from '@/utils/format'

const props = defineProps({
  stats: { type: Object, default: () => ({}) },
})

const cards = computed(() => {
  const s = props.stats || {}
  return [
    { key: 'events', label: '24 小时事件', numeric: s.event_count_24h || 0, digits: 0, unit: '条', color: '#ff9500', sub: '按发现时间统计' },
    { key: 'pending', label: '待派单工单', numeric: s.pending_tasks || 0, digits: 0, unit: '单', color: '#8e8e93', sub: (s.pending_tasks || 0) > 0 ? '需要关注' : '全部已派发' },
    // 口径：后端 collecting_tasks 统计的是 collecting 或 navigating，
    // 也就是「机器人已经在路上/在作业」的合计，不等于仅有打捞动作的那些。
    { key: 'collecting', label: '作业中', numeric: s.collecting_tasks || 0, digits: 0, unit: '单', color: '#007aff', sub: '含前往中与作业中' },
    { key: 'done', label: '今日完成', numeric: s.done_tasks_24h || 0, digits: 0, unit: '单', color: '#34c759', sub: '近 24 小时' },
    { key: 'robots', label: '机器人在线', text: `${s.robots_online || 0}/${s.robots_total || 0}`, unit: '台', color: (s.robots_online || 0) > 0 ? '#007aff' : '#ff3b30', sub: '在线 / 总数' },
    // ★ 口径必须写出来：这里是全表合计（含人工建单）；
    //   「治理报表」的清理总量只统计关联了事件的工单，
    //   两个数字天然不同，不写脚注会被当成算错。
    { key: 'weight', label: '累计清理', numeric: s.collected_kg_total || 0, digits: 1, unit: 'kg', color: '#34c759', sub: '全表工单累计（含人工建单）' },
  ]
})

// 每个数字卡片的当前动画值（count-up 过程中的中间值）
const display = reactive({})
// 按卡片 key 记录进行中的动画帧。同一张卡的目标值刷新时，先 cancel 掉旧动画
// 再起新的——否则两个 step() 同时往 display[key] 写值，数字会来回打架
const rafs = new Map()

function animate(key, target, duration = 750) {
  const prev = rafs.get(key)
  if (prev != null) cancelAnimationFrame(prev)
  const start = display[key] ?? 0
  const t0 = performance.now()
  function step(now) {
    const p = Math.min((now - t0) / duration, 1)
    const eased = 1 - Math.pow(1 - p, 3) // ease-out cubic
    display[key] = start + (target - start) * eased
    if (p < 1) {
      rafs.set(key, requestAnimationFrame(step))
    } else {
      rafs.delete(key)
    }
  }
  rafs.set(key, requestAnimationFrame(step))
}

watch(
  cards,
  (list) => {
    for (const c of list) {
      if (c.numeric != null) animate(c.key, c.numeric)
    }
  },
  { immediate: true, deep: true },
)

onUnmounted(() => {
  rafs.forEach((r) => cancelAnimationFrame(r))
  rafs.clear()
})
</script>

<style scoped>
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
}

.stat-card {
  background: var(--bg-panel);
  backdrop-filter: blur(14px) saturate(1.25);
  -webkit-backdrop-filter: blur(14px) saturate(1.25);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 13px 16px;
  position: relative;
  overflow: hidden;
  animation: rise-in 0.5s var(--ease) both;
  transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease);
}

.stat-card:hover {
  transform: translateY(-2px);
  border-color: var(--border-bright);
  box-shadow: var(--shadow-glow);
}

/* 顶部渐变光带（用卡片主题色） */
.stat-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--card-color), transparent);
  opacity: 0.55;
}

/* 右上角柔和光晕 */
.stat-card__glow {
  position: absolute;
  top: -30px;
  right: -30px;
  width: 90px;
  height: 90px;
  border-radius: 50%;
  background: radial-gradient(circle, color-mix(in srgb, var(--card-color) 22%, transparent), transparent 70%);
  pointer-events: none;
}

.stat-card__label {
  font-size: 12px;
  color: var(--text-sub);
  letter-spacing: 0.4px;
  margin-bottom: 5px;
}

.stat-card__value {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.stat-card__num {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
  font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace;
}

.stat-card__unit {
  font-size: 12px;
  color: var(--text-sub);
  font-weight: 400;
}

.stat-card__sub {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 4px;
}

@media (max-width: 1400px) {
  .stat-grid { grid-template-columns: repeat(3, 1fr); }
}

/* ---------- iOS 风格覆写 ---------- */
.stat-grid {
  gap: 12px;
}

.stat-card {
  min-width: 0;
  padding: 15px 16px;
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
  box-shadow: var(--shadow-card);
}

.stat-card:hover {
  transform: none;
  border-color: var(--panel-border);
  box-shadow: var(--shadow-card);
}

.stat-card::before,
.stat-card__glow {
  display: none;
}

.stat-card__label {
  margin-bottom: 7px;
  color: var(--text-sub);
  letter-spacing: 0;
}

.stat-card__num {
  color: var(--text-main);
  font-size: 25px;
  font-weight: 650;
}

.stat-card__sub {
  margin-top: 5px;
  color: var(--text-dim);
}

@media (max-width: 760px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
}

@media (max-width: 360px) {
  .stat-card {
    padding: 13px 12px;
  }

  .stat-card__num {
    font-size: 22px;
  }
}
</style>

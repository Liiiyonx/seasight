<template>
  <div class="page">
    <!-- 汇总指标 -->
    <div class="summary">
      <div class="summary__card">
        <span class="summary__label">统计周期</span>
        <span class="summary__value">近 {{ days }} 天</span>
      </div>
      <div class="summary__card">
        <span class="summary__label">发现事件</span>
        <span class="summary__value text-warn">{{ totals.events }}</span>
      </div>
      <div class="summary__card">
        <span class="summary__label">完成工单</span>
        <span class="summary__value text-primary">{{ totals.done }}</span>
      </div>
      <div class="summary__card">
        <span class="summary__label">清理总量</span>
        <span class="summary__value">
          {{ totals.kg.toFixed(1) }}<span class="summary__unit">kg</span>
        </span>
      </div>
      <div class="summary__card">
        <span class="summary__label">治理面积</span>
        <span class="summary__value" v-if="totals.areaAvailable">
          {{ (totals.area / 10000).toFixed(2) }}<span class="summary__unit">万 m²</span>
          <span v-if="totals.areaPartial" class="text-dim" style="font-size: 12px">（部分）</span>
        </span>
        <span class="summary__value text-dim" v-else>未统计</span>
      </div>

      <div class="summary__spacer"></div>

      <div class="summary__actions">
        <select v-model.number="days" @change="load">
          <option :value="7">近 7 天</option>
          <option :value="14">近 14 天</option>
          <option :value="30">近 30 天</option>
          <option :value="90">近 90 天</option>
        </select>
        <button class="btn" @click="load">刷新</button>
        <button class="btn" @click="exportCsv">导出 CSV</button>
      </div>
    </div>

    <!-- 口径声明：这几个数字和大屏不是同一套统计范围，
         不写清楚会被当成算错（大屏「累计清理」是全表合计，
         这里只统计关联了事件的工单，且有天数窗口）。 -->
    <p class="summary__note">
      口径：本页按<strong>发现时间</strong>取近 {{ days }} 天，且只统计
      <strong>关联了事件的工单</strong>（按乡镇 × 类别聚合）。人工建单没有事件类别，
      因此不计入「清理总量」。大屏的「累计清理」是全表合计，两者数值不同属正常。
    </p>

    <!-- 图表区 -->
    <div class="chart-row">
      <ChartPanel title="各乡镇治理成果对比" :option="townshipOption" :height="250" />
      <ChartPanel title="垃圾类别构成" :option="classOption" :height="250" />
    </div>

    <!-- 明细表 -->
    <section class="panel table-panel">
      <div class="panel-title">
        <span>治理量化明细</span>
        <span class="text-dim" style="font-size: 12px; font-weight: 400">
          {{ rows.length }} 条记录
        </span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th style="width: 120px">日期</th>
            <th style="width: 110px">乡镇</th>
            <th style="width: 110px">垃圾类别</th>
            <th style="width: 100px">发现事件</th>
            <th style="width: 100px">产生工单</th>
            <th style="width: 100px">完成工单</th>
            <th style="width: 120px">清理量 (kg)</th>
            <th style="width: 130px">治理面积 (m²)</th>
            <th>完成率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="i">
            <td class="num">{{ r.stat_date }}</td>
            <td>{{ r.township || '未标注' }}</td>
            <td>
              <i class="dot" :style="{ background: classColor(r.main_class) }"></i>
              {{ r.main_class_label || classLabel(r.main_class) }}
            </td>
            <td class="num">{{ r.event_count }}</td>
            <td class="num">{{ r.task_count }}</td>
            <td class="num text-primary">{{ r.done_count }}</td>
            <td class="num">{{ Number(r.collected_kg).toFixed(2) }}</td>
            <td class="num">
              <!-- WP-07：coverage_area 为 null = 未统计（不是 0），与真实 0 区分 -->
              <template v-if="r.coverage_area == null">
                <span class="text-dim">未统计</span>
              </template>
              <template v-else>
                {{ fmtNumber(r.coverage_area) }}
                <span v-if="r.coverage_availability === 'partial'" class="text-dim" style="font-size: 11px">（部分）</span>
              </template>
            </td>
            <td>
              <div class="rate">
                <div class="rate__track">
                  <div class="rate__fill" :style="{ width: `${completion(r)}%` }"></div>
                </div>
                <span class="rate__val">{{ completion(r) }}%</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!rows.length" class="empty">
        {{ loading ? '加载中…' : '暂无报表数据（需先运行报表聚合任务）' }}
      </div>
    </section>
  </div>
</template>

<script setup>
/**
 * 治理报表 —— 这个页面对接政府客户的核心诉求：
 * "我投了这套系统，到底清理了多少垃圾、覆盖了多大面积？"
 * 报表只读预聚合宽表，所以 30 天数据也是毫秒响应。
 */
import { ref, computed, onMounted } from 'vue'
import ChartPanel from '@/components/ChartPanel.vue'
import { reportsApi, downloadBlob } from '@/api'
import { useRealtimeStore } from '@/stores/realtime'
import { classLabel, classColor, WASTE_CLASSES, CLASS_ORDER } from '@/utils/constants'
import { palette, classThemeColor } from '@/utils/palette'
import { fmtNumber } from '@/utils/format'

const store = useRealtimeStore()

const days = ref(7)
const rows = ref([])
const summary = ref([])
const loading = ref(false)

const totals = computed(() => {
  // WP-07：coverage_area 只累加非 null（实测）值；全 null 时 areaAvailable=false → 显示「未统计」
  const t = { events: 0, done: 0, kg: 0, area: 0, areaAvailable: false, areaPartial: false }
  for (const s of summary.value) {
    t.events += s.event_count || 0
    t.done += s.done_count || 0
    t.kg += s.collected_kg || 0
    if (s.coverage_area != null) {
      t.area += s.coverage_area
      t.areaAvailable = true
    }
    if (s.coverage_availability === 'partial') t.areaPartial = true
  }
  return t
})

function completion(row) {
  if (!row.task_count) return 0
  return Math.round((row.done_count / row.task_count) * 100)
}

const townshipOption = computed(() => {
  const p = palette()
  const names = summary.value.map((s) => s.township)
  return {
    grid: { left: 78, right: 20, top: 26, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: {
      data: ['发现事件', '完成工单'],
      right: 10,
      top: 0,
      textStyle: { color: p.axisLabel, fontSize: 11 },
      itemWidth: 10,
      itemHeight: 10,
    },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: p.splitLine } },
      axisLabel: { color: p.axisLabel, fontSize: 11 },
    },
    yAxis: {
      type: 'category',
      data: names,
      axisLine: { lineStyle: { color: p.axisLine } },
      axisLabel: { color: p.axisLabel, fontSize: 11 },
    },
    series: [
      {
        name: '发现事件',
        type: 'bar',
        data: summary.value.map((s) => s.event_count),
        itemStyle: { color: p.warn, borderRadius: [0, 3, 3, 0] },
        barWidth: 9,
      },
      {
        name: '完成工单',
        type: 'bar',
        data: summary.value.map((s) => s.done_count),
        itemStyle: { color: p.primary, borderRadius: [0, 3, 3, 0] },
        barWidth: 9,
      },
    ],
  }
})

const classOption = computed(() => {
  const p = palette()
  // 按类别聚合明细行
  const agg = {}
  for (const r of rows.value) {
    const k = r.main_class || 'other'
    agg[k] = (agg[k] || 0) + (r.event_count || 0)
  }

  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} 条 ({d}%)' },
    legend: {
      bottom: 0,
      textStyle: { color: p.axisLabel, fontSize: 11 },
      itemWidth: 9,
      itemHeight: 9,
    },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '44%'],
        itemStyle: { borderColor: p.pieBorder, borderWidth: 1 },
        label: {
          color: p.axisLabel,
          fontSize: 11,
          formatter: '{b}\n{c}',
        },
        data: CLASS_ORDER.map((k) => ({
          name: WASTE_CLASSES[k].label,
          value: agg[k] || 0,
          itemStyle: { color: classThemeColor(k) },
        })).filter((d) => d.value > 0),
      },
    ],
  }
})

async function load() {
  loading.value = true
  try {
    const [d, s] = await Promise.all([
      reportsApi.daily({ days: days.value }),
      reportsApi.summary(days.value),
    ])
    rows.value = d || []
    summary.value = s || []
  } catch (err) {
    store.error = err.message
  } finally {
    loading.value = false
  }
}

async function exportCsv() {
  try {
    const blob = await reportsApi.export({ days: days.value })
    downloadBlob(blob, `治理日报_近${days.value}天.csv`)
  } catch (err) {
    store.error = err.message
  }
}

onMounted(load)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

/* 口径说明：常驻但压低视觉权重，不抢数字的注意力 */
.summary__note {
  margin: 0;
  padding: 7px 11px;
  background: var(--bg-panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-dim);
  font-size: 11.5px;
  line-height: 1.6;
}

.summary__note strong {
  color: var(--text-sub);
  font-weight: 600;
}

.summary__card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 9px 15px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  min-width: 118px;
}

.summary__label {
  font-size: 12px;
  color: var(--text-sub);
}

.summary__value {
  font-size: 19px;
  font-weight: 600;
  font-family: 'SF Mono', Consolas, monospace;
  color: var(--text-main);
}

.summary__unit {
  font-size: 12px;
  color: var(--text-sub);
  font-weight: 400;
  margin-left: 2px;
}

.summary__spacer {
  flex: 1;
}

.summary__actions {
  display: flex;
  gap: 8px;
}

.chart-row {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 12px;
  flex-shrink: 0;
}

.table-panel {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

/* 完成率进度条 */
.rate {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rate__track {
  flex: 1;
  height: 4px;
  background: var(--bg-panel-2);
  border-radius: 2px;
  overflow: hidden;
  min-width: 40px;
}

.rate__fill {
  height: 100%;
  background: var(--c-primary);
  border-radius: 2px;
}

.rate__val {
  font-size: 12px;
  color: var(--text-sub);
  font-family: 'SF Mono', Consolas, monospace;
  min-width: 34px;
  text-align: right;
}

@media (max-width: 1400px) {
  .summary { flex-wrap: wrap; }
  .chart-row { grid-template-columns: 1fr; }
}

@media (max-width: 900px) {
  .page {
    height: auto;
    min-height: 100%;
  }

  .summary {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .summary__card {
    min-width: 0;
    padding: 10px 12px;
  }

  .summary__value {
    font-size: 18px;
  }

  .summary__spacer {
    display: none;
  }

  .summary__actions {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 8px;
  }

  .summary__actions select,
  .summary__actions .btn {
    width: 100%;
  }

  .chart-row {
    grid-template-columns: 1fr;
  }

  .table-panel {
    flex: none;
    min-height: 420px;
    max-width: 100%;
    overflow: auto;
    -webkit-overflow-scrolling: touch;
  }

  .table-panel .data-table {
    min-width: 1040px;
  }
}

@media (max-width: 520px) {
  .summary__card {
    min-height: 74px;
  }

  .summary__actions {
    grid-template-columns: 1fr 1fr;
  }

  .summary__actions select {
    grid-column: 1 / -1;
  }
}
</style>

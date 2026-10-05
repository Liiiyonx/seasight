/** 格式化工具。 */

/** 时间：2026-09-18 01:23:45 */
export function fmtTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes(),
  )}:${p(d.getSeconds())}`
}

/** 短时间：09-18 01:23 */
export function fmtShortTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 相对时间：3 分钟前 */
export function fmtRelative(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)

  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 0) return '刚刚'
  if (diff < 60) return `${Math.floor(diff)} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`
  return fmtShortTime(value)
}

/** 坐标：119.6531, 26.3867 */
export function fmtCoord(lng, lat, digits = 4) {
  if (lng == null || lat == null) return '—'
  return `${Number(lng).toFixed(digits)}, ${Number(lat).toFixed(digits)}`
}

/** 数字千分位 */
export function fmtNumber(value, digits = 0) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** 置信度：0.9123 → 91.2% */
export function fmtConfidence(value) {
  if (value == null) return '—'
  return `${(Number(value) * 100).toFixed(1)}%`
}

/** 仓容占用：0.35 → 35% */
export function fmtUsage(value) {
  if (value == null) return '—'
  return `${(Number(value) * 100).toFixed(0)}%`
}

/** 电量颜色（低电量告警） */
export function batteryColor(pct) {
  const n = Number(pct ?? 100)
  if (n < 20) return '#ff3b30'
  if (n < 40) return '#ff9500'
  return '#34c759'
}

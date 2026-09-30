/**
 * 全局常量 —— 中文标签与配色集中在此。
 *
 * 为什么集中：后端枚举的英文值散落在各处时，
 * 一旦新增类别就会出现「有的页面显示中文、有的显示 foam」。
 */

// ---------- 垃圾类别 ----------
export const WASTE_CLASSES = {
  foam: { label: '泡沫类', color: '#ff9500', desc: 'EPS 泡沫浮球及碎片' },
  plastic: { label: '塑胶类', color: '#007aff', desc: '塑胶浮球、塑料瓶、塑料袋' },
  fishing_gear: { label: '渔具类', color: '#ff3b30', desc: '废旧渔网、绳索、饵料袋' },
  other: { label: '其他', color: '#8e8e93', desc: '木板、生活杂物、藻类聚集' },
}

export const CLASS_ORDER = ['foam', 'plastic', 'fishing_gear', 'other']

export function classLabel(code) {
  return WASTE_CLASSES[code]?.label || code || '未知'
}

export function classColor(code) {
  return WASTE_CLASSES[code]?.color || '#8e8e93'
}

// ---------- 事件状态 ----------
export const EVENT_STATUS = {
  new: '待处理',
  dispatched: '已派单',
  resolved: '已清理',
  ignored: '已忽略',
}

// ---------- 任务状态（与后端 TaskStatus.TRANSITIONS 对应） ----------
export const TASK_STATUS = {
  pending: '待派单',
  assigned: '已派单',
  navigating: '前往中',
  collecting: '作业中',
  done: '已完成',
  cancelled: '已取消',
}

/** 状态机合法迁移 —— 前端做按钮禁用，后端做最终校验 */
export const TASK_TRANSITIONS = {
  pending: ['assigned', 'cancelled'],
  assigned: ['navigating', 'pending', 'cancelled'],
  navigating: ['collecting', 'cancelled'],
  collecting: ['done', 'cancelled'],
  done: [],
  cancelled: [],
}

export function canTransition(current, target) {
  return (TASK_TRANSITIONS[current] || []).includes(target)
}

// ---------- 设备状态 ----------
export const DEVICE_STATUS = {
  online: '在线',
  offline: '离线',
  fault: '故障',
}

// ---------- 复核结果 ----------
export const REVIEW_RESULT = {
  pending: '待复核',
  confirmed: '确认清理',
  not_found: '到场未发现',
  recheck: '需人工复查',
}

// ---------- 设备类型 ----------
export const DEVICE_TYPE = {
  shore_camera: '岸基摄像头',
  drone: '无人机',
  robot: '水面机器人',
}

// ---------- 连江沿海乡镇（用于筛选与报表分组） ----------
export const TOWNSHIPS = [
  '马鼻镇',
  '黄岐镇',
  '筱埕镇',
  '苔菉镇',
  '安凯镇',
  '下宫镇',
]

// ---------- 地图中心（连江沿海） ----------
export const MAP_CENTER = { lng: 119.86, lat: 26.35, zoom: 11 }

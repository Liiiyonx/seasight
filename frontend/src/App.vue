<template>
  <RouterView v-if="isLoginPage" />
  <div v-else class="layout">
    <!-- 顶栏 -->
    <header class="layout__header">
      <BrandLockup
        class="brand"
        variant="header"
        :size="32"
        subtitle="海漂垃圾全链路智能治理平台"
      />

      <nav class="nav">
        <RouterLink
          v-for="r in navRoutes"
          :key="r.path"
          :to="r.path"
          class="nav__item"
          active-class="nav__item--active"
        >
          <span class="nav__icon">{{ r.meta.icon }}</span>
          <span>{{ r.meta.title }}</span>
        </RouterLink>
      </nav>

      <div class="header-right">
        <button
          class="caption-toggle"
          type="button"
          :class="{ 'caption-toggle--on': captionVisible }"
          :title="captionVisible ? '隐藏口径字幕' : '显示口径字幕（演示录屏时用）'"
          :aria-pressed="captionVisible"
          @click="toggleCaption()"
        >
          口径
        </button>

        <button
          class="theme-toggle"
          type="button"
          :title="theme === 'light' ? '切换到深色模式' : '切换到浅色模式'"
          :aria-label="theme === 'light' ? '切换到深色模式' : '切换到浅色模式'"
          @click="toggleThemeBtn"
        >
          <span aria-hidden="true">{{ theme === 'light' ? '☾' : '☀' }}</span>
        </button>

        <div ref="notifRef" class="notif">
          <button
            class="notif__bell"
            type="button"
            aria-label="打开通知中心"
            aria-haspopup="dialog"
            :aria-expanded="showNotif"
            aria-controls="seasight-notifications"
            @click="toggleNotif"
          >
            通知
            <span v-if="unreadCount" class="notif__badge">{{ unreadCount > 99 ? '99+' : unreadCount }}</span>
          </button>
          <div
            v-if="showNotif"
            id="seasight-notifications"
            class="notif__panel"
            role="dialog"
            aria-label="通知中心"
            @click.stop
          >
            <div class="notif__head">
              <span>通知中心</span>
              <button class="notif__read" @click="markAllRead">全部已读</button>
            </div>
            <div class="notif__list">
              <div
                v-for="n in notifications"
                :key="n._key"
                class="notif__item"
              >
                <span class="notif__dot" :class="n.type === 'event' ? 'notif__dot--event' : 'notif__dot--task'"></span>
                <div class="notif__body">
                  <div class="notif__title">{{ n.title }}</div>
                  <div class="notif__time">{{ fmtTime(n.time) }}</div>
                </div>
              </div>
              <div v-if="!notifications.length" class="notif__empty">暂无通知</div>
            </div>
          </div>
        </div>

        <span v-if="currentUser" class="user">
          <span class="user__name">{{ currentUser.full_name || currentUser.username }}</span>
          <span class="user__role">{{ roleLabel(currentUser.role) }}</span>
          <button class="user__logout" type="button" @click="logout">退出</button>
        </span>
        <span
          class="conn"
          :class="store.wsConnected ? 'conn--on' : 'conn--off'"
          :title="store.wsConnected ? '实时连接正常' : '实时连接已断开'"
        >
          <i class="conn__dot"></i>
          <span class="conn__label">{{ store.wsConnected ? '实时已连接' : '实时已断开' }}</span>
        </span>
        <span class="clock">{{ clock }}</span>
      </div>
    </header>

    <!-- 内容 -->
    <main class="layout__main">
      <RouterView v-slot="{ Component }">
        <transition name="fade-slide" mode="out-in">
          <component :is="Component" />
        </transition>
      </RouterView>
    </main>

    <!-- 全局错误条
         ★ 只在**消费 realtime 数据的页面**显示（route.meta.realtime，见 router/index.js）。
           为什么：这条错误来自 App 外壳的 30 秒兜底轮询（stats/devices/robots/events），
           而 /vision-compare 这类页面根本不读这些数据 —— 在它上面挂一条
           "服务异常，请稍后重试"，评委看到的是一条与当前画面无关的红色报错。
           链路状态并没有被隐藏：右下角字幕仍显示「实时已断开 / 未自检」，
           大屏等消费页也照常显示这条错误条。 -->
    <div v-if="store.error && route.meta?.realtime" class="error-bar">
      <span>{{ store.error }}</span>
      <button @click="store.refreshAll()">重试</button>
      <button class="error-bar__close" @click="store.error = ''">×</button>
    </div>

    <!-- 演示口径字幕（固定右下角；开关状态全局共享，见 utils/demoCaption.js） -->
    <DemoCaption />

    <!-- 移动端底部 Tab（≤620px 显示，桌面演示不受影响）：
         9 个导航项在手机上塞不下，取 4 个高频页 +「更多」收纳其余，
         顶栏导航在手机上整个隐藏，顶栏从两行 97px 回到单行。 -->
    <nav class="tabbar" aria-label="底部导航">
      <RouterLink
        v-for="r in tabbarRoutes"
        :key="r.path"
        :to="r.path"
        class="tabbar__item"
        active-class="tabbar__item--active"
      >
        <span class="tabbar__icon" aria-hidden="true">{{ r.meta.icon }}</span>
        <span class="tabbar__label">{{ r.meta.title }}</span>
      </RouterLink>
      <button
        class="tabbar__item"
        type="button"
        :aria-expanded="showMore"
        aria-controls="seasight-more-sheet"
        @click="showMore = !showMore"
      >
        <span class="tabbar__icon" aria-hidden="true">⋯</span>
        <span class="tabbar__label">更多</span>
      </button>
    </nav>

    <!-- 「更多」底部抽屉：挂 body 避开 .layout__main 层叠上下文（见下方样式注释） -->
    <Teleport to="body">
      <Transition name="sheet-up">
        <div v-if="showMore" class="more-sheet" role="dialog" aria-label="更多页面">
          <div class="more-sheet__grid">
            <RouterLink
              v-for="r in moreRoutes"
              :key="r.path"
              :to="r.path"
              class="more-sheet__item"
              active-class="more-sheet__item--active"
              @click="showMore = false"
            >
              <span class="more-sheet__icon" aria-hidden="true">{{ r.meta.icon }}</span>
              <span>{{ r.meta.title }}</span>
            </RouterLink>
          </div>
          <button class="more-sheet__close" type="button" @click="showMore = false">关闭</button>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { RouterView, RouterLink, useRoute, useRouter } from 'vue-router'
import { routes } from '@/router'
import { useRealtimeStore } from '@/stores/realtime'
import { getUser, clearAuth, isLoggedIn, roleLabel } from '@/utils/auth'
import { statsApi } from '@/api'
import { fmtTime } from '@/utils/format'
import { getTheme, toggleTheme } from '@/utils/theme'
import { captionVisible, toggleCaption } from '@/utils/demoCaption'
import BrandLockup from '@/components/BrandLockup.vue'
import DemoCaption from '@/components/DemoCaption.vue'

const store = useRealtimeStore()
const route = useRoute()
const router = useRouter()
const navRoutes = routes.filter(
  (r) => r.meta?.title && !r.meta?.noLayout && !r.meta?.hideInNav,
)

// 移动端底部 Tab：4 个高频页直达，其余收进「更多」抽屉
const TABBAR_PATHS = ['/dashboard', '/events', '/tasks', '/assistant']
const tabbarRoutes = navRoutes.filter((r) => TABBAR_PATHS.includes(r.path))
const moreRoutes = navRoutes.filter((r) => !TABBAR_PATHS.includes(r.path))
const showMore = ref(false)

// 登录页不渲染主布局（无顶栏/导航）
const isLoginPage = computed(() => route.name === 'login')

const currentUser = ref(getUser())

function logout() {
  clearAuth()
  currentUser.value = null
  router.replace('/login')
}

// 登录页与主布局由同一个 App 实例承载，登录成功不会重新挂载组件。
// 路由切回业务页时重新读取本地身份，确保顶栏立即出现账号与退出入口。
watch(
  () => route.name,
  () => {
    currentUser.value = getUser()
  },
)

// ---------- 主题切换 ----------
const theme = ref(getTheme())

function toggleThemeBtn() {
  theme.value = toggleTheme()
}

// ---------- 站内通知铃铛 ----------
const NOTIF_KEY = 'seasight_last_read'
const notifications = ref([])
const showNotif = ref(false)
const unreadCount = ref(0)
const notifRef = ref(null)
// 通知行的渲染 key：模块级自增序号。不用 (event_id||task_id)+time 拼——
// 同一事件/任务在同一时刻可能刷出多条（重试、合并上报），拼出来会撞 key
let notifSeq = 0

function lastReadAt() {
  return Number(localStorage.getItem(NOTIF_KEY) || 0)
}

async function loadNotifications() {
  try {
    const items = await statsApi.notifications({ hours: 24, limit: 50 })
    notifications.value = (items || []).map((n) => ({ ...n, _key: ++notifSeq }))
    const last = lastReadAt()
    unreadCount.value = notifications.value.filter(
      (n) => new Date(n.time || 0).getTime() > last,
    ).length
  } catch {
    /* 通知加载失败不阻断主流程 */
  }
}

function toggleNotif() {
  showNotif.value = !showNotif.value
  if (showNotif.value) loadNotifications()
}

function closeNotif() {
  showNotif.value = false
}

function markAllRead() {
  const latest = notifications.value[0]?.time
  if (latest) {
    localStorage.setItem(NOTIF_KEY, String(new Date(latest).getTime()))
    unreadCount.value = 0
  }
}

function onDocumentPointerDown(event) {
  if (!showNotif.value) return
  if (!notifRef.value?.contains(event.target)) closeNotif()
}

function onDocumentKeydown(event) {
  if (event.key === 'Escape') {
    closeNotif()
    showMore.value = false
  }
}

// 从「更多」抽屉跳转后收起面板
watch(
  () => route.name,
  () => {
    showMore.value = false
  },
)

// ---------- 时钟 ----------
const clock = ref('')
let clockTimer = null

function tickClock() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  clock.value = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes(),
  )}:${p(d.getSeconds())}`
}

// 每 30 秒静默刷新一次兜底数据。
// 为什么需要：WebSocket 只推增量，若某条消息丢了大屏就会一直少数据。
let pollTimer = null
let notifTimer = null
let dataServicesStarted = false

function startDataServices() {
  if (dataServicesStarted || !isLoggedIn()) return
  dataServicesStarted = true
  store.startRealtime()
  store.refreshAll()
  loadNotifications()
  pollTimer = setInterval(() => store.refreshAll(), 30000)
  notifTimer = setInterval(loadNotifications, 30000)
}

function stopDataServices() {
  if (!dataServicesStarted) return
  dataServicesStarted = false
  clearInterval(pollTimer)
  clearInterval(notifTimer)
  pollTimer = null
  notifTimer = null
  store.stopRealtime()
}

onMounted(() => {
  tickClock()
  clockTimer = setInterval(tickClock, 1000)
  document.addEventListener('pointerdown', onDocumentPointerDown)
  document.addEventListener('keydown', onDocumentKeydown)

  // 登录页不建立需要鉴权的实时连接，避免未登录时产生 403。
  if (!isLoginPage.value) startDataServices()
})

watch(isLoginPage, (loginPage) => {
  if (loginPage) stopDataServices()
  else startDataServices()
})

onUnmounted(() => {
  clearInterval(clockTimer)
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  document.removeEventListener('keydown', onDocumentKeydown)
  stopDataServices()
})
</script>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: transparent;
  position: relative;
  isolation: isolate;
}

/* ---------- 顶栏（玻璃拟态；外观见下方「iOS 风格覆写」段） ---------- */
.layout__header {
  position: relative;
  z-index: 300;
  overflow: visible;
  flex-shrink: 0;
  display: flex;
  align-items: center;
}

.brand {
  flex-shrink: 0;
}

.nav {
  display: flex;
  gap: 3px;
  flex: 1;
}

.nav__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 5px 13px;
  font-size: 13px;
  color: var(--text-sub);
  text-decoration: none;
  border-radius: var(--radius);
  transition: color 0.2s var(--ease), background 0.2s var(--ease);
  white-space: nowrap;
}

/* 导航下划线动效 */
.nav__item::after {
  content: '';
  position: absolute;
  left: 50%;
  bottom: 1px;
  width: 0;
  height: 2px;
  border-radius: 1px;
  background: var(--grad-primary);
  transform: translateX(-50%);
  transition: width 0.25s var(--ease);
}

.nav__item:hover {
  color: var(--text-main);
  background: var(--bg-hover);
}

.nav__item:hover::after,
.nav__item--active::after {
  width: 56%;
}

/* .nav__item--active 的颜色/背景由下方 iOS 覆写段统一管理 */

.nav__icon {
  font-size: 12px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}

.conn {
  display: flex;
  align-items: center;
  gap: 5px;
}

.conn__dot {
  border-radius: 50%;
  background: currentColor;
}

.conn--on .conn__dot {
  animation: blink 2s infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.clock {
  font-family: 'SF Mono', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.user {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.user__name {
  color: var(--text-main);
}

/* .user__role 外观由 iOS 覆写段管理 */

.user__logout {
  font-size: 11px;
  cursor: pointer;
}

.user__logout:hover {
  color: var(--text-main);
}

/* ---------- 口径字幕开关 ---------- */
.caption-toggle {
  min-height: 28px;
  padding: 3px 10px;
  font-size: 12px;
  color: var(--text-sub);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  transition: color 0.2s var(--ease), border-color 0.2s var(--ease);
}

.caption-toggle:hover {
  color: var(--c-primary);
  border-color: var(--c-primary-dim);
}

.caption-toggle--on {
  color: var(--c-primary);
  border-color: color-mix(in srgb, var(--c-primary) 42%, transparent);
  background: color-mix(in srgb, var(--c-primary) 10%, transparent);
}

/* ---------- 主题切换 ---------- */
.theme-toggle {
  width: 30px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  color: var(--text-sub);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  transition: color 0.2s var(--ease), border-color 0.2s var(--ease);
}

.theme-toggle:hover {
  color: var(--c-primary);
  border-color: var(--c-primary-dim);
}

/* ---------- 通知铃铛 ---------- */
.notif {
  position: relative;
  z-index: 340;
}

.notif__bell {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  font-size: 12px;
  color: var(--text-sub);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 3px;
  cursor: pointer;
}

.notif__bell:hover {
  color: var(--text-main);
  border-color: var(--border-bright);
}

.notif__badge {
  text-align: center;
  color: #fff;
}

/* .notif__panel 外观由 iOS 覆写段管理（定位锚定在 .notif 上） */
.notif__panel {
  position: absolute;
  right: 0;
  display: flex;
  flex-direction: column;
  z-index: 360;
  overflow: hidden;
}

.notif__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  font-size: 13px;
  color: var(--text-main);
  border-bottom: 1px solid var(--border);
}

.notif__read {
  font-size: 12px;
  background: none;
  border: none;
  cursor: pointer;
}

.notif__list {
  overflow-y: auto;
}

.notif__item {
  display: flex;
}

.notif__item:hover {
  background: var(--bg-hover);
}

.notif__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}

.notif__dot--event {
  background: var(--c-warn);
}

.notif__dot--task {
  background: var(--c-primary);
}

.notif__body {
  min-width: 0;
}

.notif__title {
  color: var(--text-main);
  line-height: 1.45;
}

.notif__time {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 2px;
}

.notif__empty {
  padding: 24px 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-dim);
}

/* ---------- 主体 ---------- */
/* ★ 这个 z-index:1 会让 .layout__main 成为一个层叠上下文：
   它的所有后代（含弹层/抽屉）都只能在这个层里比大小，
   永远越不过兄弟节点 .layout__header 的 z-index:300。
   所以页面内的浮层必须用 <Teleport to="body"> 挂出去（见 TasksView 的抽屉），
   不要靠调高自己的 z-index 来解决被遮挡的问题。 */
.layout__main {
  position: relative;
  z-index: 1;
  flex: 1;
  min-height: 0;
  overflow: auto;
}

/* ---------- 错误条（外观由 iOS 覆写段管理） ---------- */
.error-bar {
  position: fixed;
  left: 50%;
  bottom: 18px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  z-index: 100;
}

.error-bar button {
  font-size: 12px;
  cursor: pointer;
}

.error-bar button:hover {
  background: var(--bg-active);
}

.error-bar__close {
  padding: 2px 7px !important;
}

/* ---------- iOS 风格覆写：布局、顶栏与移动端导航 ---------- */
.layout {
  background: var(--bg-page);
}

.layout__header {
  min-height: 64px;
  height: auto;
  padding: 8px 18px;
  gap: 18px;
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent);
  backdrop-filter: blur(20px) saturate(1.35);
  -webkit-backdrop-filter: blur(20px) saturate(1.35);
  border-bottom: 1px solid var(--separator);
  box-shadow: 0 1px 0 rgba(0, 0, 0, 0.02);
}

.brand {
  min-width: 0;
}

.nav {
  align-items: center;
  justify-content: center;
  gap: 2px;
  min-width: 0;
}

.nav__item {
  min-height: 38px;
  padding: 8px 12px;
  border-radius: 10px;
  color: var(--text-sub);
  font-size: 13px;
  font-weight: 500;
}

.nav__item::after {
  display: none;
}

.nav__item:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.nav__item--active,
.nav__item--active:hover {
  background: var(--bg-active);
  color: var(--c-primary);
}

.nav__icon {
  font-size: 12px;
  opacity: 0.9;
}

.header-right {
  gap: 8px;
  min-width: 0;
}

.theme-toggle {
  width: 36px;
  height: 36px;
  border: 0;
  border-radius: 50%;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  font-size: 16px;
}

.caption-toggle {
  min-height: 36px;
  padding: 6px 12px;
  border: 0;
  border-radius: 10px;
  background: var(--bg-panel-2);
  color: var(--text-main);
  font-size: 13px;
}

.caption-toggle:hover {
  border: 0;
  background: var(--bg-hover);
  color: var(--c-primary);
}

.caption-toggle--on {
  border: 0;
  background: var(--bg-active);
  color: var(--c-primary);
}

.theme-toggle:hover {
  border: 0;
  background: var(--bg-hover);
  color: var(--c-primary);
}

.notif__bell {
  min-height: 36px;
  padding: 6px 11px;
  border: 0;
  border-radius: 10px;
  background: var(--bg-panel-2);
  color: var(--text-main);
  font-size: 13px;
}

.notif__bell:hover {
  border: 0;
  background: var(--bg-hover);
}

.notif__badge {
  min-width: 17px;
  height: 17px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--c-danger);
  font-size: 10.5px;
  line-height: 17px;
}

.notif__panel {
  top: calc(100% + 8px);
  width: 350px;
  max-height: min(520px, 72vh);
  border: 1px solid var(--panel-border);
  border-radius: 14px;
  background: var(--bg-panel);
  box-shadow:
    0 22px 60px rgba(0, 0, 0, 0.2),
    0 2px 8px rgba(0, 0, 0, 0.08);
}

.notif__head {
  min-height: 48px;
  padding: 12px 15px;
  border-bottom-color: var(--separator);
  font-weight: 600;
}

.notif__read {
  color: var(--c-primary);
  font-weight: 500;
}

.notif__list {
  padding: 5px 0;
}

.notif__item {
  gap: 10px;
  padding: 10px 15px;
}

.notif__dot--event { background: var(--c-warn); }
.notif__dot--task { background: var(--c-primary); }
.notif__title { font-size: 13px; }

.user {
  gap: 7px;
}

.user__name {
  font-weight: 500;
}

.user__role {
  padding: 2px 8px;
  border: 0;
  border-radius: 999px;
  background: var(--bg-active);
  color: var(--c-primary);
  font-size: 11px;
}

.user__logout {
  min-height: 34px;
  padding: 5px 10px;
  border: 0;
  border-radius: 9px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
}

.user__logout:hover {
  border: 0;
  background: var(--bg-hover);
}

.conn {
  min-height: 32px;
  padding: 0 9px;
  border-radius: 999px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  font-size: 12px;
}

.conn--on { color: var(--c-success); }
.conn--off { color: var(--c-danger); }
.conn__dot { width: 7px; height: 7px; }

.clock {
  color: var(--text-dim);
  font-size: 11px;
}

.layout__main {
  padding: 16px;
  overscroll-behavior: contain;
}

.error-bar {
  max-width: min(560px, calc(100vw - 32px));
  padding: 10px 14px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--bg-panel) 94%, var(--c-danger));
  border: 1px solid color-mix(in srgb, var(--c-danger) 42%, transparent);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.16);
  color: var(--text-main);
}

.error-bar button {
  min-height: 30px;
  padding: 4px 10px;
  border: 0;
  border-radius: 8px;
  background: var(--bg-hover);
  color: var(--text-main);
}

/* ★ 顶栏溢出修复：9 个导航项约 890px，加上品牌区与右侧控件组，
   <1650px（1366/1440/1536 笔记本）必然放不下 —— 之前内容直接溢出容器，
   激活胶囊压住品牌字标、「双通道对照」被右侧按钮遮挡。
   方案：允许导航横滑（safe center：未溢出时保持居中，溢出时退化为
   左对齐且左端可滚回），右缘 24px 渐隐提示可滑，时钟提前隐藏释放宽度。 */
@media (max-width: 1650px) {
  .brand :deep(.brand-lockup__sub) {
    display: none;
  }

  .clock {
    display: none;
  }

  .nav {
    justify-content: flex-start;
    justify-content: safe center;
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scrollbar-width: none;
    padding-right: 24px;
    -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 24px), transparent 100%);
    mask-image: linear-gradient(to right, #000 calc(100% - 24px), transparent 100%);
  }

  .nav::-webkit-scrollbar {
    display: none;
  }
}

@media (max-width: 1280px) {
  .clock {
    display: none;
  }

  .layout__header {
    gap: 12px;
  }

  .nav {
    justify-content: flex-start;
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scrollbar-width: none;
  }

  .nav::-webkit-scrollbar {
    display: none;
  }

  .nav__item {
    padding-inline: 10px;
  }
}

@media (max-width: 1080px) {
  .user__role {
    display: none;
  }

  .conn__label {
    display: none;
  }

  .conn {
    width: 34px;
    justify-content: center;
    padding: 0;
  }
}

@media (max-width: 900px) {
  .layout__header {
    flex-wrap: wrap;
    gap: 6px 10px;
    padding: 8px 12px 0;
  }

  .brand {
    flex: 1;
    min-width: 0;
  }

  .header-right {
    margin-left: auto;
    gap: 6px;
  }

  .nav {
    order: 3;
    flex: 1 0 100%;
    width: calc(100% + 24px);
    margin: 0 -12px;
    padding: 0 12px 8px;
    justify-content: flex-start;
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scrollbar-width: none;
  }

  .nav::-webkit-scrollbar {
    display: none;
  }

  .nav__item {
    flex: 0 0 auto;
    min-height: 36px;
    padding: 7px 12px;
    background: var(--bg-panel-2);
  }

  .nav__item--active,
  .nav__item--active:hover {
    background: var(--c-primary);
    color: #ffffff;
  }

  .notif__panel {
    position: fixed;
    top: 110px;
    right: 12px;
    left: 12px;
    width: auto;
    max-height: min(72dvh, 560px);
    overscroll-behavior: contain;
  }

  .layout__main {
    padding: 12px;
    padding-bottom: calc(16px + env(safe-area-inset-bottom, 0px));
  }
}

/* ---------- 移动端底部 Tab（默认隐藏，≤620px 显示） ---------- */
.tabbar {
  display: none;
}

.tabbar__item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  min-width: 58px;
  padding: 5px 10px;
  border-radius: 12px;
  color: var(--text-sub);
  font-size: 10.5px;
  line-height: 1.2;
  text-decoration: none;
  background: transparent;
  border: 0;
  cursor: pointer;
  transition: color 0.2s var(--ease);
}

.tabbar__icon {
  font-size: 17px;
  line-height: 1;
}

.tabbar__item--active,
.tabbar__item--active:hover {
  color: var(--c-primary);
}

/* 「更多」底部抽屉（Teleport 到 body，z-index 高于顶栏 300 / 通知面板 360） */
.more-sheet {
  position: fixed;
  left: 12px;
  right: 12px;
  bottom: calc(12px + env(safe-area-inset-bottom, 0px));
  z-index: 410;
  padding: 16px;
  background: var(--bg-panel);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-lg);
  box-shadow: 0 22px 60px rgba(0, 0, 0, 0.2), 0 2px 8px rgba(0, 0, 0, 0.08);
}

.more-sheet__grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}

.more-sheet__item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 12px 4px;
  border-radius: 12px;
  background: var(--bg-panel-2);
  color: var(--text-sub);
  font-size: 12px;
  text-decoration: none;
  transition: color 0.2s var(--ease), background 0.2s var(--ease);
}

.more-sheet__item--active,
.more-sheet__item--active:hover {
  color: var(--c-primary);
  background: var(--bg-active);
}

.more-sheet__icon {
  font-size: 18px;
  line-height: 1;
}

.more-sheet__close {
  width: 100%;
  min-height: 42px;
  margin-top: 12px;
  border: 0;
  border-radius: 12px;
  background: var(--bg-hover);
  color: var(--text-main);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}

.sheet-up-enter-active,
.sheet-up-leave-active {
  transition: transform 0.25s var(--ease), opacity 0.25s var(--ease);
}

.sheet-up-enter-from,
.sheet-up-leave-to {
  transform: translateY(28px);
  opacity: 0;
}

@media (max-width: 620px) {
  .clock,
  .user__name,
  .user__role {
    display: none;
  }

  /* 底部 Tab 接管导航后，顶栏导航隐藏，顶栏回归单行 */
  .nav {
    display: none;
  }

  .layout__header {
    flex-wrap: nowrap;
  }

  .theme-toggle,
  .caption-toggle,
  .notif__bell,
  .user__logout {
    min-width: 38px;
    min-height: 38px;
  }

  .notif__bell {
    padding: 0 9px;
  }

  .user__logout {
    padding: 0 10px;
  }

  .conn {
    display: none;
  }

  .tabbar {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 320;
    display: flex;
    justify-content: space-around;
    padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px));
    background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
    backdrop-filter: blur(20px) saturate(1.35);
    -webkit-backdrop-filter: blur(20px) saturate(1.35);
    border-top: 1px solid var(--separator);
  }

  /* 顶栏单行后通知面板跟着上移 */
  .notif__panel {
    position: fixed;
    top: 64px;
    right: 12px;
    left: 12px;
    width: auto;
    max-height: min(72dvh, 560px);
    overscroll-behavior: contain;
  }

  /* 给底部 Tab 留出空间 */
  .layout__main {
    padding-bottom: calc(76px + env(safe-area-inset-bottom, 0px));
  }

  .error-bar {
    right: 12px;
    bottom: calc(84px + env(safe-area-inset-bottom, 0px));
    left: 12px;
    transform: none;
    flex-wrap: wrap;
  }
}
</style>

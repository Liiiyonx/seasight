import { createRouter, createWebHistory } from 'vue-router'
import { isLoggedIn } from '@/utils/auth'

const routes = [
  { path: '/', redirect: '/login' },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', noLayout: true },
  },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { title: '监测大屏', icon: '◈', realtime: true },
  },
  {
    path: '/events',
    name: 'events',
    component: () => import('@/views/EventsView.vue'),
    meta: { title: '事件中心', icon: '◉', realtime: true },
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('@/views/TasksView.vue'),
    meta: { title: '工单看板', icon: '▤', realtime: true },
  },
  {
    path: '/simulation/:taskId',
    name: 'simulation',
    component: () => import('@/views/SimulationView.vue'),
    meta: { title: '工单执行仿真', hideInNav: true, realtime: true },
  },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('@/views/DevicesView.vue'),
    meta: { title: '设备态势', icon: '◎', realtime: true },
  },
  {
    path: '/reports',
    name: 'reports',
    component: () => import('@/views/ReportsView.vue'),
    meta: { title: '治理报表', icon: '▦', realtime: true },
  },
  {
    path: '/assistant',
    name: 'assistant',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '智能助手', icon: '✦' },
    // 支持 query 直达：?session=<session_id> 自动打开指定会话
    props: (route) => ({
      sessionId: typeof route.query.session === 'string' ? route.query.session : '',
    }),
  },
  {
    path: '/agents',
    name: 'agents',
    component: () => import('@/views/AgentsView.vue'),
    // 智能体控制台：轨迹回放 / 审批队列 / 工具目录 / 评测雷达 / 经验库都在这页。
    // 它一度被 redirect 到 /assistant（智能助手），但对话页只承接了「发起一次
    // 派单」这个入口，轨迹回放、审批队列、评测产物这些能力并没有搬过去 ——
    // 而智能体赛道的答辩要看的恰恰是这几样。所以恢复成独立路由。
    // 支持 query 直达：?run=<run_id> 自动展开运行详情，?page=<n> 直达页码。
    meta: { title: '智能体控制台', icon: '⚙', realtime: true },
    props: (route) => ({
      runId: typeof route.query.run === 'string' ? route.query.run : '',
      page: Number(route.query.page) > 0 ? Number(route.query.page) : 1,
    }),
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
    meta: { title: '知识智能体', icon: '◇' },
  },
  {
    path: '/analyze',
    // ★ name 必须保留：KnowledgeView 的「视觉实测」样本图用
    //   router.push({ name: 'analyze', query: { sample } }) 跳过来。
    //   之前这条路由只有 redirect、没有 name，vue-router 找不到目标路由会
    //   直接抛错并中止导航 —— 表现就是「点样本图毫无反应」，且只在控制台
    //   留一条没有 message 的 Error。
    name: 'analyze',
    // 手动上传分析页已并入「智能助手」对话页（拖图即检）；保留 redirect 以免旧链接 404。
    // ★ 用函数式 redirect 而不是字符串：字符串 redirect 会丢掉 query，
    //   而 ?sample= 正是「视觉实测 → 图片分析」这条链路的唯一参数载体。
    redirect: (to) => ({ path: '/assistant', query: to.query }),
  },
  {
    // 双通道对照（定性）。为什么单独一页：助手页里检测结果是一条"消息"，
    // 没地方放"同一张图两条通道并排 + 各自适用条件 + 置信度不可横向比较"
    // 这组说明 —— 而这组说明恰恰是本页的全部价值。
    path: '/vision-compare',
    name: 'vision-compare',
    component: () => import('@/views/VisionCompareView.vue'),
    meta: { title: '双通道对照', icon: '⧉' },
    // 支持 ?sample=<id> 直达：从「知识智能体 · 视觉实测」或旧 /analyze 链接带过来
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

// ---------- 路由守卫：未登录一律回登录页 ----------
router.beforeEach((to) => {
  const isLoginPage = to.name === 'login'
  if (!isLoginPage && !isLoggedIn()) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta?.title
  document.title = title ? `${title} · 探海灵眸 SeaSight` : '探海灵眸 SeaSight'
})

export default router
export { routes }

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import './assets/main.css'
import { initTheme } from './utils/theme'
import { useRealtimeStore } from './stores/realtime'

initTheme()

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.mount('#app')

// 调试钩子：让浏览器控制台能直接访问 pinia store。
// ★ 仅开发环境挂载。生产构建里 import.meta.env.DEV 是常量 false，
//   整段会被摇树移除 —— 遥测是设备数据，不该在生产环境暴露到全局。
if (import.meta.env.DEV) {
  window.__oceanusStore = useRealtimeStore(pinia)
}

// PWA：仅生产注册 Service Worker（dev 下缓存热更新产物会捣乱）
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`).catch(() => {
      /* 注册失败不影响页面本身 —— PWA 是增强项 */
    })
  })
}

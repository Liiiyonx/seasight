/**
 * Oceanus Service Worker（PWA 离线壳）
 *
 * 策略刻意保守：
 * - /api/、/stream/、WebSocket 一律放行 —— 实时数据永远走网络，
 *   断网时页面自身的降级提示（"实时已断开"）比缓存的假数据诚实；
 * - 带 hash 的 /assets/ 构建产物 cache-first（immutable，二次进入零请求）；
 * - SPA 导航 network-first，断网时回退到已缓存的 index.html，
 *   让"添加到主屏"后的启动在弱网/离线也能出壳。
 */

const CACHE = 'seasight-shell-v1'
const ASSETS_CACHE = 'seasight-assets-v1'

self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(
        keys
          .filter((k) => k.startsWith('seasight-') && k !== CACHE && k !== ASSETS_CACHE)
          .map((k) => caches.delete(k)),
      )
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return

  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return
  // API / 视频流：永不拦截
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/stream/')) return

  // 带 hash 的构建产物：cache-first
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      (async () => {
        const cached = await caches.match(req)
        if (cached) return cached
        const res = await fetch(req)
        if (res.ok) {
          const cache = await caches.open(ASSETS_CACHE)
          cache.put(req, res.clone())
        }
        return res
      })(),
    )
    return
  }

  // SPA 导航：network-first，断网回退缓存的壳
  if (req.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const res = await fetch(req)
          if (res.ok) {
            const cache = await caches.open(CACHE)
            cache.put('/', res.clone())
          }
          return res
        } catch {
          const cached =
            (await caches.match(req)) || (await caches.match('/')) || Response.error()
          return cached
        }
      })(),
    )
  }
})

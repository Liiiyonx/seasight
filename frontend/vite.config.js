import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backendHttp = env.VITE_BACKEND_URL || 'http://localhost:8000'
  const streamHttp = env.VITE_STREAM_URL || 'http://localhost:1984'
  const rawBasePath = env.VITE_BASE_PATH || '/'
  const basePath =
    rawBasePath === '/'
      ? '/'
      : `/${rawBasePath.replace(/^\/+|\/+$/g, '')}/`

  return {
    base: basePath,
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      host: true,
      proxy: {
        // 后端 API + WebSocket（真实路径是 /api/v1/ws/alerts）
        '/api': {
          target: backendHttp,
          changeOrigin: true,
          ws: true,
        },
        // go2rtc 视频流；开发服务器与生产 Nginx 保持同源路径
        '/stream': {
          target: streamHttp,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/stream/, ''),
          ws: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      chunkSizeWarningLimit: 1200,
      rollupOptions: {
        output: {
          manualChunks: {
            echarts: ['echarts'],
            vendor: ['vue', 'vue-router', 'pinia', 'axios'],
          },
        },
      },
    },
  }
})

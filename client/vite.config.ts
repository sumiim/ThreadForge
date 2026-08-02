import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端地址：以 api-server 实际启动端口为准，可通过环境变量覆盖
const API_PROXY_TARGET = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // REST 与 SSE 统一走 /api 前缀
      '/api': {
        target: API_PROXY_TARGET,
        changeOrigin: true,
        configure(proxy) {
          // 后端若经 nginx 等反向代理，SSE 事件流需关闭缓冲；本地直连 uvicorn 无此问题
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Accel-Buffering', 'no')
          })
        },
      },
    },
  },
})

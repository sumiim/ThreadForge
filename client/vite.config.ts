import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import electron from 'vite-plugin-electron/simple'

// 后端地址:以 api-server 实际启动端口为准,可通过环境变量覆盖
const API_PROXY_TARGET = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'

// 桌面壳(Electron)仅在 ELECTRON=1 时启用:
// - pnpm dev / pnpm build 保持纯 web,不弹窗口、不产出桌面文件
// - 桌面构建用相对 base,否则打包后 file:// 加载资源会 404
const isElectron = process.env.ELECTRON === '1'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    ...(isElectron
      ? [
          electron({
            main: { entry: 'electron/main.ts' },
            preload: { input: 'electron/preload.ts' },
          }),
        ]
      : []),
  ],
  base: isElectron ? './' : '/',
  server: {
    port: 5173,
    proxy: {
      // REST 与 SSE 统一走 /api 前缀
      '/api': {
        target: API_PROXY_TARGET,
        changeOrigin: true,
        configure(proxy) {
          // 后端若经 nginx 等反向代理,SSE 事件流需关闭缓冲;本地直连 uvicorn 无此问题
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Accel-Buffering', 'no')
          })
        },
      },
    },
  },
})

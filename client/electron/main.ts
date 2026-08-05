import { app, BrowserWindow, shell } from 'electron'
import path from 'node:path'

// vite-plugin-electron 开发模式下注入;生产模式加载打包产物
const devServerUrl = process.env.VITE_DEV_SERVER_URL

function resolveWebUrl(): string | undefined {
  const candidate = process.env.THREADFORGE_WEB_URL
  if (!candidate) return undefined
  try {
    const url = new URL(candidate)
    const loopback = ['127.0.0.1', '::1', 'localhost'].includes(url.hostname)
    const safeTransport = url.protocol === 'https:' || (url.protocol === 'http:' && loopback)
    if (!safeTransport || url.username || url.password || url.search || url.hash) return undefined
    return url.toString()
  } catch {
    return undefined
  }
}

const configuredWebUrl = resolveWebUrl()
if (configuredWebUrl && !process.env.THREADFORGE_API_BASE_URL) {
  process.env.THREADFORGE_API_BASE_URL = new URL(configuredWebUrl).origin
}

function openExternalUrl(url: string) {
  if (
    url.startsWith('https://') ||
    url.startsWith('mailto:') ||
    url === 'threadforge://worker/start'
  ) {
    void shell.openExternal(url)
  }
}

function createMainWindow() {
  const win = new BrowserWindow({
    title: 'ThreadForge Console',
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    backgroundColor: '#fafaf9', // 与 theme.ts colorBgLayout 一致,避免加载时的白闪
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(import.meta.dirname, 'preload.mjs'),
      contextIsolation: true, // 渲染进程无 Node,仅经 preload 的 contextBridge 桥接
      nodeIntegration: false,
      sandbox: false, // preload 为 ESM 产物,sandbox 模式不支持 ESM preload
    },
  })

  // 外链(https/mailto)交给系统浏览器,不在应用内开新窗口
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternalUrl(url)
    return { action: 'deny' }
  })

  // Markdown 等渲染内容也可能使用当前窗口导航。阻止离开已加载的
  // ThreadForge 页面，避免远程内容继续运行在桌面应用的 webContents 中。
  win.webContents.on('will-navigate', (event, url) => {
    if (url === win.webContents.getURL()) return
    if (configuredWebUrl) {
      const target = new URL(url)
      const appOrigin = new URL(configuredWebUrl).origin
      const current = new URL(win.webContents.getURL())
      const githubOAuth =
        target.origin === 'https://github.com' &&
        (current.origin === 'https://github.com' ||
          (current.origin === appOrigin && current.pathname === '/api/v1/auth/github/start'))
      if (target.origin === appOrigin || githubOAuth) return
    }
    event.preventDefault()
    openExternalUrl(url)
  })

  if (devServerUrl) {
    void win.loadURL(devServerUrl)
  } else if (configuredWebUrl) {
    void win.loadURL(configuredWebUrl)
  } else {
    void win.loadFile(path.join(import.meta.dirname, '../dist/index.html'))
  }
}

app.whenReady().then(() => {
  createMainWindow()

  // macOS 惯例:点击 Dock 图标且无窗口时重建窗口
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow()
  })
})

// 单用户本机工具,无托盘常驻需求:Windows/Linux 全部窗口关闭即退出
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

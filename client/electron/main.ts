import { app, BrowserWindow, shell } from 'electron'
import path from 'node:path'

// vite-plugin-electron 开发模式下注入;生产模式加载打包产物
const devServerUrl = process.env.VITE_DEV_SERVER_URL

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
    if (url.startsWith('https://') || url.startsWith('mailto:')) {
      void shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  if (devServerUrl) {
    void win.loadURL(devServerUrl)
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

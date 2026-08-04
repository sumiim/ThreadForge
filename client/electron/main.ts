import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import { execFile as execFileCallback, spawn, type ChildProcess } from 'node:child_process'
import { promisify } from 'node:util'
import { existsSync, statSync } from 'node:fs'
import path from 'node:path'

const execFile = promisify(execFileCallback)

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

type WorkerStatus = {
  installed: boolean
  paired: boolean
  running: boolean
  workspaceCount: number
  error: string | null
}

let workerProcess: ChildProcess | undefined
let workerError: string | null = null

function workerCommand(): string {
  return process.platform === 'win32' ? 'threadforge-worker.cmd' : 'threadforge-worker'
}

function workerExecOptions() {
  return {
    timeout: 10_000,
    windowsHide: true,
    maxBuffer: 256 * 1024,
    shell: process.platform === 'win32',
  }
}

async function workerInstalled(): Promise<boolean> {
  try {
    await execFile(process.platform === 'win32' ? 'where.exe' : 'which', [workerCommand()], workerExecOptions())
    return true
  } catch {
    return false
  }
}

async function readWorkerStatus(): Promise<WorkerStatus> {
  const installed = await workerInstalled()
  if (!installed) {
    return { installed: false, paired: false, running: false, workspaceCount: 0, error: null }
  }

  try {
    const { stdout } = await execFile(workerCommand(), ['status'], workerExecOptions())
    const deviceLine = stdout.split(/\r?\n/).find((line) => line.startsWith('Device:')) ?? ''
    const workspaceLine = stdout.split(/\r?\n/).find((line) => line.startsWith('Workspaces:')) ?? ''
    const paired = !deviceLine.includes('(not paired)') && deviceLine.trim().length > 0
    const workspaceCount = Number.parseInt(workspaceLine.split(':', 2)[1] ?? '0', 10)
    return {
      installed,
      paired,
      running: workerProcess?.exitCode == null && workerProcess != null,
      workspaceCount: Number.isFinite(workspaceCount) ? workspaceCount : 0,
      error: workerError,
    }
  } catch (error) {
    return {
      installed,
      paired: false,
      running: workerProcess?.exitCode == null && workerProcess != null,
      workspaceCount: 0,
      error: error instanceof Error ? error.message : '无法读取 Worker 状态',
    }
  }
}

function validateServerUrl(value: string): string {
  const url = new URL(value.trim())
  const loopback = ['127.0.0.1', '::1', 'localhost'].includes(url.hostname)
  const safeTransport = url.protocol === 'https:' || (url.protocol === 'http:' && loopback)
  if (!safeTransport || url.pathname !== '/' || url.search || url.hash || url.username || url.password) {
    throw new Error('Worker 服务地址必须使用 HTTPS；本地开发可使用 loopback HTTP')
  }
  return url.origin
}

function validateText(value: string, label: string): string {
  const normalized = value.trim()
  if (!normalized || normalized.length > 256) throw new Error(`${label}不能为空且不能超过 256 个字符`)
  return normalized
}

async function pairWorker(server: string, code: string, name: string): Promise<void> {
  const normalizedServer = validateServerUrl(server)
  const normalizedCode = validateText(code, '配对码')
  const normalizedName = validateText(name, '设备名称')
  await execFile(workerCommand(), ['pair', '--server', normalizedServer, '--code', normalizedCode, '--name', normalizedName], workerExecOptions())
  await startWorker()
}

async function addWorkerWorkspace(workspacePath: string, name?: string): Promise<void> {
  const normalizedPath = path.resolve(validateText(workspacePath, '工作区路径'))
  if (!existsSync(normalizedPath) || !statSync(normalizedPath).isDirectory()) {
    throw new Error('选择的路径不是有效目录')
  }
  const args = ['workspace', 'add', normalizedPath]
  if (name?.trim()) args.push('--name', validateText(name, '工作区名称'))
  await execFile(workerCommand(), args, workerExecOptions())
  await restartWorker()
}

async function startWorker(): Promise<void> {
  if (workerProcess?.exitCode == null && workerProcess != null) return
  if (!(await workerInstalled())) throw new Error('未检测到 ThreadForge Worker，请先运行安装脚本')
  workerError = null
  const child = spawn(workerCommand(), ['run'], {
    detached: false,
    shell: process.platform === 'win32',
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })
  workerProcess = child
  child.stdout?.on('data', (chunk: Buffer) => {
    if (process.env.NODE_ENV === 'development') console.log(`[threadforge-worker] ${chunk.toString().trim()}`)
  })
  child.stderr?.on('data', (chunk: Buffer) => {
    workerError = chunk.toString().trim().slice(-1000)
  })
  child.once('error', (error) => {
    workerError = error.message
  })
  child.once('exit', (code) => {
    if (code !== 0) workerError = workerError ?? `Worker 已退出（${code ?? 'unknown'}）`
    if (workerProcess === child) workerProcess = undefined
  })
}

function stopWorker(): Promise<void> {
  const child = workerProcess
  workerProcess = undefined
  if (!child || child.exitCode != null) return Promise.resolve()
  return new Promise((resolve) => {
    const finish = () => resolve()
    child.once('exit', finish)
    if (process.platform === 'win32' && child.pid) {
      void execFile('taskkill.exe', ['/pid', String(child.pid), '/t', '/f'], workerExecOptions()).catch(() => undefined)
    } else {
      child.kill()
    }
    setTimeout(finish, 2_000)
  })
}

async function restartWorker(): Promise<void> {
  await stopWorker()
  await startWorker()
}

function registerDesktopIpc(): void {
  ipcMain.handle('threadforge:select-directory', async () => {
    const result = await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] })
    return result.canceled ? null : (result.filePaths[0] ?? null)
  })
  ipcMain.handle('threadforge:worker-status', () => readWorkerStatus())
  ipcMain.handle('threadforge:worker-pair', (_event, args: { server: string; code: string; name: string }) =>
    pairWorker(args.server, args.code, args.name),
  )
  ipcMain.handle('threadforge:worker-add-workspace', (_event, args: { path: string; name?: string }) =>
    addWorkerWorkspace(args.path, args.name),
  )
  ipcMain.handle('threadforge:worker-start', () => startWorker())
  ipcMain.handle('threadforge:worker-stop', () => stopWorker())
}

async function autoStartWorker(): Promise<void> {
  const status = await readWorkerStatus()
  if (status.paired && !status.running) {
    try {
      await startWorker()
    } catch (error) {
      workerError = error instanceof Error ? error.message : '无法启动 Worker'
    }
  }
}

function openExternalUrl(url: string) {
  if (url.startsWith('https://') || url.startsWith('mailto:')) {
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
  registerDesktopIpc()
  createMainWindow()
  void autoStartWorker()

  // macOS 惯例:点击 Dock 图标且无窗口时重建窗口
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow()
  })
})

// 单用户本机工具,无托盘常驻需求:Windows/Linux 全部窗口关闭即退出
app.on('window-all-closed', () => {
  void stopWorker()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  void stopWorker()
})

import { contextBridge, ipcRenderer } from 'electron'

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'

function resolveApiBaseUrl(): string {
  const candidate = process.env.THREADFORGE_API_BASE_URL ?? DEFAULT_API_BASE_URL
  try {
    const url = new URL(candidate)
    const loopback = ['127.0.0.1', '::1', 'localhost'].includes(url.hostname)
    const safeTransport = url.protocol === 'https:' || (url.protocol === 'http:' && loopback)
    if (!safeTransport || url.pathname !== '/' || url.search || url.hash || url.username || url.password) {
      return DEFAULT_API_BASE_URL
    }
    return url.origin
  } catch {
    return DEFAULT_API_BASE_URL
  }
}

// 渲染进程 ↔ 主进程桥:web 环境(window.threadforge 为 undefined)下渲染层需判空访问。
// 后续原生能力(workspace 目录选择、自动拉起 api-server 等)经这里按需扩展。
const threadforge = {
  platform: process.platform,
  apiBaseUrl: resolveApiBaseUrl(),
  versions: {
    electron: process.versions.electron ?? '',
    chrome: process.versions.chrome ?? '',
  },
  desktop: {
    selectDirectory: (): Promise<string | null> => ipcRenderer.invoke('threadforge:select-directory'),
    workerStatus: (): Promise<{
      installed: boolean
      paired: boolean
      running: boolean
      workspaceCount: number
      error: string | null
    }> => ipcRenderer.invoke('threadforge:worker-status'),
    pairWorker: (args: { server: string; code: string; name: string }): Promise<void> =>
      ipcRenderer.invoke('threadforge:worker-pair', args),
    addWorkspace: (args: { path: string; name?: string }): Promise<void> =>
      ipcRenderer.invoke('threadforge:worker-add-workspace', args),
    startWorker: (): Promise<void> => ipcRenderer.invoke('threadforge:worker-start'),
    stopWorker: (): Promise<void> => ipcRenderer.invoke('threadforge:worker-stop'),
  },
} as const

contextBridge.exposeInMainWorld('threadforge', threadforge)

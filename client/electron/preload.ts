import { contextBridge } from 'electron'

// 渲染进程 ↔ 主进程桥:web 环境(window.threadforge 为 undefined)下渲染层需判空访问。
// 后续原生能力(workspace 目录选择、自动拉起 api-server 等)经这里按需扩展。
const threadforge = {
  platform: process.platform,
  versions: {
    electron: process.versions.electron ?? '',
    chrome: process.versions.chrome ?? '',
  },
} as const

contextBridge.exposeInMainWorld('threadforge', threadforge)

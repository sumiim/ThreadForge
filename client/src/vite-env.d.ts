/// <reference types="vite/client" />

// Electron preload 经 contextBridge 注入的桥接 API;纯 web 环境不存在,使用前判空
interface Window {
  threadforge?: {
    platform: string
    apiBaseUrl: string
    versions: { electron: string; chrome: string }
  }
}

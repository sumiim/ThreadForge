import { useCallback, useEffect, useState } from 'react'

export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = 'threadforge-theme'

function getInitialMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    // localStorage 不可用(隐私模式等)时回落系统偏好
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

// 主题状态:localStorage 持久化,未设置过则跟随系统。
// 切换时同步 documentElement 的 .dark 类(Tailwind 变量色板)与 antd 主题由 App 层决定。
export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(getInitialMode)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', mode === 'dark')
    try {
      localStorage.setItem(STORAGE_KEY, mode)
    } catch {
      // 持久化失败不影响本次会话内的主题切换
    }
  }, [mode])

  const toggle = useCallback(() => {
    setMode((m) => (m === 'dark' ? 'light' : 'dark'))
  }, [])

  return { mode, toggle }
}

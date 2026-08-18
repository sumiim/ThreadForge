/**
 * CodeBlock: 增强代码块
 * - shiki 语法高亮（异步加载，fallback 为纯文本）
 * - 语言标签 + 复制按钮 Banner
 * - 暗色自动适配
 * 灵感来自 deepseek-harness 的 CodeBlock + figma 设计
 */

import { useCallback, useEffect, useMemo, useState, useRef } from 'react'
import type { BundledLanguage, BundledTheme, HighlighterGeneric } from 'shiki'

const SHIKI_LANGUAGES = [
  'javascript', 'typescript', 'jsx', 'tsx', 'html', 'css', 'json',
  'bash', 'shell', 'python', 'rust', 'go', 'java', 'yaml', 'xml',
  'sql', 'markdown', 'diff', 'docker',
] as const satisfies readonly BundledLanguage[]
const SHIKI_THEMES = ['github-light', 'github-dark'] as const satisfies readonly BundledTheme[]

type ShikiLanguage = (typeof SHIKI_LANGUAGES)[number]
type ThreadForgeHighlighter = HighlighterGeneric<BundledLanguage, BundledTheme>

function isShikiLanguage(value: string): value is ShikiLanguage {
  return (SHIKI_LANGUAGES as readonly string[]).includes(value)
}

/** 单例 highlighter（懒加载） */
let highlighterPromise: Promise<ThreadForgeHighlighter> | null = null
let highlighter: ThreadForgeHighlighter | null = null

async function getHighlighter(): Promise<ThreadForgeHighlighter> {
  if (highlighter) return highlighter
  if (!highlighterPromise) {
    highlighterPromise = (async () => {
      const { createHighlighter } = await import('shiki')
      const hl = await createHighlighter({
        langs: [...SHIKI_LANGUAGES],
        themes: [...SHIKI_THEMES],
      })
      highlighter = hl
      return hl
    })()
  }
  return highlighterPromise
}

/** 预热 highlighter（不影响渲染，提前加载） */
if (typeof window !== 'undefined') {
  // 空闲时启动，不阻塞关键渲染路径
  const idleWindow = window as Window & {
    requestIdleCallback?: (callback: () => void) => number
  }
  if (idleWindow.requestIdleCallback) {
    idleWindow.requestIdleCallback(() => { void getHighlighter() })
  } else {
    setTimeout(() => { void getHighlighter() }, 200)
  }
}

interface CodeBlockProps {
  code: string
  lang?: string
  /** 自定义类名 */
  className?: string
}

export default function CodeBlock({ code, lang, className }: CodeBlockProps) {
  const trimmed = code.endsWith('\n') ? code.slice(0, -1) : code
  const [highlighted, setHighlighted] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  // 是否暗色（检查 <html> 或 <body> 的 class/dark 变量）
  const isDark = useMemo(() => {
    if (typeof document === 'undefined') return false
    return document.documentElement.classList.contains('dark')
      || document.body.classList.contains('dark')
  }, [])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      const language = lang?.toLowerCase()
      if (!language || !isShikiLanguage(language)) {
        setHighlighted(null)
        return
      }
      try {
        const hl = await getHighlighter()
        if (cancelled) return
        const theme = isDark ? 'github-dark' : 'github-light'
        const html = hl.codeToHtml(trimmed, {
          lang: language,
          theme,
        })
        if (!cancelled) setHighlighted(html)
      } catch {
        // highlight 失败时保持 null，走纯文本 fallback
        if (!cancelled) setHighlighted(null)
      }
    })()
    return () => { cancelled = true }
  }, [trimmed, lang, isDark])

  const onCopy = useCallback(() => {
    if (copied) return
    const text = rootRef.current?.querySelector('pre')?.textContent ?? trimmed
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1000)
    })
  }, [copied, trimmed])

  return (
    <div ref={rootRef} className={`group/code relative my-3 overflow-hidden rounded-lg border border-stone-200 bg-white dark:border-stone-700 dark:bg-[#0d1117] ${className ?? ''}`}>
      {/* Banner: 语言标签 + 复制按钮 */}
      <div className="flex items-center justify-between border-b border-stone-200 bg-stone-50 px-4 py-1.5 text-xs dark:border-white/10 dark:bg-white/5">
        <span className="font-mono uppercase tracking-wide text-stone-500 dark:text-stone-400">
          {lang ?? 'code'}
        </span>
        <button
          type="button"
          onClick={onCopy}
          className="flex items-center gap-1 rounded-md px-2 py-1 font-sans text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-700 active:scale-95 dark:text-stone-400 dark:hover:bg-white/10 dark:hover:text-stone-200"
        >
          {copied ? (
            <>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
                <path d="M3 7.5L5.5 10L11 4" stroke="currentColor" strokeWidth="1.5"
                  strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              已复制
            </>
          ) : (
            <>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
                <rect x="3.5" y="3.5" width="9" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
                <path d="M10.5 3.5V2.5A1.5 1.5 0 009 1H3.5A1.5 1.5 0 002 2.5v5A1.5 1.5 0 003.5 9h.5"
                  stroke="currentColor" strokeWidth="1.2" />
              </svg>
              复制
            </>
          )}
        </button>
      </div>

      {/* 代码体 */}
      {highlighted ? (
        <div
          className="overflow-x-auto p-4 text-sm leading-relaxed [&_pre]:!bg-transparent [&_pre]:!p-0"
          dangerouslySetInnerHTML={{ __html: highlighted }}
        />
      ) : (
        <pre className="overflow-x-auto p-4 text-sm leading-relaxed text-stone-700 dark:text-stone-200">
          <code>{trimmed}</code>
        </pre>
      )}
    </div>
  )
}

/**
 * ReasoningRow: "Think" 可折叠推理块
 * 灵感来自 deepseek-harness 的 ReasoningRow（Think 标题 + 摘要行 + 展开灰色正文）
 *
 * 在 ThreadForge 中，推理文本作为 assistant 消息 content 的一部分以
 *  ```think
 *  ...
 *  ```
 * 代码块形式嵌入。Markdown 渲染器检测到 language="think" 的代码块时，
 * 不渲染为代码块，而是渲染为这个可折叠的推理行。
 */

import { useEffect, useRef, useState } from 'react'

function firstLine(text: string): string {
  const newline = text.indexOf('\n')
  return newline === -1 ? text : text.slice(0, newline)
}

function latestLine(text: string): string {
  const visible = text.trimEnd()
  const newline = visible.lastIndexOf('\n')
  return newline === -1 ? visible : visible.slice(newline + 1)
}

interface ReasoningRowProps {
  /** 推理文本 */
  text: string
  /** 是否正在流式输出（作为最后一块） */
  running?: boolean
}

export default function ReasoningRow({ text, running = false }: ReasoningRowProps) {
  const [expanded, setExpanded] = useState(false)
  const summaryRef = useRef<HTMLSpanElement>(null)
  const summary = running ? latestLine(text) : firstLine(text)

  // 流式输出时自动滚动摘要到最右（跟踪最新内容）
  useEffect(() => {
    if (running && summaryRef.current) {
      summaryRef.current.scrollLeft = summaryRef.current.scrollWidth - summaryRef.current.clientWidth
    }
  }, [running, summary])

  return (
    <div
      className="group/reasoning"
      data-state={running ? 'running' : 'idle'}
    >
      <div
        className="flex cursor-pointer items-center gap-1.5 rounded-lg px-1.5 py-1 text-xs text-stone-500 select-none hover:bg-stone-100/80 transition-colors duration-150"
        onClick={() => setExpanded(v => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(v => !v) } }}
      >
        {/* Think 图标 */}
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="shrink-0 text-stone-400" aria-hidden>
          <path
            d="M7 1C3.686 1 1 3.686 1 7s2.686 6 6 6 6-2.686 6-6-2.686-6-6-6z"
            stroke="currentColor" strokeWidth="1.2" fill="none"
          />
          <path
            d="M5.5 5.5L8 7l-2.5 1.5" stroke="currentColor" strokeWidth="1.2"
            strokeLinecap="round" strokeLinejoin="round" fill="none"
          />
        </svg>

        <span className="font-medium text-stone-500">Think</span>

        {/* 圆点分隔符 */}
        <span className="h-1 w-1 shrink-0 rounded-full bg-stone-300" aria-hidden />

        {/* 摘要行 */}
        <span
          ref={summaryRef}
          className="min-w-0 flex-1 truncate text-stone-400"
          data-follow-end={running || undefined}
        >
          {summary}
        </span>

        {/* 展开/折叠箭头 */}
        <svg
          width="12" height="12" viewBox="0 0 12 12" fill="none"
          className={`shrink-0 text-stone-400 transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}
          aria-hidden
        >
          <path d="M4.5 2.5L7.5 6l-3 3.5" stroke="currentColor" strokeWidth="1.2"
            strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>

        {/* 流式运行时扫光指示器 */}
        {running && (
          <span className="ml-auto h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-blue-500" />
        )}
      </div>

      {/* 展开的推理正文 */}
      {expanded && (
        <div className="ml-4 mt-0.5 border-l-2 border-stone-200 pl-3.5 py-1">
          <pre className="whitespace-pre-wrap text-xs leading-relaxed text-stone-500 font-[inherit] m-0">
            {text}
          </pre>
        </div>
      )}
    </div>
  )
}
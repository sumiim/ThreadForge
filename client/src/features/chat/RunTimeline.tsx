import { useMemo, useState } from 'react'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import type { SessionRun } from '../../api/types'
import { barClass, type TimelineInput } from './traceModel'

interface RunTimelineProps {
  runs: SessionRun[]
  activeRunId?: string
  onSelectRun?: (runId: string) => void
  inputs?: TimelineInput[]
}

interface RequestIndexEntry extends TimelineInput {
  run?: SessionRun
}

function requestLabel(content: string): string {
  return content.trim().replace(/\s+/g, ' ') || '空请求'
}

function nearestRun(input: TimelineInput, runs: SessionRun[]): SessionRun | undefined {
  const inputTime = new Date(input.createdAt).getTime()
  if (Number.isNaN(inputTime)) return undefined
  return runs.reduce<SessionRun | undefined>((nearest, run) => {
    const runTime = new Date(run.startedAt).getTime()
    if (Number.isNaN(runTime)) return nearest
    if (!nearest) return run
    const nearestTime = new Date(nearest.startedAt).getTime()
    return Math.abs(runTime - inputTime) < Math.abs(nearestTime - inputTime) ? run : nearest
  }, undefined)
}

export default function RunTimeline({ runs, activeRunId, onSelectRun, inputs = [] }: RunTimelineProps) {
  const [expanded, setExpanded] = useState(false)
  const [activeKey, setActiveKey] = useState('')
  const entries = useMemo<RequestIndexEntry[]>(() => (
    [...inputs]
      .sort((left, right) => new Date(left.createdAt).getTime() - new Date(right.createdAt).getTime())
      .map((input) => ({ ...input, run: nearestRun(input, runs) }))
  ), [inputs, runs])

  const jump = (entry: RequestIndexEntry) => {
    setActiveKey(entry.id)
    if (entry.run) onSelectRun?.(entry.run.runId)
    const target = document.getElementById(`message-${entry.id}`)
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      target.classList.add('timeline-jump-highlight')
      window.setTimeout(() => target.classList.remove('timeline-jump-highlight'), 900)
    }
  }

  if (entries.length === 0) return null

  return (
    <aside
      aria-label="请求索引"
      className={`flex min-h-0 shrink-0 flex-col border-r border-stone-200 bg-white/90 transition-[width] ${expanded ? 'w-64' : 'w-9'}`}
    >
      <div className={`flex h-8 shrink-0 items-center border-b border-stone-100 ${expanded ? 'gap-1 px-2' : 'justify-center'}`}>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-label={expanded ? '收起请求索引' : '展开请求索引'}
          title={expanded ? '收起请求索引' : '展开请求索引'}
          className="flex h-5 w-5 items-center justify-center rounded text-stone-500 hover:bg-stone-100"
        >
          {expanded ? <DownOutlined className="text-[10px]" /> : <RightOutlined className="text-[10px]" />}
        </button>
        {expanded ? (
          <span className="truncate text-[11px] font-medium text-stone-700">请求索引 · {entries.length}</span>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {expanded ? (
          // 主对话只索引用户请求；模型、工具和审查事件留在独立审计界面。
          <div className="flex flex-col gap-0.5 p-2">
            {entries.map((entry, index) => {
              const active = activeKey === entry.id || (!activeKey && entry.run?.runId === activeRunId)
              const time = new Date(entry.createdAt).getTime()
              const timeLabel = Number.isNaN(time)
                ? ''
                : new Date(time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
              const label = requestLabel(entry.content)
              return (
                <button
                  key={entry.id}
                  type="button"
                  aria-label={`请求 ${index + 1}：${label}`}
                  title={label}
                  onClick={() => jump(entry)}
                  className={`flex items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-[11px] transition-colors ${
                    active ? 'bg-blue-50 text-blue-700' : 'text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-700/40'
                  }`}
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${barClass({ type: 'user.input', status: entry.run?.status }, active)}`}
                    aria-hidden
                  />
                  <span className="w-5 shrink-0 font-mono text-stone-400">{index + 1}</span>
                  <span className="min-w-0 flex-1 truncate">{label}</span>
                  <span className="shrink-0 font-mono text-stone-400 dark:text-stone-500">{timeLabel}</span>
                </button>
              )
            })}
          </div>
        ) : null}
      </div>
    </aside>
  )
}

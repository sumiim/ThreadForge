import { useMemo, useState, type MouseEvent } from 'react'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import type { SessionRun } from '../../api/types'
import {
  barClass,
  eventLabel,
  eventTimeOf,
  inputTimelineEvent,
  isFailed,
  isMainTimelineEvent,
  sortTimelineItems,
  type TimelineInput,
  type TimelineEvent,
} from './traceModel'

interface RunTimelineProps {
  runs: SessionRun[]
  activeRunId?: string
  onSelectRun?: (runId: string) => void
  inputs?: TimelineInput[]
}

interface TimelineEntry extends TimelineEvent {
  run: SessionRun
}

const ROWS = [
  { id: 'input', label: '输入', match: (item: TimelineEntry) => item.type === 'user.input' },
  { id: 'conversation', label: '对话', match: (item: TimelineEntry) => item.type === 'assistant.commentary' || item.type === 'message.completed' },
  { id: 'planning', label: '计划', match: (item: TimelineEntry) => item.type === 'plan.created' || item.type === 'plan.skipped' },
  { id: 'model', label: '模型', match: (item: TimelineEntry) => item.type === 'model.started' },
  { id: 'tools', label: '工具', match: (item: TimelineEntry) => item.type === 'tool.requested' || item.type === 'tool.failed' },
  { id: 'review', label: '审查', match: (item: TimelineEntry) => item.type === 'review.started' || item.type === 'review.completed' },
  { id: 'final', label: '终答', match: (item: TimelineEntry) => item.type.startsWith('task.') },
] as const

function safeSpan(entries: TimelineEntry[]): { start: number; span: number } {
  const starts = entries.map(eventTimeOf).filter((value) => !Number.isNaN(value))
  if (starts.length === 0) return { start: 0, span: 1 }
  const ends = entries
    .map((entry) => entry.ended_at ? new Date(entry.ended_at).getTime() : eventTimeOf(entry))
    .filter((value) => !Number.isNaN(value))
  const start = Math.min(...starts)
  const end = Math.max(...ends)
  return { start, span: Math.max(1_000, end - start) }
}

function eventWidth(entry: TimelineEntry, span: number): number {
  const start = eventTimeOf(entry)
  const end = entry.ended_at ? new Date(entry.ended_at).getTime() : Number.NaN
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return 0.45
  return Math.max(0.45, ((end - start) / span) * 100)
}

export default function RunTimeline({ runs, activeRunId, onSelectRun, inputs = [] }: RunTimelineProps) {
  const [expanded, setExpanded] = useState(false)
  const [activeKey, setActiveKey] = useState('')
  const activeRun = useMemo(
    () => runs.find((run) => run.runId === activeRunId) ?? runs[runs.length - 1],
    [activeRunId, runs],
  )

  const entries = useMemo<TimelineEntry[]>(() => {
    if (!activeRun) return []
    const runItems = activeRun.items
      .filter(isMainTimelineEvent)
      .map((item) => ({ ...item, source: 'run' as const, run: activeRun }))
    const runStart = new Date(activeRun.startedAt).getTime()
    const nearestInput = inputs.reduce<TimelineInput | undefined>((nearest, input) => {
      if (!nearest || Number.isNaN(runStart)) return input
      const nearestDistance = Math.abs(new Date(nearest.createdAt).getTime() - runStart)
      const inputDistance = Math.abs(new Date(input.createdAt).getTime() - runStart)
      return inputDistance < nearestDistance ? input : nearest
    }, undefined)
    const inputItems = nearestInput ? [{ ...inputTimelineEvent(nearestInput), run: activeRun }] : []
    return sortTimelineItems([...runItems, ...inputItems])
  }, [activeRun, inputs])

  const { start, span } = useMemo(() => safeSpan(entries), [entries])

  const position = (entry: TimelineEntry): number => {
    const time = eventTimeOf(entry)
    if (Number.isNaN(time)) return 0
    return Math.min(99.5, Math.max(0, ((time - start) / span) * 100))
  }

  const jump = (entry: TimelineEntry) => {
    setActiveKey(entry.event_id)
    onSelectRun?.(entry.run.runId)
    const target = entry.message_id
      ? document.getElementById(`message-${entry.message_id}`)
      : entry.tool_call_id
        ? document.getElementById(`tool-call-${entry.tool_call_id}`)
        : document.getElementById(`run-event-${entry.event_id}`)
    if (target) {
      const details = target.closest('details')
      if (details && !details.open) details.open = true
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      target.classList.add('timeline-jump-highlight')
      window.setTimeout(() => target.classList.remove('timeline-jump-highlight'), 900)
      return
    }
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const messages = Array.from(container.querySelectorAll<HTMLElement>('[data-message-created-at]'))
    if (messages.length === 0) return
    const eventTime = eventTimeOf(entry)
    const nearest = messages.reduce((best, message) => {
      const messageTime = new Date(message.dataset.messageCreatedAt ?? '').getTime()
      const distance = Number.isNaN(eventTime) || Number.isNaN(messageTime) ? Number.POSITIVE_INFINITY : Math.abs(messageTime - eventTime)
      return distance < best.distance ? { message, distance } : best
    }, { message: messages[0], distance: Number.POSITIVE_INFINITY })
    nearest.message.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const jumpNearest = (event: MouseEvent<HTMLDivElement>, rowEntries: TimelineEntry[]) => {
    if ((event.target as HTMLElement).closest('button')) return
    if (rowEntries.length === 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    const time = start + Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)) * span
    const nearest = rowEntries.reduce((best, entry) => {
      const distance = Math.abs(eventTimeOf(entry) - time)
      return distance < best.distance ? { entry, distance } : best
    }, { entry: rowEntries[0], distance: Number.POSITIVE_INFINITY })
    jump(nearest.entry)
  }

  if (!activeRun || entries.length === 0) return null

  const runIndex = runs.findIndex((run) => run.runId === activeRun.runId)
  const visibleRows = expanded ? ROWS : ROWS.filter((row) => row.id === 'input' || row.id === 'model' || row.id === 'tools')
  return (
    <nav aria-label="运行轨迹导航" className="w-full shrink-0 border-b border-stone-200 bg-white/95 px-3 py-1.5">
      <div className="mb-1 flex items-center gap-2">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-label={expanded ? '收起运行轨迹' : '展开运行轨迹'}
          title={expanded ? '收起运行轨迹' : '展开运行轨迹'}
          className="flex h-5 w-5 items-center justify-center rounded text-stone-500 hover:bg-stone-100"
        >
          {expanded ? <DownOutlined className="text-[10px]" /> : <RightOutlined className="text-[10px]" />}
        </button>
        <span className="text-xs font-medium text-stone-700">运行轨迹</span>
        <span className="font-mono text-[10px] text-stone-400">R{runIndex >= 0 ? runIndex + 1 : 1}</span>
        <span className="ml-auto hidden text-[10px] text-stone-400 sm:inline">点击事件跳转到对话</span>
      </div>
      <div className="overflow-hidden">
        {visibleRows.map((row) => {
          const rowEntries = entries.filter(row.match)
          return (
            <div key={row.id} className="grid grid-cols-[42px_minmax(0,1fr)] items-center gap-2">
              <span className="truncate text-[10px] text-stone-400">{row.label}</span>
              <div
                className="relative h-3 rounded-sm bg-stone-50"
                onClick={(event) => jumpNearest(event, rowEntries)}
              >
                {rowEntries.map((entry) => (
                  <button
                    key={entry.event_id}
                    type="button"
                    aria-label={eventLabel(entry.type)}
                    title={`${eventLabel(entry.type)}${entry.tool_name ? ` · ${entry.tool_name}` : ''}${isFailed(entry) ? ' · 失败' : ''}`}
                    onClick={() => jump(entry)}
                    className={`absolute top-0.5 h-2 rounded-[2px] transition-opacity hover:opacity-100 ${barClass(entry, activeKey === entry.event_id)} ${activeKey === entry.event_id ? 'opacity-100' : 'opacity-70'}`}
                    style={{ left: `${position(entry)}%`, width: `${eventWidth(entry, span)}%`, minWidth: 3 }}
                  />
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </nav>
  )
}

import { useMemo, useState, type MouseEvent, type PointerEvent } from 'react'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import type { SessionRun } from '../../api/types'
import {
  eventTimeOf,
  groupTimelineItems,
  inputTimelineEvent,
  isMainTimelineEvent,
  LANE_ORDER,
  laneOf,
  runStatusLabel,
  sortTimelineItems,
  timelineBounds,
  type TimelineBounds,
  type TimelineEvent,
  type TimelineGroup,
  type TimelineInput,
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

function laneOffset(type: string, expanded: boolean): number {
  const index = Math.max(0, LANE_ORDER.indexOf(laneOf(type)))
  return (expanded ? 12 : 3) * index + (expanded ? 10 : 5)
}

function groupRange(group: TimelineGroup, bounds: TimelineBounds): { top: number; height: number; point: boolean } {
  const start = Number.isFinite(group.start) ? group.start : bounds.start
  const end = Number.isFinite(group.end) && group.end > start ? group.end : start
  const safeStart = Math.min(bounds.end, Math.max(bounds.start, start))
  const safeEnd = Math.max(safeStart, Math.min(bounds.end, end))
  return {
    top: ((safeStart - bounds.start) / bounds.span) * 100,
    height: ((safeEnd - safeStart) / bounds.span) * 100,
    point: safeEnd <= safeStart,
  }
}

function groupTitle(group: TimelineGroup): string {
  const label = group.count > 1 && group.toolNames.length > 0
    ? `${group.toolNames.join('、')} × ${group.count}`
    : eventLabelOf(group)
  const ms = Number.isFinite(group.start) && Number.isFinite(group.end) && group.end > group.start
    ? group.end - group.start
    : 0
  const duration = ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : ms > 0 ? `${ms}ms` : ''
  return `${label}${duration ? ` · ${duration}` : ''}${group.failed ? ' · 失败' : ''}`
}

function eventLabelOf(group: TimelineGroup): string {
  if (group.type === 'bucket') return `${group.count} 个事件`
  return group.sample.label || group.type
}

export default function RunTimeline({ runs, activeRunId, onSelectRun, inputs = [] }: RunTimelineProps) {
  const [expanded, setExpanded] = useState(false)
  const [activeKey, setActiveKey] = useState('')
  const [scrubbing, setScrubbing] = useState(false)
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

  const bounds = useMemo(() => timelineBounds(entries), [entries])
  const groups = useMemo(() => groupTimelineItems(entries), [entries])

  const jump = (entry: TimelineEvent) => {
    setActiveKey(entry.event_id)
    onSelectRun?.(activeRun?.runId ?? '')
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
      const distance = Number.isNaN(eventTime) || Number.isNaN(messageTime)
        ? Number.POSITIVE_INFINITY
        : Math.abs(messageTime - eventTime)
      return distance < best.distance ? { message, distance } : best
    }, { message: messages[0], distance: Number.POSITIVE_INFINITY })
    nearest.message.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const scrubTo = (clientY: number, rect: DOMRect) => {
    if (entries.length === 0) return
    const time = bounds.start + Math.min(1, Math.max(0, (clientY - rect.top) / rect.height)) * bounds.span
    const nearest = entries.reduce((best, entry) => {
      const distance = Math.abs(eventTimeOf(entry) - time)
      return distance < best.distance ? { entry, distance } : best
    }, { entry: entries[0]!, distance: Number.POSITIVE_INFINITY })
    jump(nearest.entry)
  }

  const jumpNearest = (event: MouseEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button') || entries.length === 0) return
    scrubTo(event.clientY, event.currentTarget.getBoundingClientRect())
  }

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button') || entries.length === 0) return
    event.currentTarget.setPointerCapture(event.pointerId)
    setScrubbing(true)
    scrubTo(event.clientY, event.currentTarget.getBoundingClientRect())
  }
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!scrubbing) return
    scrubTo(event.clientY, event.currentTarget.getBoundingClientRect())
  }
  const onPointerUp = () => setScrubbing(false)

  if (!activeRun || entries.length === 0) return null

  const runIndex = runs.findIndex((run) => run.runId === activeRun.runId)
  return (
    <aside
      aria-label="运行轨迹导航"
      className={`flex min-h-0 shrink-0 flex-col border-r border-stone-200 bg-white/90 transition-[width] ${expanded ? 'w-32' : 'w-9'}`}
    >
      <div className={`flex h-8 shrink-0 items-center border-b border-stone-100 ${expanded ? 'gap-1 px-2' : 'justify-center'}`}>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-label={expanded ? '收起运行轨迹' : '展开运行轨迹'}
          title={expanded ? '收起运行轨迹' : '展开运行轨迹'}
          className="flex h-5 w-5 items-center justify-center rounded text-stone-500 hover:bg-stone-100"
        >
          {expanded ? <DownOutlined className="text-[10px]" /> : <RightOutlined className="text-[10px]" />}
        </button>
        {expanded ? (
          <span
            className="truncate text-[11px] font-medium text-stone-700"
            title={`开始 ${activeRun.startedAt ? new Date(activeRun.startedAt).toLocaleTimeString() : '未知'} · 状态 ${runStatusLabel(activeRun.status)}`}
          >
            运行轨迹 R{runIndex >= 0 ? runIndex + 1 : 1} · {runStatusLabel(activeRun.status)}
          </span>
        ) : null}
      </div>
      <div
        className="relative min-h-0 flex-1 select-none overflow-hidden"
        onClick={jumpNearest}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        title="点击或拖动轨迹跳转到对话"
      >
        <div className="absolute inset-x-2 bottom-2 top-2">
          <div className="absolute bottom-0 left-1/2 top-0 w-px -translate-x-1/2 bg-stone-100" aria-hidden />
          {groups.map((group) => {
            const range = groupRange(group, bounds)
            const point = range.point
            const active = activeKey === group.sample.event_id
            const barClass = group.failed
              ? (group.sample.status === 'needs_fix' ? 'bg-amber-400' : 'bg-red-400 ring-1 ring-red-500')
              : group.running
                ? 'animate-pulse bg-blue-400'
                : active
                  ? 'bg-blue-600'
                  : 'bg-stone-400'
            return (
              <button
                key={group.key}
                type="button"
                aria-label={groupTitle(group)}
                title={groupTitle(group)}
                onClick={() => jump(group.sample)}
                className={`absolute z-10 -translate-y-0.5 transition-opacity hover:opacity-100 ${point ? 'h-2 w-2 -translate-x-1/2 rounded-full' : 'w-1 rounded-full'} ${barClass} ${active ? 'opacity-100' : 'opacity-65'}`}
                style={{
                  top: `${range.top}%`,
                  height: point ? undefined : `${range.height}%`,
                  left: `${laneOffset(group.type, expanded)}px`,
                }}
              />
            )
          })}
        </div>
      </div>
    </aside>
  )
}

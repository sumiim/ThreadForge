import { useMemo, useState } from 'react'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import type { SessionRun } from '../../api/types'
import {
  eventTimeOf,
  groupTimelineItems,
  inputTimelineEvent,
  isMainTimelineEvent,
  runStatusLabel,
  sortTimelineItems,
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

  if (!activeRun || entries.length === 0) return null

  const runIndex = runs.findIndex((run) => run.runId === activeRun.runId)
  return (
    <aside
      aria-label="运行轨迹导航"
      className={`flex min-h-0 shrink-0 flex-col border-r border-stone-200 bg-white/90 transition-[width] ${expanded ? 'w-64' : 'w-9'}`}
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
      <div className="min-h-0 flex-1 overflow-y-auto">
        {expanded ? (
          // 对话索引：文字条目列表（时间 + 标签 + 状态色点），点击跳转对话。
          // 替代原泳道图（§8 前端改造，参考 DSH EAC 右侧对话索引；位置仍在左）。
          <div className="flex flex-col gap-0.5 p-2">
            {groups.map((group) => {
              const active = activeKey === group.sample.event_id
              const time = eventTimeOf(group.sample)
              const timeLabel = Number.isNaN(time)
                ? ''
                : new Date(time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
              return (
                <button
                  key={group.key}
                  type="button"
                  aria-label={groupTitle(group)}
                  title={groupTitle(group)}
                  onClick={() => jump(group.sample)}
                  className={`flex items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-[11px] transition-colors ${
                    active ? 'bg-blue-50 text-blue-700' : 'text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-700/40'
                  }`}
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${barClass(group.sample, active)}`}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1 truncate">{groupTitle(group)}</span>
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

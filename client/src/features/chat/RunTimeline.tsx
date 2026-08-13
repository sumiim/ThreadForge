import { useEffect, useMemo, useState, type MouseEvent } from 'react'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import type { RunIndexItem, SessionRun } from '../../api/types'

// 纵向分层泳道：时间自上而下流动，事件为竖向条（替换旧的 RunMinimap/对话索引）。
// 与 §7.1 的 Trace/审计查看器复用同一套泳道映射与颜色语义。

type ScaleMode = 'duration' | 'turns' | 'calls'

const LANE_ORDER = ['model', 'plan', 'tools', 'approval', 'review', 'system'] as const
type Lane = (typeof LANE_ORDER)[number]

const LANE_OF: Record<string, Lane> = {
  'assistant.commentary': 'model',
  'model.started': 'model',
  'model.completed': 'model',
  'model.retrying': 'model',
  'model.protocol_retrying': 'model',
  'plan.created': 'plan',
  'plan.skipped': 'plan',
  'tool.requested': 'tools',
  'tool.started': 'tools',
  'tool.completed': 'tools',
  'tool.failed': 'tools',
  'approval.required': 'approval',
  'approval.resolved': 'approval',
  'review.started': 'review',
  'review.completed': 'review',
  'task.cancel_requested': 'system',
  'task.completed': 'system',
  'task.cancelled': 'system',
  'task.failed': 'system',
  'task.interrupted': 'system',
  'task.blocked': 'system',
}

const LANE_TITLE: Record<Lane, string> = {
  model: '模型',
  plan: '计划',
  tools: '工具',
  approval: '审批',
  review: '审查',
  system: '系统',
}

const SCALE_LABEL: Record<ScaleMode, string> = {
  duration: '耗时',
  turns: '轮次',
  calls: '调用',
}

const SCALE_HINT: Record<ScaleMode, string> = {
  duration: '按真实起止时间投影，保留等待间隔',
  turns: '按模型轮次等高排列',
  calls: '按模型/工具调用序号等高排列',
}

interface RunTimelineProps {
  runs: SessionRun[]
  activeRunId?: string
  onSelectRun?: (runId: string) => void
  /** 用户输入（会话内 user 消息），渲染在 Input 泳道 */
  inputs?: { id: string; content: string; createdAt: string }[]
}

interface TimelineItem extends RunIndexItem {
  lane: Lane
  run: SessionRun
  failed: boolean
  running: boolean
}

function isFailed(item: RunIndexItem): boolean {
  return item.type === 'tool.failed' || ['failed', 'blocked', 'interrupted'].includes(item.status ?? '')
}

function isRunning(item: RunIndexItem): boolean {
  return item.type === 'tool.started' || item.type === 'approval.required'
}

function barClassName(item: TimelineItem, active: boolean): string {
  if (item.failed) return 'bg-red-400 ring-1 ring-red-500'
  if (item.status === 'needs_fix') return 'bg-amber-400'
  if (item.running) return 'animate-pulse bg-blue-400'
  return active ? 'bg-blue-600' : 'bg-stone-400'
}

export default function RunTimeline({ runs, activeRunId, onSelectRun, inputs = [] }: RunTimelineProps) {
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem('threadforge-timeline-expanded') === '1'
    } catch {
      return false
    }
  })
  const [scale, setScale] = useState<ScaleMode>('duration')
  const [activeKey, setActiveKey] = useState('')

  const items = useMemo<TimelineItem[]>(() => {
    const result: TimelineItem[] = []
    for (const run of runs) {
      for (const item of run.items) {
        const lane = LANE_OF[item.type]
        if (!lane) continue
        result.push({ ...item, lane, run, failed: isFailed(item), running: isRunning(item) })
      }
    }
    return result
  }, [runs])

  // 正文滚动时高亮当前视口所在事件
  useEffect(() => {
    if (items.length === 0) return
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const update = () => {
      const maximum = Math.max(1, container.scrollHeight - container.clientHeight)
      const ratio = Math.min(1, Math.max(0, container.scrollTop / maximum))
      const index = Math.round(ratio * Math.max(0, items.length - 1))
      setActiveKey(items[index]?.event_id ?? '')
    }
    update()
    container.addEventListener('scroll', update, { passive: true })
    return () => container.removeEventListener('scroll', update)
  }, [items])

  const activeRun = useMemo(
    () => runs.find((run) => run.runId === activeRunId) ?? runs[runs.length - 1],
    [activeRunId, runs],
  )
  const runItems = useMemo(
    () => (activeRun ? items.filter((item) => item.run.runId === activeRun.runId) : []),
    [activeRun, items],
  )

  // 纵轴投影：返回每个事件的 top/height 百分比（0-100）。
  // Duration 尺度按 started_at/ended_at 计算真实区间；无区间的事件退化为固定高度标记。
  const positions = useMemo(() => {
    const map = new Map<string, { top: number; height: number }>()
    if (runItems.length === 0) return map
    const toTime = (iso?: string) => {
      if (!iso) return Number.NaN
      const t = new Date(iso).getTime()
      return Number.isNaN(t) ? Number.NaN : t
    }
    const MIN_HEIGHT = 1.2
    if (scale === 'duration') {
      const starts = runItems
        .map((item) => toTime(item.started_at) || toTime(item.timestamp))
        .filter((t) => !Number.isNaN(t))
      const ends = runItems
        .map((item) => toTime(item.ended_at) || toTime(item.started_at) || toTime(item.timestamp))
        .filter((t) => !Number.isNaN(t))
      const start = starts.length > 0 ? Math.min(...starts) : 0
      const end = ends.length > 0 ? Math.max(...ends) : start + 1
      const span = Math.max(1, end - start)
      for (const item of runItems) {
        const s = toTime(item.started_at) || toTime(item.timestamp)
        const e = toTime(item.ended_at) || s
        const top = Number.isNaN(s) ? 0 : ((s - start) / span) * 100
        const height = Number.isNaN(e) || e <= s ? MIN_HEIGHT : Math.max(MIN_HEIGHT, ((e - s) / span) * 100)
        map.set(item.event_id, { top, height })
      }
    } else {
      const ordered = scale === 'calls'
        ? runItems.filter((item) => item.lane === 'tools' || item.lane === 'model')
        : runItems
      const n = Math.max(1, ordered.length)
      ordered.forEach((item, index) => map.set(item.event_id, { top: (index / n) * 100, height: Math.max(1.5, 100 / n) }))
    }
    return map
  }, [runItems, scale])

  if (!activeRun) return null

  const toggleExpanded = () => {
    setExpanded((current) => {
      const next = !current
      try {
        localStorage.setItem('threadforge-timeline-expanded', next ? '1' : '0')
      } catch {
        // 忽略 localStorage 不可用
      }
      return next
    })
  }

  const jump = (item: TimelineItem) => {
    setActiveKey(item.event_id)
    onSelectRun?.(item.run.runId)
    const target = item.tool_call_id
      ? document.getElementById(`tool-call-${item.tool_call_id}`)
      : document.getElementById(`run-event-${item.event_id}`)
    if (target) {
      // 若锚点位于折叠的 <details> 内，先展开再滚动，保证跳转精确可达。
      const details = target.closest('details')
      if (details && !details.open) details.open = true
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const messages = Array.from(container.querySelectorAll<HTMLElement>('[data-message-created-at]'))
    if (messages.length > 0) {
      const eventTime = new Date(item.timestamp).getTime()
      const nearest = messages.reduce((best, message) => {
        const distance = Math.abs(new Date(message.dataset.messageCreatedAt ?? '').getTime() - eventTime)
        return distance < best.distance ? { message, distance } : best
      }, { message: messages[0], distance: Number.POSITIVE_INFINITY })
      nearest.message.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }

  const scrollTrackToRatio = (event: MouseEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button')) return
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const rect = event.currentTarget.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))
    container.scrollTo({
      top: ratio * Math.max(1, container.scrollHeight - container.clientHeight),
      behavior: 'smooth',
    })
  }

  const failedCount = runItems.filter((item) => item.failed).length
  const activeIndex = runItems.findIndex((item) => item.event_id === activeKey)

  // 折叠态：窄轨 + 展开按钮 + 淡色刻度 + 失败标记
  if (!expanded) {
    return (
      <nav aria-label="运行轨迹（已折叠）" className="flex w-4 shrink-0 flex-col items-center overflow-hidden border-r border-stone-100 bg-white/70 py-2">
        <button
          type="button"
          onClick={toggleExpanded}
          aria-label="展开运行轨迹"
          title="展开运行轨迹"
          className="mb-1 flex h-4 w-4 shrink-0 items-center justify-center rounded text-stone-400 hover:bg-stone-100 hover:text-stone-700"
        >
          <RightOutlined className="text-[9px]" />
        </button>
        {runItems.length > 0 ? (
          <span className="mb-1 shrink-0 font-mono text-[8px] leading-none text-stone-400" aria-label={`当前第 ${activeIndex + 1} 个事件，共 ${runItems.length} 个`}>
            {activeIndex + 1}/{runItems.length}
          </span>
        ) : null}
        <div className="relative min-h-0 flex-1 w-full" onClick={scrollTrackToRatio}>
          {runItems.map((item) => (
            <button
              key={item.event_id}
              type="button"
              title={`${item.label}${item.tool_name ? ` · ${item.tool_name}` : ''}${item.failed ? ' · 失败' : ''}`}
              aria-label={`${item.label}${item.failed ? '（失败）' : ''}`}
              onClick={() => jump(item)}
              style={{ top: `${positions.get(item.event_id)?.top ?? 0}%` }}
              className={`absolute left-1/2 h-2 w-2.5 -translate-x-1/2 rounded-[2px] transition-all hover:w-3.5 ${barClassName(item, activeKey === item.event_id)}`}
            />
          ))}
        </div>
        {failedCount > 0 ? (
          <span className="shrink-0 font-mono text-[9px] text-red-500" aria-label={`${failedCount} 个失败事件`}>
            {failedCount}
          </span>
        ) : null}
      </nav>
    )
  }

  // 展开态：纵向泳道面板
  const laneColumns = LANE_ORDER.map((lane) => ({
    lane,
    list: runItems.filter((item) => item.lane === lane),
  }))

  return (
    <nav aria-label="运行轨迹" className="flex w-64 shrink-0 flex-col overflow-hidden border-r border-stone-100 bg-white/80">
      <div className="flex items-center gap-1 border-b border-stone-100 px-2 py-1.5">
        <button
          type="button"
          onClick={toggleExpanded}
          aria-label="折叠运行轨迹"
          title="折叠运行轨迹"
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-stone-400 hover:bg-stone-100 hover:text-stone-700"
        >
          <DownOutlined className="text-[10px]" />
        </button>
        <span className="flex-1 text-[11px] font-medium text-stone-700">运行轨迹</span>
        <div className="flex gap-0.5">
          {(Object.keys(SCALE_LABEL) as ScaleMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setScale(mode)}
              aria-pressed={scale === mode}
              title={SCALE_HINT[mode]}
              className={`rounded px-1.5 py-0.5 text-[10px] transition-colors ${
                scale === mode ? 'bg-blue-50 font-medium text-blue-700' : 'text-stone-400 hover:text-stone-700'
              }`}
            >
              {SCALE_LABEL[mode]}
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1" onClick={scrollTrackToRatio}>
        {inputs.length > 0 ? (
          <div className="flex min-h-0 w-14 shrink-0 flex-col">
            <div className="shrink-0 border-b border-stone-100 py-0.5 text-center text-[10px] text-stone-400">输入</div>
            <div className="relative min-h-0 flex-1 border-r border-stone-50">
              {inputs.map((input, index) => (
                <div
                  key={input.id}
                  title={input.content}
                  className="absolute left-1/2 w-3/4 -translate-x-1/2 rounded-[2px] bg-stone-300"
                  style={{ top: `${index * 6}%`, height: '3%' }}
                />
              ))}
            </div>
          </div>
        ) : null}
        {laneColumns.map(({ lane, list }) => (
          <div key={lane} className="flex min-h-0 flex-1 flex-col">
            <div className="shrink-0 border-b border-stone-100 py-0.5 text-center text-[10px] text-stone-400">
              {LANE_TITLE[lane]}
            </div>
            <div className="relative min-h-0 flex-1 border-r border-stone-50">
              {list.map((item) => (
                <button
                  key={item.event_id}
                  type="button"
                  title={`${item.label}${item.tool_name ? ` · ${item.tool_name}` : ''}${item.failed ? ' · 失败' : ''}`}
                  aria-label={`${item.label}${item.failed ? '（失败）' : ''}`}
                  aria-current={activeKey === item.event_id ? 'true' : undefined}
                  onClick={() => jump(item)}
                  style={{ top: `${positions.get(item.event_id)?.top ?? 0}%`, height: `${positions.get(item.event_id)?.height ?? 3}%` }}
                  className={`absolute left-1/2 w-3/4 -translate-x-1/2 rounded-[2px] transition-opacity hover:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 ${barClassName(item, activeKey === item.event_id)} ${activeKey === item.event_id ? 'opacity-100' : 'opacity-70'}`}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </nav>
  )
}

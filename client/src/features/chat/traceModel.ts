// 统一事件契约 -> 轨迹投影：泳道、阶段、状态与颜色语义。
// 会话左侧时间轴与 Trace/审计查看器复用同一套映射，避免两套索引并存。
import type { RunIndexItem } from '../../api/types'

export interface TimelineInput {
  id: string
  content: string
  createdAt: string
}

export type TimelineEvent = RunIndexItem & {
  /** Synthetic user-input events point back to the message in the chat. */
  source?: 'run' | 'input'
}

export type Lane =
  | 'talk'
  | 'plan'
  | 'model'
  | 'execute'
  | 'approval'
  | 'review'
  | 'final'
  | 'system'

// 时间从上到下，泳道横向并列；顺序即横向排布顺序。
export const LANE_ORDER: Lane[] = [
  'talk',
  'plan',
  'model',
  'execute',
  'approval',
  'review',
  'final',
  'system',
]

export const LANE_TITLE: Record<Lane, string> = {
  talk: '对话',
  plan: '计划',
  model: '模型',
  execute: '工具',
  approval: '审批',
  review: '审查',
  final: '终答',
  system: '系统',
}

const LANE_OF_TYPE: Record<string, Lane> = {
  'user.input': 'talk',
  'assistant.delta': 'talk',
  'assistant.commentary': 'talk',
  'plan.created': 'plan',
  'plan.skipped': 'plan',
  'model.started': 'model',
  'model.completed': 'model',
  'model.retrying': 'model',
  'model.protocol_retrying': 'model',
  'model.heartbeat': 'model',
  'tool.requested': 'execute',
  'tool.started': 'execute',
  'tool.completed': 'execute',
  'tool.failed': 'execute',
  'policy.violation': 'execute',
  'approval.required': 'approval',
  'approval.resolved': 'approval',
  'review.started': 'review',
  'review.completed': 'review',
  'message.completed': 'final',
  'task.completed': 'final',
  'task.cancelled': 'final',
  'task.failed': 'final',
  'task.interrupted': 'final',
  'task.blocked': 'final',
  'task.cancel_requested': 'final',
  'agent.state': 'system',
  'task.queued': 'system',
  'task.started': 'system',
  'task.snapshot': 'system',
}

/** 事件类型 -> 泳道；未知类型归入 system，绝不返回 undefined。 */
export function laneOf(type: string): Lane {
  return LANE_OF_TYPE[type] ?? 'system'
}

/** Return the first usable timestamp for a timeline item. */
export function eventTimeOf(item: Pick<RunIndexItem, 'timestamp' | 'started_at' | 'ended_at'> & { createdAt?: string }): number {
  for (const value of [item.started_at, item.timestamp, item.createdAt, item.ended_at]) {
    if (!value) continue
    const time = new Date(value).getTime()
    if (!Number.isNaN(time)) return time
  }
  return Number.NaN
}

/** Event occurrence time for chronological lists. Intervals still use eventTimeOf(). */
export function eventOrderTimeOf(item: Pick<RunIndexItem, 'timestamp' | 'started_at' | 'ended_at'> & { createdAt?: string }): number {
  for (const value of [item.timestamp, item.started_at, item.createdAt, item.ended_at]) {
    if (!value) continue
    const time = new Date(value).getTime()
    if (!Number.isNaN(time)) return time
  }
  return Number.NaN
}

/** Stable chronological ordering shared by the chat navigator and audit view. */
export function sortTimelineItems<T extends Pick<RunIndexItem, 'timestamp' | 'started_at' | 'ended_at'>>(items: T[]): T[] {
  return items
    .map((item, index) => ({ item, index, time: eventOrderTimeOf(item) }))
    .sort((a, b) => {
      if (!Number.isNaN(a.time) && !Number.isNaN(b.time) && a.time !== b.time) return a.time - b.time
      if (!Number.isNaN(a.time)) return -1
      if (!Number.isNaN(b.time)) return 1
      return a.index - b.index
    })
    .map(({ item }) => item)
}

export interface TimelineBounds {
  start: number
  end: number
  span: number
}

/** Compute a shared time domain without imposing a per-event visual duration. */
export function timelineBounds(items: Array<Pick<RunIndexItem, 'timestamp' | 'started_at' | 'ended_at'> & { createdAt?: string }>): TimelineBounds {
  const starts = items.map(eventTimeOf).filter((value) => !Number.isNaN(value))
  if (starts.length === 0) return { start: 0, end: 1, span: 1 }
  const explicitEnds = items
    .map((item) => item.ended_at ? new Date(item.ended_at).getTime() : Number.NaN)
    .filter((value) => !Number.isNaN(value))
  const start = Math.min(...starts)
  const end = Math.max(start, ...starts, ...explicitEnds)
  return { start, end, span: Math.max(1, end - start) }
}

/** Resolve an event's vertical interval from real ends or the next event start. */
export function timelineRange(
  items: Array<Pick<RunIndexItem, 'timestamp' | 'started_at' | 'ended_at'> & { createdAt?: string }>,
  index: number,
  bounds: TimelineBounds,
): { top: number; height: number; point: boolean } {
  const current = items[index]
  const start = current ? eventTimeOf(current) : bounds.start
  const explicitEnd = current?.ended_at ? new Date(current.ended_at).getTime() : Number.NaN
  const nextStart = index + 1 < items.length ? eventTimeOf(items[index + 1]!) : Number.NaN
  const end = Number.isFinite(explicitEnd) && explicitEnd > start
    ? explicitEnd
    : Number.isFinite(nextStart) && nextStart > start
      ? nextStart
      : bounds.end
  const safeStart = Number.isFinite(start) ? Math.min(bounds.end, Math.max(bounds.start, start)) : bounds.start
  const safeEnd = Math.max(safeStart, Math.min(bounds.end, end))
  return {
    top: ((safeStart - bounds.start) / bounds.span) * 100,
    height: ((safeEnd - safeStart) / bounds.span) * 100,
    point: safeEnd <= safeStart,
  }
}

/** Convert a chat message into the same event shape as run-index entries. */
export function inputTimelineEvent(input: TimelineInput): TimelineEvent {
  return {
    event_id: `input:${input.id}`,
    type: 'user.input',
    timestamp: input.createdAt,
    label: '用户输入',
    status: 'completed',
    text: input.content,
    message_id: input.id,
    source: 'input',
  }
}

/** Events useful for jumping in the conversation; audit keeps the full stream. */
const MAIN_TIMELINE_TYPES = new Set([
  'user.input',
  'assistant.commentary',
  'plan.created',
  'plan.skipped',
  'model.started',
  'tool.requested',
  'tool.failed',
  'approval.required',
  'review.started',
  'review.completed',
  'message.completed',
  'task.completed',
  'task.cancelled',
  'task.failed',
  'task.interrupted',
  'task.blocked',
])

export function isMainTimelineEvent(item: Pick<RunIndexItem, 'type'>): boolean {
  return MAIN_TIMELINE_TYPES.has(item.type)
}

const FAILED_STATUSES = new Set(['failed', 'blocked', 'interrupted', 'needs_fix'])

export function isFailed(item: Pick<RunIndexItem, 'type' | 'status'>): boolean {
  return item.type === 'tool.failed' || FAILED_STATUSES.has(item.status ?? '')
}

export function isRunning(item: Pick<RunIndexItem, 'type'>): boolean {
  return item.type === 'tool.started' || item.type === 'approval.required'
}

/** 颜色/状态语义：失败红、进行中蓝（脉冲）、选中深蓝、默认灰。 */
export function barClass(item: Pick<RunIndexItem, 'type' | 'status'>, active: boolean): string {
  if (isFailed(item)) return 'bg-red-400 ring-1 ring-red-500'
  if (item.status === 'needs_fix') return 'bg-amber-400'
  if (isRunning(item)) return 'animate-pulse bg-blue-400'
  return active ? 'bg-blue-600' : 'bg-stone-400'
}

/** 事件类型的中文标签，供时间轴 tooltip 与审计表复用。 */
export function eventLabel(type: string): string {
  const labels: Record<string, string> = {
    'user.input': '用户输入',
    'assistant.delta': '模型输出',
    'assistant.commentary': '过程更新',
    'plan.created': '计划已创建',
    'plan.skipped': '直接回答',
    'model.started': '模型请求',
    'model.completed': '模型完成',
    'model.retrying': '模型重试',
    'model.protocol_retrying': '协议重试',
    'model.heartbeat': '模型心跳',
    'tool.requested': '工具请求',
    'tool.started': '工具开始',
    'tool.completed': '工具完成',
    'tool.failed': '工具失败',
    'policy.violation': '策略拦截',
    'approval.required': '等待审批',
    'approval.resolved': '审批完成',
    'review.started': '开始审查',
    'review.completed': '审查完成',
    'message.completed': '最终回答',
    'task.completed': '运行完成',
    'task.cancelled': '运行已取消',
    'task.failed': '运行失败',
    'task.interrupted': '运行已中断',
    'task.blocked': '运行受阻',
    'task.cancel_requested': '正在停止',
    'task.queued': '排队中',
    'task.started': '运行开始',
    'agent.state': 'Agent 状态',
  }
  return labels[type] ?? type
}

/** 事件区间真实耗时（悬停摘要）；无起止时间或非正区间返回空串。 */
export function durationLabel(item: Pick<RunIndexItem, 'started_at' | 'ended_at'>): string {
  const start = item.started_at ? new Date(item.started_at).getTime() : Number.NaN
  const end = item.ended_at ? new Date(item.ended_at).getTime() : Number.NaN
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return ''
  const ms = end - start
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

/** Run 状态的中文标签，供轨迹条头部复用。 */
export function runStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断',
    blocked: '受阻',
    queued: '排队中',
    needs_fix: '待修复',
  }
  return labels[status] ?? status
}

export interface TimelineGroup {
  key: string
  lane: Lane
  type: string
  toolNames: string[]
  count: number
  start: number
  end: number
  failed: boolean
  running: boolean
  /** 代表性事件，用于点击跳转。 */
  sample: TimelineEvent
}

const MAX_CONSECUTIVE_TOOLS = 24

/**
 * 把时间轴事件折叠成可视化分组：连续的工具事件合并成一个区段（保留调用数量），
 * 分组过多时再按序聚合为桶，避免长 Run 渲染数百个 DOM 节点（§8.1 虚拟化/区段聚合）。
 */
export function groupTimelineItems(entries: TimelineEvent[], maxBuckets = 160): TimelineGroup[] {
  const collapsed: TimelineGroup[] = []
  for (const entry of entries) {
    const lane = laneOf(entry.type)
    const time = eventTimeOf(entry)
    const prev = collapsed[collapsed.length - 1]
    if (prev && prev.lane === 'execute' && lane === 'execute' && prev.count < MAX_CONSECUTIVE_TOOLS) {
      prev.count += 1
      prev.end = Math.max(prev.end, time)
      prev.failed = prev.failed || isFailed(entry)
      prev.running = prev.running || isRunning(entry)
      if (entry.tool_name && !prev.toolNames.includes(entry.tool_name)) {
        prev.toolNames.push(entry.tool_name)
      }
    } else {
      collapsed.push({
        key: entry.event_id,
        lane,
        type: entry.type,
        toolNames: entry.tool_name ? [entry.tool_name] : [],
        count: 1,
        start: time,
        end: time,
        failed: isFailed(entry),
        running: isRunning(entry),
        sample: entry,
      })
    }
  }

  if (collapsed.length <= maxBuckets) return collapsed

  const buckets: TimelineGroup[] = []
  const per = Math.ceil(collapsed.length / maxBuckets)
  for (let i = 0; i < collapsed.length; i += per) {
    const chunk = collapsed.slice(i, i + per)
    const first = chunk[0]!
    const last = chunk[chunk.length - 1]!
    buckets.push({
      key: first.key,
      lane: first.lane,
      type: 'bucket',
      toolNames: [...new Set(chunk.flatMap((group) => group.toolNames))].slice(0, 6),
      count: chunk.reduce((sum, group) => sum + group.count, 0),
      start: first.start,
      end: last.end,
      failed: chunk.some((group) => group.failed),
      running: chunk.some((group) => group.running),
      sample: first.sample,
    })
  }
  return buckets
}

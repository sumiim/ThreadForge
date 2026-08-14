// 统一事件契约 -> 轨迹投影：泳道、阶段、状态与颜色语义。
// 会话左侧时间轴与 Trace/审计查看器复用同一套映射，避免两套索引并存。
import type { RunIndexItem } from '../../api/types'

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

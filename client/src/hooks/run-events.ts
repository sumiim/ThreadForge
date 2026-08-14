// 统一事件契约的合并语义：同一 event_id 幂等、乱序按 sequence 合并、
// SSE 重放不重复、刷新/重连不归零、不把 incomplete 误判为成功。
import type { RunEventEnvelope, RunIndexItem } from '../api/types'

export const TERMINAL_EVENT_TYPES = new Set([
  'task.completed',
  'task.cancelled',
  'task.failed',
  'task.interrupted',
  'task.blocked',
])

export function isTerminalEventType(type: string): boolean {
  return TERMINAL_EVENT_TYPES.has(type)
}

/** 事件类型 -> 终态状态名（非终态返回 null）。 */
export function terminalStatusOf(type: string): string | null {
  return isTerminalEventType(type) ? type.slice('task.'.length) : null
}

/**
 * 按 event_id 幂等合并事件信封，并按 sequence 升序稳定排序。
 *
 * - 同一 event_id 只保留一次（SSE 重放 / 快照重复不产生重复条目）。
 * - 乱序到达的事件按 sequence 归位。
 * - 空 incoming 原样返回 existing，刷新/重连不会把状态清零。
 */
export function mergeEvents(
  existing: RunEventEnvelope[],
  incoming: RunEventEnvelope[],
): RunEventEnvelope[] {
  if (incoming.length === 0) return existing
  const byId = new Map<string, RunEventEnvelope>()
  for (const event of existing) byId.set(event.event_id, event)
  for (const event of incoming) byId.set(event.event_id, event)
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence)
}

/**
 * 按 event_id 幂等合并 run_index 条目，并按时间戳稳定排序。
 * run_index 快照不携带 sequence，以 timestamp 作为顺序代理；同一 event_id
 * 以 incoming 覆盖（终态/更新字段以最新为准），不产生重复。
 */
export function mergeRunIndex(
  existing: RunIndexItem[],
  incoming: RunIndexItem[],
): RunIndexItem[] {
  if (incoming.length === 0) return existing
  const byId = new Map<string, RunIndexItem>()
  for (const item of existing) byId.set(item.event_id, item)
  for (const item of incoming) byId.set(item.event_id, item)
  return [...byId.values()].sort((a, b) => {
    if (a.timestamp < b.timestamp) return -1
    if (a.timestamp > b.timestamp) return 1
    return 0
  })
}

/**
 * 从已合并的事件流推导终态。返回 null 表示尚未收敛到终态，调用方不得把
 * incomplete 当成成功（例如模型请求尚未返回时不得显示“完成”）。
 */
export function terminalStatus(
  events: Array<Pick<RunEventEnvelope, 'type'>>,
): string | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const status = terminalStatusOf(events[i].type)
    if (status !== null) return status
  }
  return null
}

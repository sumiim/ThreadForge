import type { SessionTask, ToolCall } from '../api/types'

export type HistoryStatus = 'loading' | 'error' | 'loaded'

export function resolveHistoryStatus(
  sessionId: string | null,
  loadedSessions: ReadonlySet<string>,
  failedSessions: ReadonlySet<string>,
): HistoryStatus {
  if (!sessionId || loadedSessions.has(sessionId)) return 'loaded'
  return failedSessions.has(sessionId) ? 'error' : 'loading'
}

export function historyAllowsSending(draft: boolean, status: HistoryStatus): boolean {
  return draft || status === 'loaded'
}

// api-server returns session tasks newest first (mtime DESC).
export function getLatestTask(tasks: SessionTask[]): SessionTask | undefined {
  return tasks[0]
}

export function isInternalReviewDiagnostic(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const text = value.trim()
  return /^(?:status\s*:\s*(?:pass|needs_fix)\b|\{\s*["']?status["']?\s*:\s*["']?(?:pass|needs_fix)\b)/i.test(text)
}

export function getFinalAnswer(data: Record<string, unknown>): string | null {
  const status = String(data.status ?? '')
  // Failed runs may carry internal review diagnostics. Only completed runs
  // can promote final_answer into the visible conversation.
  if (status && status !== 'completed') return null
  if (typeof data.final_answer !== 'string') return null
  const finalAnswer = data.final_answer.trim()
  return finalAnswer && !isInternalReviewDiagnostic(finalAnswer) ? data.final_answer : null
}

export function reconcileToolCalls(
  toolCalls: ToolCall[] | undefined,
  terminalStatus: string,
): ToolCall[] | undefined {
  if (!toolCalls) return undefined
  const completedNormally = terminalStatus === 'completed'
  return toolCalls.map<ToolCall>((tool) => {
    if (tool.status === 'running') {
      return completedNormally
        ? { ...tool, status: 'completed', result: tool.result ?? '工具已执行完成' }
        : { ...tool, status: 'error', result: tool.result ?? '任务已停止，工具未完成' }
    }
    if (tool.status === 'pending') {
      return { ...tool, status: 'rejected', result: tool.result ?? '任务已结束，工具未执行' }
    }
    return tool
  })
}

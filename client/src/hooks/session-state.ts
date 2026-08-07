import type { SessionTask, ToolCall } from '../api/types'

// api-server returns session tasks newest first (mtime DESC).
export function getLatestTask(tasks: SessionTask[]): SessionTask | undefined {
  return tasks[0]
}

export function getFinalAnswer(data: Record<string, unknown>): string | null {
  return typeof data.final_answer === 'string' && data.final_answer.length > 0
    ? data.final_answer
    : null
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

import type { SessionTask, ToolCall, ToolStatus } from '../api/types'

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

const modelFailureMessages: Record<string, string> = {
  model_rate_limited: '模型服务当前请求过多，已自动重试，请稍后再试。',
  model_timeout: '模型服务响应超时，已自动重试，请稍后再试。',
  model_connection_error: '无法稳定连接模型服务，已自动重试，请检查网络后再试。',
  model_server_error: '模型服务暂时不可用，已自动重试，请稍后再试。',
  model_auth_error: '模型服务认证失败，请在 Worker 中重新配置 API 密钥。',
  model_request_rejected: '模型服务拒绝了请求，请检查模型与推理强度配置。',
  model_response_invalid: '模型服务返回了无法解析的响应，请稍后再试。',
  model_provider_error: '模型服务返回错误，请检查供应商配置后再试。',
  model_call_failed: '模型调用失败，请稍后再试。',
}

const stopReasonMessages: Record<string, string> = {
  retry_limit_reached: '模型输出未通过执行协议校验，达到重试上限后停止。',
  review_retry_limit_reached: '审查阶段未能确认任务已经完成，达到重试上限后停止。',
  budget_exhausted: '本次运行已达到时间、步骤或令牌预算，请缩小任务范围后重试。',
  step_limit_reached: '本次运行已达到工具步骤上限，请缩小任务范围后重试。',
  approval_denied: '所需工具操作未获批准，运行已停止。',
  no_changes_to_review: '没有检测到可供审查的文件变更。',
  completion_gate_failed: '运行结果未满足计划中的全部完成条件，请根据当前进度重试。',
  user_cancelled: '已停止当前任务。',
}

export function terminalFailureMessage(data: Record<string, unknown>): string {
  const code = String(data.error_code ?? '')
  if (code && modelFailureMessages[code]) return modelFailureMessages[code]
  const stopReason = String(data.stop_reason ?? '')
  if (stopReason && stopReasonMessages[stopReason]) return stopReasonMessages[stopReason]
  const status = String(data.status ?? '')
  if (status === 'cancelled') return '已停止当前任务。'
  if (status === 'interrupted') return '运行因服务重启或连接中断而终止，请重新执行。'
  if (status === 'blocked') return '运行受阻，请查看运行事件中的具体原因后重试。'
  if (status === 'failed') {
    const detail = String(data.error_detail ?? '').trim()
    return detail ? `Agent 运行失败：${detail}` : 'Agent 运行失败，请稍后重试。'
  }
  return ''
}

interface ToolEventUpdate {
  id: string
  toolName: string
  status: ToolStatus
  args?: Record<string, unknown>
  result?: string
}

export function applyToolEvent(
  toolCalls: ToolCall[] | undefined,
  update: ToolEventUpdate,
): ToolCall[] | undefined {
  const current = toolCalls ?? []
  let index = update.id ? current.findIndex((tool) => tool.id === update.id) : -1
  if (index < 0 && !update.id && update.toolName) {
    for (let position = current.length - 1; position >= 0; position -= 1) {
      if (current[position].toolName === update.toolName) {
        index = position
        break
      }
    }
  }
  if (index < 0) {
    if (!update.id) return toolCalls
    return [...current, {
      id: update.id,
      toolName: update.toolName,
      status: update.status,
      args: update.args,
      result: update.result,
    }]
  }
  return current.map((tool, position) => position === index ? {
    ...tool,
    toolName: update.toolName || tool.toolName,
    status: update.status,
    args: update.args ?? tool.args,
    result: update.result ?? tool.result,
  } : tool)
}

export function reconcileToolCalls(
  toolCalls: ToolCall[] | undefined,
  terminalStatus: string,
): ToolCall[] | undefined {
  if (!toolCalls) return undefined
  const completedNormally = terminalStatus === 'completed'
  const cancelled = terminalStatus === 'cancelled' || terminalStatus === 'interrupted'
  return toolCalls.map<ToolCall>((tool) => {
    if (tool.status === 'running') {
      return completedNormally
        ? { ...tool, status: 'completed', result: tool.result ?? '工具已执行完成' }
        : cancelled
          ? { ...tool, status: 'rejected', result: tool.result ?? '任务已取消，工具未完成' }
        : { ...tool, status: 'error', result: tool.result ?? '任务已停止，工具未完成' }
    }
    if (tool.status === 'pending') {
      return { ...tool, status: 'rejected', result: tool.result ?? '任务已结束，工具未执行' }
    }
    return tool
  })
}

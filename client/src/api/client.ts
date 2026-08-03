// api-server 客户端：REST 调用 + SSE 事件流
// 后端统一经 vite 代理访问（/api -> http://127.0.0.1:8000），无需跨域
import type {
  PendingApproval,
  RunEventEnvelope,
  SessionDetail,
  SessionSummary,
  TaskQueued,
  TaskSnapshot,
  Workspace,
} from './types'

export class ApiError extends Error {
  code: string
  status: number
  details: Record<string, unknown>

  constructor(message: string, code: string, status: number, details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string; details?: Record<string, unknown> }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    })
  } catch {
    throw new ApiError('无法连接 api-server，请确认后端已启动', 'network_error', 0)
  }
  if (!response.ok) {
    let code = 'unknown_error'
    let message = `请求失败（HTTP ${response.status}）`
    let details: Record<string, unknown> = {}
    try {
      const body = (await response.json()) as ErrorBody
      if (body.error) {
        code = body.error.code ?? code
        message = body.error.message ?? message
        details = body.error.details ?? {}
      }
    } catch {
      // 非 JSON 错误体，保留默认信息
    }
    throw new ApiError(message, code, response.status, details)
  }
  return (await response.json()) as T
}

// 后端错误码 -> 前端可读文案
const errorText: Record<string, string> = {
  network_error: '无法连接 api-server，请确认后端已启动',
  model_not_configured: '后端未配置模型（PICO_OPENAI_API_KEY），无法运行 Agent',
  active_task_exists: '已有任务在运行，请等待完成或先停止',
  task_runner_unavailable: '任务运行器不可用，请稍后重试',
  session_not_found: '会话不存在，可能已被删除',
  task_not_found: '任务不存在',
  input_too_long: '任务描述过长，请精简后重试',
  approval_not_found: '审批请求不存在',
  approval_already_resolved: '该审批已完成',
  approval_expired: '审批已过期',
  approval_stale: '审批已失效（任务状态已变化）',
  persistence_unavailable: '后端存储不可用，请检查数据目录权限',
  not_ready: '后端尚未就绪，请稍后重试',
}

export function friendlyMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return errorText[error.code] ?? (error.message || '请求失败')
  }
  return error instanceof Error ? error.message : '请求失败'
}

// ---- REST -----------------------------------------------------------------

export function listWorkspaces(): Promise<{ items: Workspace[] }> {
  return request('/api/v1/workspaces')
}

export interface CreateSessionResult {
  session_id: string
  workspace_id: string
  title: string
  created_at: string
}

export function createSession(workspaceId: string, title?: string): Promise<CreateSessionResult> {
  return request('/api/v1/sessions', {
    method: 'POST',
    body: JSON.stringify({ workspace_id: workspaceId, title: title ?? null }),
  })
}

export function listSessions(limit = 50): Promise<{ items: SessionSummary[]; total: number }> {
  return request(`/api/v1/sessions?limit=${limit}`)
}

export function getSession(sessionId: string, messageLimit = 200): Promise<SessionDetail> {
  return request(`/api/v1/sessions/${encodeURIComponent(sessionId)}?message_limit=${messageLimit}`)
}

export function createTask(
  sessionId: string,
  input: string,
  maxSteps?: number,
): Promise<TaskQueued> {
  return request('/api/v1/tasks', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, input, max_steps: maxSteps ?? null }),
  })
}

export function getTask(taskId: string): Promise<TaskSnapshot> {
  return request(`/api/v1/tasks/${encodeURIComponent(taskId)}`)
}

export function cancelTask(taskId: string): Promise<TaskSnapshot> {
  return request(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
}

export function resolveApproval(
  taskId: string,
  approvalId: string,
  decision: 'approved' | 'rejected',
): Promise<{ approval_id: string; task_id: string; status: string; decision: string }> {
  return request(`/api/v1/tasks/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}`, {
    method: 'POST',
    body: JSON.stringify({ decision }),
  })
}

export interface ArtifactItem {
  name: string
  size?: number
  updated_at?: string
}

export function listArtifacts(runId: string): Promise<{ run_id: string; items: ArtifactItem[] }> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts`)
}

// 制品返回原始文本（trace 为 NDJSON，task_state / report 为 JSON）
export async function getArtifactText(runId: string, name: string): Promise<string> {
  let response: Response
  try {
    response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`)
  } catch {
    throw new ApiError('无法连接 api-server，请确认后端已启动', 'network_error', 0)
  }
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    try {
      const body = (await response.json()) as ErrorBody
      message = body.error?.message ?? message
    } catch {
      // ignore
    }
    throw new ApiError(message, 'artifact_unavailable', response.status)
  }
  return response.text()
}

// ---- SSE ------------------------------------------------------------------

// 事件流帧头自带 event 名（task.snapshot / tool.started / ...），
// 用 EventSource 的命名事件监听；断开时浏览器自动重连，重连后后端先补发 task.snapshot
export function openTaskEventStream(taskId: string): EventSource {
  return new EventSource(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`)
}

export function parseEventFrame(raw: string): RunEventEnvelope | null {
  try {
    return JSON.parse(raw) as RunEventEnvelope
  } catch {
    return null
  }
}

export type { PendingApproval, RunEventEnvelope }

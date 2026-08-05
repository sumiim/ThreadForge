// api-server 客户端：REST 调用 + SSE 事件流
// 后端统一经 vite 代理访问（/api -> http://127.0.0.1:8000），无需跨域
import type {
  AuthStatus,
  Device,
  McpServerMetadata,
  PendingApproval,
  RunEventEnvelope,
  RuntimeConfig,
  SessionDetail,
  SessionSummary,
  SkillMetadata,
  TaskQueued,
  TaskSnapshot,
  Workspace,
  WorkspaceSelectionRequest,
  WorkerReleaseManifest,
} from './types'
import { apiUrl } from './base-url.ts'

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
    const method = (init?.method ?? 'GET').toUpperCase()
    const headers = new Headers(init?.headers)
    if (init?.body) headers.set('Content-Type', 'application/json')
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-ThreadForge-CSRF', '1')
    response = await fetch(apiUrl(path), {
      ...init,
      credentials: 'include',
      headers,
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
  model_not_configured: '当前执行环境未配置模型，请先完成模型设置',
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
  authentication_required: '登录状态已失效，请重新使用 GitHub 登录',
  authorization_denied: '当前 GitHub 账户没有访问权限',
  oauth_state_invalid: '登录请求已失效，请重新登录',
  oauth_provider_error: 'GitHub 登录服务暂时不可用，请稍后重试',
  device_not_found: '本地 Worker 设备不存在',
  pairing_code_invalid: '配对码无效或已过期，请重新生成',
  worker_offline: '所选本地 Worker 已离线',
  worker_capability_unavailable: '本地 Worker 版本过旧，请更新后重试',
  worker_command_pending: '该设备已有目录选择请求等待处理',
  worker_command_failed: '本地 Worker 未能完成请求，请检查配置后重试',
  worker_protocol_error: '本地 Worker 协议错误，请检查版本',
  worker_release_unavailable: 'Worker 安装包暂时不可用，请稍后重试',
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

export function getAuthStatus(): Promise<AuthStatus> {
  return request('/api/v1/auth/status')
}

export function githubLoginUrl(): string {
  return apiUrl('/api/v1/auth/github/start')
}

export function logout(): Promise<{ status: string }> {
  return request('/api/v1/auth/logout', { method: 'POST' })
}

export function listDevices(): Promise<{ items: Device[] }> {
  return request('/api/v1/devices')
}

export function createPairingCode(): Promise<{ code: string; expires_in_seconds: number }> {
  return request('/api/v1/devices/pairing-codes', { method: 'POST' })
}

export function revokeDevice(deviceId: string): Promise<{ status: string; device_id: string }> {
  return request(`/api/v1/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' })
}

export function requestWorkspaceSelection(deviceId: string): Promise<WorkspaceSelectionRequest> {
  return request(`/api/v1/devices/${encodeURIComponent(deviceId)}/workspace-selection-requests`, {
    method: 'POST',
  })
}

export function getWorkspaceSelection(
  deviceId: string,
  requestId: string,
): Promise<WorkspaceSelectionRequest> {
  return request(
    `/api/v1/devices/${encodeURIComponent(deviceId)}/workspace-selection-requests/${encodeURIComponent(requestId)}`,
  )
}

export function configureWorkerModel(
  deviceId: string,
  config: { base_url: string; api_key: string; model: string },
): Promise<{ status: string; model: string }> {
  return request(`/api/v1/devices/${encodeURIComponent(deviceId)}/model-config`, {
    method: 'PUT',
    body: JSON.stringify(config),
  })
}

export function getLatestWorkerRelease(): Promise<WorkerReleaseManifest> {
  return request('/api/v1/worker/releases/latest')
}

export async function downloadWorkerRelease(
  platformName: string,
  onProgress: (received: number, total: number) => void,
): Promise<{ blob: Blob; filename: string }> {
  const response = await fetch(
    apiUrl(`/api/v1/worker/releases/download/${encodeURIComponent(platformName)}`),
    { credentials: 'include' },
  )
  if (!response.ok) {
    let message = `下载失败（HTTP ${response.status}）`
    try {
      const body = (await response.json()) as ErrorBody
      message = body.error?.message ?? message
    } catch {
      // Keep the fallback for non-JSON proxy errors.
    }
    throw new ApiError(message, 'worker_release_unavailable', response.status)
  }
  const total = Number(response.headers.get('Content-Length') ?? 0)
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? 'threadforge-worker.exe'
  if (!response.body) {
    const blob = await response.blob()
    onProgress(blob.size, total || blob.size)
    return { blob, filename }
  }
  const reader = response.body.getReader()
  const chunks: ArrayBuffer[] = []
  let received = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(Uint8Array.from(value).buffer)
    received += value.byteLength
    onProgress(received, total)
  }
  return { blob: new Blob(chunks, { type: 'application/octet-stream' }), filename }
}

export function getRuntimeConfig(): Promise<RuntimeConfig> {
  return request('/api/v1/config')
}

export function listSkills(): Promise<{ items: SkillMetadata[] }> {
  return request('/api/v1/skills')
}

export function listMcpServers(): Promise<{ items: McpServerMetadata[] }> {
  return request('/api/v1/mcp/servers')
}

export interface CreateSessionResult {
  session_id: string
  workspace_id: string
  title: string
  created_at: string
  execution_environment: string
  device_id: string
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
    response = await fetch(
      apiUrl(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`),
      { credentials: 'include' },
    )
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
  return new EventSource(apiUrl(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`), {
    withCredentials: true,
  })
}

export function parseEventFrame(raw: string): RunEventEnvelope | null {
  try {
    return JSON.parse(raw) as RunEventEnvelope
  } catch {
    return null
  }
}

export type { PendingApproval, RunEventEnvelope }

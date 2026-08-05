// 数据契约：与 api-server 的 REST / SSE 事件对齐（经 vite /api 代理访问）

// ---- 后端 REST 实体 ------------------------------------------------------

export interface Workspace {
  workspace_id: string
  name: string
  display_path: string
  available: boolean
  is_git: boolean
  execution_environment: string
  container_sandbox_enabled: boolean
  device_id?: string
  device_name?: string
  device_platform?: string
  model?: string
  model_configured?: boolean
}

export interface WorkerWorkspace {
  workspace_id: string
  name: string
  is_git: boolean
}

export interface Device {
  device_id: string
  name: string
  online: boolean
  model: string
  model_configured: boolean
  version: string
  protocol_version: number
  platform: string
  architecture: string
  compatible: boolean
  capabilities: string[]
  created_at: string
  last_seen_at: string
  workspaces: WorkerWorkspace[]
}

/** 在线 Worker 池条目；用于未来的多 Worker 路由/故障转移。 */
export interface OnlineWorker extends Pick<
  Device,
  | 'device_id'
  | 'name'
  | 'online'
  | 'version'
  | 'protocol_version'
  | 'platform'
  | 'architecture'
  | 'compatible'
  | 'capabilities'
  | 'workspaces'
> {
  worker_id: string
}

export type WorkerRoutingMode = 'single' | 'parallel' | 'failover'

export interface WorkerConnectionPlan {
  mode: WorkerRoutingMode
  worker_ids: string[]
}

export interface WorkspaceSelectionRequest {
  request_id: string
  device_id: string
  status: 'pending' | 'completed' | 'cancelled' | 'failed' | 'expired'
  workspace_id: string | null
  error: string | null
  created_at: string
  expires_at: string
}

export interface WorkerReleaseArtifact {
  filename: string
  size: number
  sha256: string
}

export interface WorkerReleaseManifest {
  schema_version: 1
  channel: 'stable'
  version: string
  protocol_version: number
  minimum_server_protocol: number
  published_at: string
  platforms: Record<string, WorkerReleaseArtifact>
  signature: { algorithm: 'ed25519'; value: string }
}

export interface RuntimeConfig {
  model: string
  model_configured: boolean
  execution_environment: string
  container_sandbox_enabled: boolean
  identity_mode: 'single_owner_instance' | 'github_oauth'
  multi_user_enabled: boolean
}

export interface AuthUser {
  owner_id: string
  subject: string
  login: string
  name: string
  avatar_url: string
}

export interface AuthStatus {
  identity_mode: 'single_owner_instance' | 'github_oauth'
  multi_user_enabled: boolean
  authentication_required: boolean
  authenticated: boolean
  user: AuthUser | null
}

export interface SkillMetadata {
  id: string
  name: string
  description: string
  status: 'planned'
  available: false
}

export interface McpServerMetadata {
  id: string
  name: string
  description: string
  status: 'not_configured'
  connected: false
}

// GET /api/v1/sessions 列表项
export interface SessionSummary {
  session_id: string
  workspace_id: string
  title: string
  created_at: string
  message_total: number
  execution_environment: string
  device_id: string
}

export interface SessionMessage {
  role: 'user' | 'assistant'
  name: string
  content: string
  created_at: string
}

export interface SessionTask {
  task_id: string
  run_id: string
  status: string
  input: string
  final_answer: string | null
  stop_reason: string | null
  created_at: string
  updated_at: string
}

// GET /api/v1/sessions/{id} 会话详情
export interface SessionDetail {
  session_id: string
  workspace_id: string
  title: string
  created_at: string
  message_total: number
  has_more: boolean
  messages: SessionMessage[]
  task_total: number
  tasks: SessionTask[]
  execution_environment: string
  device_id: string
}

// 审批请求（task.pending_approval / approval.required）
export interface PendingApproval {
  approval_id: string
  tool_call_id: string
  tool_name: string
  args_preview: Record<string, unknown>
  created_at: string
  expires_at: string
}

// GET /api/v1/tasks/{id} 任务快照
export interface TaskSnapshot {
  task_id: string
  run_id: string
  session_id: string
  workspace_id: string
  status: string
  input: string
  final_answer: string | null
  stop_reason: string | null
  attempts: number | null
  tool_steps: number | null
  pending_approval: PendingApproval | null
  created_at: string
  updated_at: string
  execution_environment: string
  container_sandbox_enabled: boolean
  device_id: string
}

// POST /api/v1/tasks 响应（202）
export interface TaskQueued {
  task_id: string
  run_id: string
  session_id: string
  status: string
  events_url: string
}

// ---- SSE 事件流 ----------------------------------------------------------

// 每个 SSE 帧的 data 都是一个事件信封；type 事件名同帧头 event 字段
export interface RunEventEnvelope {
  event_id: string
  sequence: number
  type: string
  task_id: string
  run_id: string
  timestamp: string
  data: Record<string, unknown>
}

export type RunEvent = RunEventEnvelope & {
  data: {
    final_answer?: string
    stop_reason?: string
    text?: string
    tool_call_id?: string
    tool_name?: string
    tool_status?: string
    tool_error_code?: string
    affected_paths?: string[]
    approval_id?: string
    args_preview?: Record<string, unknown>
    created_at?: string
    expires_at?: string
    status?: string
    decision?: string
    [key: string]: unknown
  }
}

// ---- 前端 UI 模型 ---------------------------------------------------------

export type ToolStatus = 'pending' | 'running' | 'completed' | 'rejected' | 'error'

export interface ToolCall {
  id: string
  toolName: string
  args?: Record<string, unknown>
  status: ToolStatus
  result?: string
  requiresApproval?: boolean
  /** 审批事件里的 approval_id（approval.required 填充，审批/拒绝时回传） */
  approvalId?: string
  /** 归属任务 id（审批决策需要） */
  taskId?: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string // ISO 时间
  toolCalls?: ToolCall[]
  status?: 'streaming' | 'done' // assistant 消息运行状态
}

export interface Session {
  id: string
  title: string
  createdAt: string
  /** 后端 workspace_id，显示路径由 Workspace.display_path 映射 */
  workspaceId: string
  executionEnvironment?: string
  deviceId?: string
  model: string
  messages: Message[]
  lastTaskId?: string
  lastRunId?: string
}

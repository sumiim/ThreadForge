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
}

export interface RuntimeConfig {
  model: string
  model_configured: boolean
  execution_environment: string
  container_sandbox_enabled: boolean
  identity_mode: 'single_owner_instance'
  multi_user_enabled: boolean
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
  model: string
  messages: Message[]
  lastTaskId?: string
  lastRunId?: string
}

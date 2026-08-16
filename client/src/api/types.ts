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
  display_name?: string
  display_name_source?: 'auto' | 'user'
  display_name_updated_at?: string
  device_display_name?: string
  device_display_name_source?: 'auto' | 'user'
  device_display_name_updated_at?: string
  model_capabilities?: ModelCapabilities
}

export type ReasoningEffort = 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh'
export type PermissionMode = 'plan' | 'acceptEdits' | 'default' | 'bypass'

export interface ModelCapability {
  id: string
  display_name: string
  reasoning_efforts: ReasoningEffort[]
}

export interface ModelCapabilities {
  provider: string
  models: ModelCapability[]
}

export interface WorkerWorkspace {
  workspace_id: string
  name: string
  is_git: boolean
  display_name_source?: 'auto' | 'user'
  display_name_updated_at?: string
}

export interface Device {
  device_id: string
  name: string
  online: boolean
  model: string
  model_provider?: string
  model_configured: boolean
  version: string
  protocol_version: number
  platform: string
  architecture: string
  compatible: boolean
  capabilities: string[]
  display_name?: string
  display_name_source?: 'auto' | 'user'
  orchestration_backend?: string
  model_capabilities?: ModelCapabilities
  update_status?: WorkerUpdateStatus
  created_at: string
  last_seen_at: string
  display_name_updated_at?: string
  workspaces: WorkerWorkspace[]
}

export interface WorkerUpdateStatus {
  status: 'checking' | 'downloading' | 'retrying' | 'installing' | 'current' | 'failed' | 'auth_failed' | 'unsupported' | ''
  current_version: string
  target_version: string
  downloaded_bytes: number
  total_bytes: number
  bytes_per_second?: number
  retry_count?: number
  error: string
  updated_at: string
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
  model_provider?: string
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
  updated_at?: string
  display_name_source?: 'auto' | 'user'
  display_name_updated_at?: string
  has_started?: boolean
  message_total: number
  execution_environment: string
  device_id: string
}

export interface SessionMessage {
  role: 'user' | 'assistant' | 'tool'
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
  error_stage?: string
  error_code?: string
  error_retryable?: boolean
  error_attempts?: number
  created_at: string
  updated_at: string
  model_id?: string
  reasoning_effort?: ReasoningEffort
  run_index?: RunIndexItem[]
}

// GET /api/v1/sessions/{id} 会话详情
export interface SessionDetail {
  session_id: string
  workspace_id: string
  title: string
  created_at: string
  updated_at?: string
  display_name_source?: 'auto' | 'user'
  display_name_updated_at?: string
  has_started?: boolean
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
  error_stage?: string
  error_code?: string
  error_retryable?: boolean
  error_attempts?: number
  attempts: number | null
  tool_steps: number | null
  phase?: string
  next_step?: string
  checklist?: string[]
  done_when?: string[]
  completed_items?: string[]
  read_files?: number
  max_tool_steps?: number
  max_read_files?: number
  max_total_steps?: number
  pending_approval: PendingApproval | null
  created_at: string
  updated_at: string
  execution_environment: string
  container_sandbox_enabled: boolean
  device_id: string
  model_id?: string
  reasoning_effort?: ReasoningEffort
  run_index?: RunIndexItem[]
}

export interface RunIndexItem {
  event_id: string
  type: string
  timestamp: string
  label: string
  /** Chat message id for synthetic user-input timeline entries. */
  message_id?: string
  tool_name?: string
  tool_call_id?: string
  /** 只读工具脱敏后的参数预览（如 list_files 的 path、read_file 的行区间）。 */
  args_preview?: Record<string, unknown>
  intent?: string
  step_count?: number
  status?: string
  run_id?: string
  /** 统一事件契约：粗粒度分层（talk/plan/execute/approval/review/final/system） */
  phase?: string
  /** 统一事件契约：父事件 id（如所属模型轮） */
  parent_event_id?: string
  /** 统一事件契约：安全摘要及尝试次数。 */
  summary?: string
  attempt?: number
  /** 模型完成事件公开的脱敏 token 用量。 */
  usage?: {
    input_tokens?: number
    output_tokens?: number
    total_tokens?: number
    cached_tokens?: number
  }
  /** 统一事件契约：区间起止（真实耗时） */
  started_at?: string
  ended_at?: string
}

export interface SessionRun {
  taskId: string
  runId: string
  status: string
  startedAt: string
  updatedAt: string
  modelId?: string
  reasoningEffort?: ReasoningEffort
  permissionMode?: PermissionMode
  items: RunIndexItem[]
}

// POST /api/v1/tasks 响应（202）
export interface TaskQueued {
  task_id: string
  run_id: string
  session_id: string
  status: string
  events_url: string
}

// ---- 供应商（2.7 供应商管理窗口） -----------------------------------------

export type ProviderProtocol = 'openai_compatible' | 'anthropic' | 'deepseek' | 'ollama'

export interface Provider {
  provider_id: string
  owner_id: string
  device_id: string
  name: string
  protocol: ProviderProtocol
  base_url: string
  model: string
  models: string[]
  reasoning_efforts: string[]
  timeout: number
  concurrency: number
  state: 'active' | 'disabled' | 'error'
  is_default: boolean
  last_test_at: string
  last_error: string
  schema_version: number
}

// ---- SSE 事件流 ----------------------------------------------------------

// 每个 SSE 帧的 data 都是一个事件信封；type 事件名同帧头 event 字段
// 统一事件契约：信封层携带 phase/attempt/started_at/ended_at/status/summary 与
// 脱敏后的 attributes，前端投影时间轴与审计视图时只消费这一份规范化事件流。
export interface RunEventEnvelope {
  event_id: string
  sequence: number
  type: string
  task_id: string
  run_id: string
  timestamp: string
  trace_id?: string
  parent_event_id?: string
  phase?: string
  attempt?: number | null
  started_at?: string
  ended_at?: string
  status?: string
  summary?: string
  attributes?: Record<string, unknown>
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
    args_preview?: Record<string, unknown>
    result_preview?: string
    result_truncated?: boolean
    approval_id?: string
    created_at?: string
    expires_at?: string
    status?: string
    decision?: string
    [key: string]: unknown
  }
}

export interface AgentProgress {
  phase: string
  nextStep: string
  checklist: string[]
  doneWhen: string[]
  completedItems: string[]
  toolSteps: number
  readFiles: number
  maxToolSteps: number
  maxReadFiles: number
  maxTotalSteps: number
  reason?: string
}

export interface AgentActivity {
  id: string
  type: string
  label: string
  detail?: string
  createdAt: string
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
  activity?: AgentActivity[]
  runId?: string
  status?: 'streaming' | 'done' // assistant 消息运行状态
}

export interface Session {
  id: string
  title: string
  createdAt: string
  displayNameSource?: 'auto' | 'user'
  displayNameUpdatedAt?: string
  updatedAt?: string
  draft?: boolean
  /** 后端 workspace_id，显示路径由 Workspace.display_path 映射 */
  workspaceId: string
  executionEnvironment?: string
  deviceId?: string
  model: string
  modelOptions: ModelCapability[]
  runIndex?: RunIndexItem[]
  runs?: SessionRun[]
  activeRunId?: string
  messages: Message[]
  lastTaskId?: string
  lastRunId?: string
}

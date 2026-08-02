// 数据契约：与 api-server 的 REST / SSE 事件对齐（后端就绪后替换 mock 数据源）

export type ToolStatus = 'pending' | 'running' | 'completed' | 'rejected' | 'error'

export interface ToolCall {
  id: string
  toolName: string // write_file / patch_file / run_shell 等
  args: Record<string, unknown>
  status: ToolStatus
  result?: string
  requiresApproval?: boolean // 危险工具需逐次审批
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string // ISO 时间
  toolCalls?: ToolCall[]
  status?: 'streaming' | 'done' // assistant 消息流式状态
}

export interface Session {
  id: string
  title: string
  createdAt: string
  workspace: string
  model: string
  messages: Message[]
}

// 运行结果 artifacts：对应 GET /api/v1/runs/{run_id}/artifacts 的 task_state / trace / report
export interface TraceEvent {
  seq: number
  type: string
  detail: string
  ts: string
}

export interface TaskStateSummary {
  runId: string
  status: string
  steps: number
  toolCalls: { toolName: string; status: ToolStatus; approvals: number }[]
}

export interface RunArtifacts {
  taskState: TaskStateSummary
  trace: TraceEvent[]
  report: string
}

// SSE 事件（预留，与 api-server 事件名对齐）
export type RunEvent =
  | { type: 'message.start' }
  | { type: 'tool.started'; toolCall: ToolCall }
  | { type: 'tool.completed'; toolCall: ToolCall }
  | { type: 'tool.rejected'; toolCall: ToolCall }
  | { type: 'message.completed'; message: Message }

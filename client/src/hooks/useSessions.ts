import { message as notify } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  appendTaskMessage,
  cancelTask,
  createSession as apiCreateSession,
  createTask,
  deleteSession as apiDeleteSession,
  deleteWorkspace as apiDeleteWorkspace,
  friendlyMessage,
  getSession,
  getTask,
  getRuntimeConfig,
  listMcpServers,
  listSessions,
  listSkills,
  listWorkspaces,
  openTaskEventStream,
  resolveApproval,
  renameDevice as apiRenameDevice,
  renameSession as apiRenameSession,
  renameWorkspace as apiRenameWorkspace,
} from '../api/client'
import type {
  AgentProgress,
  McpServerMetadata,
  Message,
  MessageBlock,
  ModelCapability,
  PendingApproval,
  PermissionMode,
  RunEventEnvelope,
  RuntimeConfig,
  Session,
  SessionDetail,
  SessionMessage,
  ReasoningEffort,
  ReviewBattleEntry,
  RunIndexItem,
  SessionRun,
  SkillMetadata,
  ToolCall,
  Workspace,
} from '../api/types'
import {
  applyToolEvent,
  getFinalAnswer,
  getLatestTask,
  historyAllowsSending,
  isInternalReviewDiagnostic,
  reconcileToolCalls,
  resolveHistoryStatus,
  terminalFailureMessage,
} from './session-state.ts'
import type { HistoryStatus } from './session-state.ts'
import { filterNavigableSessions, selectInitialNavigableSession } from '../features/sessions/session-groups'
import { workspaceKey } from '../features/sessions/workspaceIdentity'
import { mergeRunIndex } from './run-events'

// ---- assistant 消息交替块(commentary 与行为按事件顺序) ----

function appendThinkingBlock(blocks: MessageBlock[] | undefined, text: string, turn?: number): MessageBlock[] {
  const list = blocks ?? []
  const last = list[list.length - 1]
  if (last && last.kind === 'behavior') {
    return [...list.slice(0, -1), { ...last, turn: turn ?? last.turn, thinking: `${last.thinking ?? ''}${text}` }]
  }
  return [...list, { kind: 'behavior', thinking: text, turn }]
}

/** 流式叙述(delta)按 turn 归入 commentary 块:避免堆进 content 底部一大坨。 */
function appendDeltaCommentary(blocks: MessageBlock[] | undefined, text: string, turn?: number): MessageBlock[] {
  const list = blocks ?? []
  const last = list[list.length - 1]
  if (last && last.kind === 'commentary' && (last.turn ?? undefined) === turn) {
    return [...list.slice(0, -1), { ...last, text: `${last.text ?? ''}${text}` }]
  }
  return [...list, { kind: 'commentary', text, turn }]
}

function appendToolCallBlock(blocks: MessageBlock[] | undefined, toolCall: ToolCall, turn?: number): MessageBlock[] {
  const list = blocks ?? []
  const last = list[list.length - 1]
  if (last && last.kind === 'behavior') {
    return [
      ...list.slice(0, -1),
      {
        ...last,
        turn: turn ?? last.turn,
        toolCalls: applyToolEvent(last.toolCalls, {
          id: toolCall.id,
          toolName: toolCall.toolName,
          status: toolCall.status,
          args: toolCall.args,
          result: toolCall.result,
        }),
      },
    ]
  }
  return [...list, { kind: 'behavior', toolCalls: [toolCall], turn }]
}

function updateToolCallInBlocks(
  blocks: MessageBlock[] | undefined,
  toolCallId: string,
  updater: (tool: ToolCall) => ToolCall,
): MessageBlock[] {
  return (blocks ?? []).map((block) => {
    if (block.kind !== 'behavior' || !block.toolCalls?.some((tool) => tool.id === toolCallId)) {
      return block
    }
    return { ...block, toolCalls: block.toolCalls.map((tool) => tool.id === toolCallId ? updater(tool) : tool) }
  })
}

function appendReviewBlock(blocks: MessageBlock[] | undefined, entry: ReviewBattleEntry, turn?: number): MessageBlock[] {
  return [...(blocks ?? []), { kind: 'review', entries: [entry], turn }]
}

function appendReviewEntry(blocks: MessageBlock[] | undefined, entry: ReviewBattleEntry, turn?: number): MessageBlock[] {
  const list = blocks ?? []
  const last = list[list.length - 1]
  if (last && last.kind === 'review') {
    return [...list.slice(0, -1), { ...last, turn: turn ?? last.turn, entries: [...last.entries, entry] }]
  }
  return [...list, { kind: 'review', entries: [entry], turn }]
}

function appendReviewThinking(blocks: MessageBlock[] | undefined, text: string, turn?: number): MessageBlock[] {
  const list = blocks ?? []
  const last = list[list.length - 1]
  // §7.8.9 修正：thinking 只累积到「未完成(entries 为空)」的激活 review 块——
  // 已 completed 的块（entries 非空）另起新块,避免多次 review 的思考堆叠错位。
  if (last && last.kind === 'review' && last.entries.length === 0) {
    return [...list.slice(0, -1), { ...last, turn: turn ?? last.turn, thinking: `${last.thinking ?? ''}${text}` }]
  }
  return [...list, { kind: 'review', entries: [], thinking: text, turn }]
}

/** 是否存在「未完成(entries 为空)」的激活 review 块（thinking 已累积,等 completed 塞结果）。 */
function hasPendingReviewBlock(blocks: MessageBlock[] | undefined): boolean {
  const last = (blocks ?? [])[blocks?.length ? blocks.length - 1 : -1]
  return Boolean(last && last.kind === 'review' && last.entries.length === 0)
}

let idCounter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${idCounter++}`

const EMPTY_SESSION_TITLE = '新对话'
const automaticSessionTitle = (request: string) => request.trim().replace(/\s+/g, ' ').slice(0, 200) || EMPTY_SESSION_TITLE

function visibleSessionTitle(title: string | undefined, hasStarted?: boolean, messageTotal?: number): string {
  const started = hasStarted ?? (messageTotal ?? 0) > 0
  const normalized = title?.trim()
  return started && normalized ? normalized : EMPTY_SESSION_TITLE
}

function parseRunUsage(value: unknown): RunIndexItem['usage'] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const source = value as Record<string, unknown>
  const result: NonNullable<RunIndexItem['usage']> = {}
  for (const key of ['input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens'] as const) {
    const number = source[key]
    if (typeof number === 'number' && Number.isFinite(number) && number >= 0) result[key] = number
  }
  return Object.keys(result).length > 0 ? result : undefined
}

// 与后端 TaskStatus 对齐
const TERMINAL_STATUSES = new Set(['completed', 'cancelled', 'failed', 'interrupted', 'blocked'])
const RUNNING_STATUSES = new Set(['queued', 'running', 'waiting_for_approval', 'cancel_requested'])
const TERMINAL_EVENTS = new Set([
  'task.completed',
  'task.cancelled',
  'task.failed',
  'task.interrupted',
  'task.blocked',
])

function sessionModel(
  workspaceId: string,
  deviceId: string | undefined,
  executionEnvironment: string | undefined,
  workspaces: Workspace[],
  serverModel: string,
): string {
  const workspace = workspaces.find(
    (item) => workspaceKey(item) === workspaceKey({
      workspace_id: workspaceId,
      device_id: deviceId,
      execution_environment: executionEnvironment,
    }),
  )
  if (workspace?.execution_environment === 'local_worker') {
    return workspace.model_configured ? (workspace.model ?? '本地模型') : '本地模型未配置'
  }
  return serverModel
}

function sessionModelOptions(
  workspaceId: string,
  deviceId: string | undefined,
  executionEnvironment: string | undefined,
  workspaces: Workspace[],
  fallbackModel: string,
): ModelCapability[] {
  const workspace = workspaces.find(
    (item) => workspaceKey(item) === workspaceKey({
      workspace_id: workspaceId,
      device_id: deviceId,
      execution_environment: executionEnvironment,
    }),
  )
  const models = workspace?.model_capabilities?.models ?? []
  if (models.length > 0) return models
  const model = workspace?.model || fallbackModel
  return [{ id: model, display_name: model, reasoning_efforts: ['none'] }]
}

function parseAgentProgress(data: Record<string, unknown>): AgentProgress {
  const list = (value: unknown): string[] => (Array.isArray(value) ? value.map(String).slice(0, 20) : [])
  return {
    phase: String(data.phase ?? ''),
    nextStep: String(data.next_step ?? ''),
    checklist: list(data.checklist),
    doneWhen: list(data.done_when),
    completedItems: list(data.completed_items),
    toolSteps: Number(data.tool_steps ?? 0),
    readFiles: Number(data.read_files ?? 0),
    maxToolSteps: Number(data.max_tool_steps ?? 0),
    maxReadFiles: Number(data.max_read_files ?? 0),
    maxTotalSteps: Number(data.max_total_steps ?? 0),
    reason: data.reason ? String(data.reason) : undefined,
  }
}

interface ActiveRun {
  taskId: string
  runId: string
  sessionId: string
  assistantId: string
}

export interface UseSessions {
  sessions: Session[]
  activeId: string | null
  active: Session | null
  workspaces: Workspace[]
  runtimeConfig: RuntimeConfig | null
  skills: SkillMetadata[]
  mcpServers: McpServerMetadata[]
  loading: boolean
  historyStatus: HistoryStatus
  running: boolean
  stopping: boolean
  agentProgress: AgentProgress | null
  refreshWorkspaces: () => Promise<Workspace[]>
  retryHistory: () => void
  select: (id: string) => void
  createSession: (workspaceId: string, deviceId?: string) => void
  sendMessage: (content: string, modelId?: string, reasoningEffort?: ReasoningEffort, permissionMode?: PermissionMode) => void
  renameDevice: (deviceId: string, displayName: string) => Promise<void>
  renameWorkspace: (deviceId: string, workspaceId: string, displayName: string) => Promise<void>
  renameSession: (sessionId: string, displayName: string) => Promise<void>
  deleteSession: (sessionId: string) => Promise<void>
  deleteWorkspace: (deviceId: string, workspaceId: string) => Promise<void>
  approveTool: (messageId: string, toolCallId: string) => void
  rejectTool: (messageId: string, toolCallId: string) => void
  stopRun: () => void
  selectRun: (runId: string) => void
}

// Session 列表、选中与运行状态：数据源为 api-server REST + SSE 事件流
export function useSessions(): UseSessions {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig | null>(null)
  const [skills, setSkills] = useState<SkillMetadata[]>([])
  const [mcpServers, setMcpServers] = useState<McpServerMetadata[]>([])
  const [loading, setLoading] = useState(true)
  const [historyStatus, setHistoryStatus] = useState<HistoryStatus>('loaded')
  const [historyFailures, setHistoryFailures] = useState<Set<string>>(() => new Set())
  const [runningSessionIds, setRunningSessionIds] = useState<Set<string>>(() => new Set())
  const [stoppingSessionIds, setStoppingSessionIds] = useState<Set<string>>(() => new Set())
  const [agentProgressBySession, setAgentProgressBySession] = useState<Map<string, AgentProgress>>(new Map())
  const [syncVersion, setSyncVersion] = useState(0)

  // 已从后端拉过完整消息的会话（避免选中时重复请求覆盖本地流式状态）
  const loadedRef = useRef<Set<string>>(new Set())
  const activeIdRef = useRef<string | null>(null)
  // 并发运行中的任务：task_id -> ActiveRun（每会话至多一个在跑任务）
  const activeRunByTaskRef = useRef<Map<string, ActiveRun>>(new Map())
  const cancelRequestedRef = useRef<Set<string>>(new Set())
  const channelRef = useRef<BroadcastChannel | null>(null)
  const esByTaskRef = useRef<Map<string, EventSource>>(new Map())
  const postToolWatchdogByTaskRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const reconcileTimerByTaskRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map())
  // 本窗口刚结束运行的会话：轮询看到其 updatedAt 变化时不要清 loadedRef 去重载，
  // 否则失败任务（本地未持久化）会把刚收尾的请求/回答重新加载成空。
  const recentlyFinishedRef = useRef<Set<string>>(new Set())
  // approval_id -> tool call（approval.required 建立，resolved 时回查）
  const approvalMapRef = useRef<Map<string, string>>(new Map())
  // 事件回调里需要读到最新 sessions（在 effect 中同步，避免 render 期间改 ref）
  const sessionsRef = useRef<Session[]>([])
  useEffect(() => {
    sessionsRef.current = sessions
  }, [sessions])
  useEffect(() => {
    activeIdRef.current = activeId
  }, [activeId])

  const broadcastSessionChange = useCallback((sessionId?: string) => {
    channelRef.current?.postMessage({ type: 'session.changed', session_id: sessionId ?? null })
  }, [])

  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return
    const channel = new BroadcastChannel('threadforge-session-sync-v1')
    channelRef.current = channel
    channel.onmessage = (event) => {
      const payload = event.data as { type?: string; session_id?: string | null } | null
      if (payload?.type !== 'session.changed') return
      if (payload.session_id) {
        loadedRef.current.delete(payload.session_id)
        if (activeIdRef.current === payload.session_id) setHistoryStatus('loading')
        setHistoryFailures((current) => {
          if (!current.has(payload.session_id!)) return current
          const next = new Set(current)
          next.delete(payload.session_id!)
          return next
        })
      }
      setSyncVersion((value) => value + 1)
    }
    return () => {
      channel.close()
      channelRef.current = null
    }
  }, [])

  useEffect(
    () => () => {
      for (const watchdog of postToolWatchdogByTaskRef.current.values()) clearTimeout(watchdog)
      postToolWatchdogByTaskRef.current.clear()
      for (const reconcile of reconcileTimerByTaskRef.current.values()) clearInterval(reconcile)
      reconcileTimerByTaskRef.current.clear()
      for (const es of esByTaskRef.current.values()) es.close()
      esByTaskRef.current.clear()
    },
    [],
  )

  const refreshWorkspaces = useCallback(async () => {
    const response = await listWorkspaces()
    setWorkspaces(response.items)
    return response.items
  }, [])

  // ---- 本地状态更新辅助 -----------------------------------------------------

  const updateSession = useCallback((sessionId: string, updater: (s: Session) => Session) => {
    setSessions((prev) => prev.map((s) => (s.id === sessionId ? updater(s) : s)))
  }, [])

  const updateSessionMessages = useCallback(
    (sessionId: string, updater: (messages: Message[]) => Message[]) => {
      updateSession(sessionId, (s) => ({ ...s, messages: updater(s.messages) }))
    },
    [updateSession],
  )

  const findTool = useCallback((sessionId: string, messageId: string, toolCallId: string) => {
    const session = sessionsRef.current.find((s) => s.id === sessionId)
    return session?.messages.find((m) => m.id === messageId)?.toolCalls?.find((t) => t.id === toolCallId)
  }, [])

  const updateTool = useCallback(
    (sessionId: string, messageId: string, toolCallId: string, updater: (t: ToolCall) => ToolCall) => {
      updateSessionMessages(sessionId, (messages) =>
        messages.map((m) =>
          m.id === messageId
            ? { ...m, toolCalls: m.toolCalls?.map((t) => (t.id === toolCallId ? updater(t) : t)) }
            : m,
        ),
      )
    },
    [updateSessionMessages],
  )

  // 按 tool_name 回查最新一个工具卡（approval.* / tool.completed 事件不含 tool_call_id）
  const findLatestToolByName = useCallback((sessionId: string, messageId: string, toolName: string) => {
    const session = sessionsRef.current.find((s) => s.id === sessionId)
    const toolCalls = session?.messages.find((m) => m.id === messageId)?.toolCalls ?? []
    for (let i = toolCalls.length - 1; i >= 0; i--) {
      if (toolCalls[i].toolName === toolName) return toolCalls[i]
    }
    return undefined
  }, [])

  // ---- 并发运行态辅助：按 session/task 分别跟踪 ---------------------------------

  const setRunningForSession = useCallback((sessionId: string, value: boolean) => {
    setRunningSessionIds((current) => {
      const next = new Set(current)
      if (value) next.add(sessionId)
      else next.delete(sessionId)
      return next
    })
  }, [])

  const setStoppingForSession = useCallback((sessionId: string, value: boolean) => {
    setStoppingSessionIds((current) => {
      const next = new Set(current)
      if (value) next.add(sessionId)
      else next.delete(sessionId)
      return next
    })
  }, [])

  const updateProgress = useCallback(
    (sessionId: string, updater: AgentProgress | null | ((prev: AgentProgress | null) => AgentProgress | null)) => {
      setAgentProgressBySession((current) => {
        const next = new Map(current)
        const prev = current.get(sessionId) ?? null
        const value = typeof updater === 'function'
          ? (updater as (p: AgentProgress | null) => AgentProgress | null)(prev)
          : updater
        if (value === null) next.delete(sessionId)
        else next.set(sessionId, value)
        return next
      })
    },
    [],
  )

  const findActiveRun = useCallback((sessionId: string): ActiveRun | undefined => {
    for (const run of activeRunByTaskRef.current.values()) {
      if (run.sessionId === sessionId) return run
    }
    return undefined
  }, [])

  // 关闭某个任务的本地运行态：清运行态、封口 placeholder、结束事件流
  const finishRun = useCallback((taskId: string, terminalStatus = 'completed') => {
    const run = activeRunByTaskRef.current.get(taskId)
    if (!run) return
    activeRunByTaskRef.current.delete(taskId)
    const watchdog = postToolWatchdogByTaskRef.current.get(taskId)
    if (watchdog) {
      clearTimeout(watchdog)
      postToolWatchdogByTaskRef.current.delete(taskId)
    }
    const reconcile = reconcileTimerByTaskRef.current.get(taskId)
    if (reconcile) {
      clearInterval(reconcile)
      reconcileTimerByTaskRef.current.delete(taskId)
    }
    setRunningForSession(run.sessionId, false)
    setStoppingForSession(run.sessionId, false)
    updateProgress(run.sessionId, null)
    cancelRequestedRef.current.delete(run.sessionId)
    recentlyFinishedRef.current.add(run.sessionId)
    const es = esByTaskRef.current.get(taskId)
    if (es) {
      es.close()
      esByTaskRef.current.delete(taskId)
    }
    updateSessionMessages(run.sessionId, (messages) =>
      messages.map((m) =>
        m.id === run.assistantId
          ? {
              ...m,
              status: 'done' as const,
              toolCalls: reconcileToolCalls(m.toolCalls, terminalStatus),
            }
          : m,
      ),
    )
    broadcastSessionChange(run.sessionId)
  }, [broadcastSessionChange, setRunningForSession, setStoppingForSession, updateProgress, updateSessionMessages])

  // 审批卡就位：approval.required 事件（无 tool_call_id，按 tool_name 匹配最近的卡）
  const ensureApprovalCard = useCallback(
    (sessionId: string, messageId: string, taskId: string, approval: PendingApproval) => {
      const existing = approval.tool_call_id
        ? findTool(sessionId, messageId, approval.tool_call_id)
        : findLatestToolByName(sessionId, messageId, approval.tool_name)
      if (existing?.approvalId === approval.approval_id) return
      const toolCallId = approval.tool_call_id || existing?.id || `tc-${approval.approval_id}`
      approvalMapRef.current.set(approval.approval_id, toolCallId)
      updateSessionMessages(sessionId, (messages) =>
        messages.map((m) => {
          if (m.id !== messageId) return m
          const toolCalls = m.toolCalls ?? []
          const index = toolCalls.findIndex((t) => t.id === toolCallId)
          const card: ToolCall = {
            id: toolCallId,
            toolName: approval.tool_name,
            args: approval.args_preview,
            status: 'pending',
            requiresApproval: true,
            approvalId: approval.approval_id,
            taskId,
          }
          return {
            ...m,
            toolCalls: index >= 0 ? toolCalls.map((t, i) => (i === index ? card : t)) : [...toolCalls, card],
          }
        }),
      )
    },
    [findLatestToolByName, findTool, updateSessionMessages],
  )

  // ---- SSE 事件流 ------------------------------------------------------------

  const attachEventStream = useCallback(
    (taskId: string, sessionId: string, assistantId: string) => {
      // §7.8.9 修正（2026-08-19）：live turn 计数——模型每轮调用 +1,
      // 行为/过程更新块按 turn 分组展示（执行 → Turn N → 思考/工具）。
      let liveTurn = 0
      esByTaskRef.current.get(taskId)?.close()
      const es = openTaskEventStream(taskId)
      esByTaskRef.current.set(taskId, es)
      es.addEventListener('error', () => {
        const current = activeRunByTaskRef.current.get(taskId)
        if (!current || current.taskId !== taskId) return
        updateProgress(sessionId, (progress) => progress ? {
          ...progress,
          nextStep: '实时连接正在恢复，任务状态仍会自动对账',
          reason: 'sse_reconnecting',
        } : progress)
      })
      const previousReconcile = reconcileTimerByTaskRef.current.get(taskId)
      if (previousReconcile) clearInterval(previousReconcile)
      reconcileTimerByTaskRef.current.set(taskId, setInterval(() => {
        const activeRun = activeRunByTaskRef.current.get(taskId)
        if (!activeRun || activeRun.taskId !== taskId) return
        void getTask(taskId).then((snapshot) => {
          const current = activeRunByTaskRef.current.get(taskId)
          if (!current || current.taskId !== taskId) return
          const finalAnswer = getFinalAnswer(snapshot as unknown as Record<string, unknown>)
            ?? terminalFailureMessage(snapshot as unknown as Record<string, unknown>)
          if (finalAnswer) {
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId ? { ...message, content: finalAnswer } : message,
            ))
          }
          if (TERMINAL_STATUSES.has(snapshot.status)) finishRun(taskId, snapshot.status)
        }).catch(() => {
          // SSE remains the primary path. A transient reconciliation failure
          // must not turn a healthy run into a client-side failure.
        })
      }, 7_000))

      const parse = (event: MessageEvent) => {
        try {
          return JSON.parse(event.data as string) as RunEventEnvelope
        } catch {
          return null
        }
      }
      // 运行中出现的新工具卡（tool.requested 先于 approval.required 到达）
      const ensureToolCard = (toolCallId: string, toolName: string, args?: Record<string, unknown>) => {
        updateSessionMessages(sessionId, (messages) =>
          messages.map((m) => {
            if (m.id !== assistantId) return m
            return {
              ...m,
              toolCalls: applyToolEvent(m.toolCalls, {
                id: toolCallId,
                toolName,
                args,
                status: 'running',
              }),
              blocks: appendToolCallBlock(m.blocks, {
                id: toolCallId,
                toolName,
                args,
                status: 'running',
              }, liveTurn),
            }
          }),
        )
      }
      const markToolByEvent = (
        status: ToolCall['status'],
        toolCallId: string,
        toolName: string,
        result?: string,
      ) => {
        updateSessionMessages(sessionId, (messages) => messages.map((message) =>
          message.id === assistantId ? {
            ...message,
            toolCalls: applyToolEvent(message.toolCalls, {
              id: toolCallId,
              toolName,
              status,
              result,
            }),
            blocks: updateToolCallInBlocks(message.blocks, toolCallId, (tool) => {
              const updated = applyToolEvent([tool], {
                id: toolCallId,
                toolName,
                status,
                result,
              })
              return updated?.[0] ?? tool
            }),
          } : message,
        ))
      }

      const appendRunIndex = (envelope: RunEventEnvelope) => {
        const labels: Record<string, string> = {
          'plan.created': '计划已创建',
          'plan.skipped': '直接回答',
          'assistant.commentary': '过程更新',
          'model.started': '模型请求',
          'model.completed': '模型完成',
          'model.retrying': '模型重试',
          'model.protocol_retrying': '协议重试',
          'review.started': '开始审查',
          'review.completed': '审查完成',
          'tool.requested': '工具请求',
          'tool.started': '工具开始',
          'tool.completed': '工具完成',
          'tool.failed': '工具失败',
          'policy.violation': '策略拦截',
          'approval.required': '等待审批',
          'approval.resolved': '审批完成',
          'message.completed': '最终回答',
          'agent.state': 'Agent 状态',
          'task.queued': '排队中',
          'task.started': '运行开始',
          'task.snapshot': '运行快照',
          'task.cancel_requested': '正在停止',
          'task.completed': '运行完成',
          'task.cancelled': '运行已取消',
          'task.failed': '运行失败',
          'task.interrupted': '运行已中断',
          'task.blocked': '运行受阻',
        }
        if (!labels[envelope.type]) return
        // 统一事件契约：优先读取信封层的 phase/interval/parent，缺失时回退到 data
        const parentEventId = envelope.parent_event_id ?? envelope.data.parent_event_id
        const startedAt = envelope.started_at ?? envelope.data.started_at
        const endedAt = envelope.ended_at ?? envelope.data.ended_at
        const item: RunIndexItem = {
          event_id: envelope.event_id,
          type: envelope.type,
          timestamp: envelope.timestamp,
          label: labels[envelope.type],
          phase: envelope.phase ? String(envelope.phase) : undefined,
          tool_name: envelope.data.tool_name ? String(envelope.data.tool_name) : undefined,
          tool_call_id: envelope.data.tool_call_id ? String(envelope.data.tool_call_id) : undefined,
          args_preview: envelope.data.args_preview && typeof envelope.data.args_preview === 'object'
            ? envelope.data.args_preview as Record<string, unknown>
            : undefined,
          result_preview: typeof envelope.data.result_preview === 'string' && envelope.data.result_preview
            ? envelope.data.result_preview
            : undefined,
          result_truncated: envelope.data.result_truncated === true ? true : undefined,
          text: typeof envelope.data.text === 'string' && envelope.data.text
            ? envelope.data.text
            : undefined,
          intent: envelope.data.intent ? String(envelope.data.intent) : undefined,
          step_count: envelope.data.step_count == null ? undefined : Number(envelope.data.step_count),
          status: envelope.status ? String(envelope.status) : envelope.data.status ? String(envelope.data.status) : undefined,
          run_id: envelope.run_id,
          parent_event_id: parentEventId ? String(parentEventId) : undefined,
          summary: envelope.summary ? String(envelope.summary) : undefined,
          attempt: typeof envelope.attempt === 'number' ? envelope.attempt : undefined,
          usage: parseRunUsage(envelope.attributes?.usage ?? envelope.data.usage),
          started_at: startedAt ? String(startedAt) : undefined,
          ended_at: endedAt ? String(endedAt) : undefined,
        }
        updateSession(sessionId, (session) => {
          const current = session.runIndex ?? []
          if (current.some((entry) => entry.event_id === item.event_id)) return session
          const runs = [...(session.runs ?? [])]
          const runPosition = runs.findIndex((run) => run.runId === envelope.run_id)
          const terminalStatus = envelope.type.startsWith('task.')
            ? envelope.type.slice('task.'.length)
            : undefined
          if (runPosition >= 0) {
            const run = runs[runPosition]
            if (!run.items.some((entry) => entry.event_id === item.event_id)) {
              runs[runPosition] = {
                ...run,
                status: terminalStatus ?? run.status,
                updatedAt: envelope.timestamp,
                items: [...run.items.slice(-499), item],
              }
            }
          } else {
            runs.push({
              taskId,
              runId: envelope.run_id,
              status: terminalStatus ?? 'running',
              startedAt: envelope.timestamp,
              updatedAt: envelope.timestamp,
              modelId: session.model,
              items: [item],
            })
          }
          return {
            ...session,
            runIndex: [...current.slice(-499), item],
            runs,
            activeRunId: envelope.run_id,
          }
        })
      }

      const appendActivity = (envelope: RunEventEnvelope, label: string, detail?: string) => {
        updateSessionMessages(sessionId, (messages) => messages.map((message) =>
          message.id === assistantId ? (() => {
            const activity = message.activity ?? []
            // 幂等：同一 event_id 只记录一次；model.protocol_retrying 走替换语义，不受此去重影响。
            if (envelope.type !== 'model.protocol_retrying' && activity.some((item) => item.id === envelope.event_id)) {
              return message
            }
            const next = {
              id: envelope.event_id,
              type: envelope.type,
              label,
              detail: detail?.trim() || undefined,
              createdAt: envelope.timestamp,
            }
            if (envelope.type === 'model.protocol_retrying') {
              const reverseIndex = [...activity].reverse().findIndex((item) => item.type === envelope.type)
              if (reverseIndex >= 0) {
                const previousIndex = activity.length - reverseIndex - 1
                return {
                  ...message,
                  activity: activity.map((item, index) => index === previousIndex ? next : item),
                }
              }
            }
            return { ...message, activity: [...activity, next].slice(-50) }
          })() : message,
        ))
      }

      const handleEnvelope = (envelope: RunEventEnvelope) => {
        const { type, data } = envelope
        appendRunIndex(envelope)
        switch (type) {
          case 'task.snapshot': {
            const status = String(data.status ?? '')
            if (data.phase) updateProgress(sessionId, parseAgentProgress(data))
            if (Array.isArray(data.run_index)) {
              // 快照只用于收敛状态：与本地流式 run_index 按 event_id 幂等合并，
              // 重连/刷新时不会把在途事件清零，也不产生重复。
              updateSession(sessionId, (session) => {
                const snapshotItems = data.run_index as RunIndexItem[]
                return {
                  ...session,
                  runIndex: mergeRunIndex(session.runIndex ?? [], snapshotItems),
                  runs: (session.runs ?? []).map((run) =>
                    run.runId === String(data.run_id ?? envelope.run_id)
                      ? {
                          ...run,
                          status,
                          updatedAt: String(data.updated_at ?? run.updatedAt),
                          items: mergeRunIndex(run.items, snapshotItems),
                        }
                      : run,
                  ),
                }
              })
            }
            const finalAnswer = getFinalAnswer(data) ?? terminalFailureMessage(data)
            if (finalAnswer) {
              updateSessionMessages(sessionId, (messages) =>
                messages.map((m) => (m.id === assistantId ? { ...m, content: finalAnswer } : m)),
              )
            }
            if (TERMINAL_STATUSES.has(status)) {
              finishRun(taskId, status)
              return
            }
            if (data.pending_approval) {
              ensureApprovalCard(sessionId, assistantId, taskId, data.pending_approval as PendingApproval)
            }
            return
          }
          case 'agent.state': {
            updateProgress(sessionId, parseAgentProgress(data))
            return
          }
          case 'model.started': {
            // §7.8.9 修正（2026-08-19）：每个 turn 独立收纳抽屉——模型每轮开始
            // 时新开一个行为块占位,后续 thinking/工具归入当前 turn,不复用上一 turn。
            liveTurn += 1
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId
                ? { ...message, blocks: [...(message.blocks ?? []), { kind: 'behavior', turn: liveTurn }] }
                : message,
            ))
            return
          }
          case 'model.completed': {
            return
          }
          case 'model.heartbeat': {
            const callSeconds = Math.max(0, Math.floor(Number(data.elapsed_seconds ?? 0)))
            const runSeconds = Math.max(callSeconds, Math.floor(Number(data.run_elapsed_seconds ?? callSeconds)))
            const round = Math.max(1, Math.floor(Number(data.round ?? 1)))
            const stage = String(data.stage ?? '')
            const stageLabel = stage === 'planning' ? 'planning'
              : stage === 'tool' ? 'running tool'
                : 'reasoning'
            updateProgress(sessionId, (current) => current ? {
              ...current,
              nextStep: `Model ${stageLabel} (total ${runSeconds}s · round ${round}, this call ${callSeconds}s)`,
              reason: stage === 'tool' ? 'tool_executing' : 'model_streaming',
            } : current)
            return
          }
          case 'model.retrying': {
            const attempt = Number(data.attempt ?? 1)
            const maxAttempts = Number(data.max_attempts ?? 1)
            const stage = String(data.stage ?? '') === 'planning' ? 'planning' : 'execute'
            updateProgress(sessionId, (current) => current ? {
              ...current,
              nextStep: `Model ${stage} request failed; retrying (${attempt + 1}/${maxAttempts})`,
              reason: String(data.error_code ?? 'model_retrying'),
            } : current)
            if (data.reset_stream === true) {
              updateSessionMessages(sessionId, (messages) => messages.map((message) =>
                message.id === assistantId ? { ...message, content: '' } : message,
              ))
            }
            appendActivity(envelope, 'Model request retry', `${stage} stage ${attempt + 1}/${maxAttempts}`)
            return
          }
          case 'model.protocol_retrying': {
            const attempt = Number(data.attempt ?? 1)
            const maxAttempts = Number(data.max_attempts ?? 1)
            const stage = String(data.stage ?? '') === 'planning' ? 'planning' : 'execute'
            updateProgress(sessionId, (current) => current ? {
              ...current,
              nextStep: `Model ${stage} output failed protocol validation; retrying (${attempt + 1}/${maxAttempts})`,
              reason: 'model_protocol_invalid',
            } : current)
            if (data.reset_stream === true) {
              updateSessionMessages(sessionId, (messages) => messages.map((message) =>
                message.id === assistantId ? { ...message, content: '' } : message,
              ))
            }
            appendActivity(envelope, 'Model protocol retry', `${stage} stage ${attempt + 1}/${maxAttempts}`)
            return
          }
          case 'assistant.delta': {
            const text = String(data.text ?? '')
            if (!text) return
            // §7.8.9 修正（2026-08-19）：流式叙述按 turn 归入 commentary 块,不再堆进
            // content 底部。content 只保留最终答案(message.completed / task.* 终答)。
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId
                ? { ...message, blocks: appendDeltaCommentary(message.blocks, text, liveTurn) }
                : message,
            ))
            return
          }
          case 'plan.created': {
            const steps = Array.isArray(data.steps) ? data.steps : []
            updateProgress(sessionId, {
              phase: 'PLANNING',
              nextStep: String(data.summary ?? ''),
              checklist: steps.map((step) => String((step as Record<string, unknown>).goal ?? '')),
              doneWhen: [],
              completedItems: [],
              toolSteps: 0,
              readFiles: 0,
              maxToolSteps: 0,
              maxReadFiles: 0,
              maxTotalSteps: 0,
            })
            appendActivity(envelope, '计划已创建', String(data.summary ?? ''))
            return
          }
          case 'plan.skipped': {
            updateProgress(sessionId, {
              phase: 'EXECUTE',
              nextStep: '正在直接回答',
              checklist: [],
              doneWhen: [],
              completedItems: [],
              toolSteps: 0,
              readFiles: 0,
              maxToolSteps: 0,
              maxReadFiles: 0,
              maxTotalSteps: 0,
            })
            appendActivity(envelope, '直接回答', '无需读取工作区')
            return
          }
          case 'assistant.commentary': {
            const text = String(data.text ?? '').trim()
            if (!text) return
            appendActivity(envelope, '过程更新', text)
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    commentary: `${message.commentary ?? ''}${message.commentary ? '\n' : ''}${text}`,
                    blocks: [...(message.blocks ?? []), { kind: 'commentary', text, turn: liveTurn }],
                  }
                : message,
            ))
            return
          }
          case 'assistant.thinking': {
            // DeepSeek 思考过程：独立于 content 累积，UI 折叠展示（不进正文）。
            const text = String(data.text ?? '')
            if (!text) return
            // §7.8.9 决策（2026-08-18）：thinking 按 stage 分区——planning 思考
            // 进 planningThinking（独立面板），每轮 turn 思考进 thinking + 行为块。
            const stage = String(data.stage ?? 'execute')
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId
                ? stage === 'planning'
                  ? { ...message, planningThinking: `${message.planningThinking ?? ''}${text}` }
                  : stage === 'review'
                    ? { ...message, blocks: appendReviewThinking(message.blocks, text, liveTurn) }
                    : {
                        ...message,
                        thinking: `${message.thinking ?? ''}${text}`,
                        blocks: appendThinkingBlock(message.blocks, text, liveTurn),
                      }
                : message,
            ))
            return
          }
          case 'review.started': {
            updateProgress(sessionId, (progress) => progress ? { ...progress, phase: 'REVIEW', nextStep: '正在审查结果' } : progress)
            appendActivity(envelope, '开始审查')
            return
          }
          case 'review.completed': {
            // §7.8.9 决策（2026-08-18）：双向对抗——review 回合进 blocks 审查对抗块
            //（谁发的、verdict、理由、obstacles、工具轮数）。
            const verdict = String(data.verdict ?? '')
            const entry: ReviewBattleEntry = {
              side: 'review',
              verdict: verdict as ReviewBattleEntry['verdict'],
              feedback: String(data.feedback ?? ''),
              reason: String(data.reason ?? ''),
              obstacles: Array.isArray(data.obstacles) ? data.obstacles.map(String) : undefined,
              result: verdict === 'finalize' ? 'passed' : verdict === 'redirect' ? 'rejected' : 'continue',
            }
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    // §7.8.9 修正（2026-08-19）：final 被 review redirect → 清空 content——
                    // 被拒的 final 不展示（只在最终通过时展示一次）,避免对话里重复
                    // 出现多版相同答案。
                    ...(verdict === 'redirect' ? { content: '' } : {}),
                    blocks: hasPendingReviewBlock(message.blocks)
                      ? appendReviewEntry(message.blocks, entry, liveTurn)
                      : appendReviewBlock(message.blocks, entry, liveTurn),
                  }
                : message,
            ))
            updateProgress(sessionId, (progress) => progress ? { ...progress, nextStep: `审查结果：${verdict || String(data.status ?? '')}` } : progress)
            appendActivity(envelope, '审查完成', `结果：${verdict || String(data.status ?? '')}`)
            return
          }
          case 'main_loop_rebuttal': {
            // §7.8.9 决策（2026-08-18）：主循环反驳 review 的「可以结束」——行动即理由。
            const entry: ReviewBattleEntry = {
              side: 'main_loop',
              action: String(data.action ?? ''),
              feedback: String(data.feedback ?? ''),
            }
            updateSessionMessages(sessionId, (messages) => messages.map((message) =>
              message.id === assistantId
                ? { ...message, blocks: appendReviewEntry(message.blocks, entry, liveTurn) }
                : message,
            ))
            appendActivity(envelope, '主循环反驳', String(data.action ?? ''))
            return
          }
          case 'tool.requested': {
            const toolCallId = String(data.tool_call_id ?? '')
            const toolName = String(data.tool_name ?? '')
            const args = data.args_preview && typeof data.args_preview === 'object'
              ? data.args_preview as Record<string, unknown>
              : undefined
            if (toolCallId) ensureToolCard(toolCallId, toolName, args)
            return
          }
          case 'tool.started': {
            const toolCallId = String(data.tool_call_id ?? '')
            const toolName = String(data.tool_name ?? '')
            markToolByEvent('running', toolCallId, toolName)
            appendActivity(envelope, '工具执行中', toolName)
            return
          }
          case 'tool.completed':
          case 'tool.failed': {
            const toolCallId = String(data.tool_call_id ?? '')
            const toolName = String(data.tool_name ?? '')
            const paths = (data.affected_paths ?? []) as string[]
            const resultPreview = typeof data.result_preview === 'string' ? data.result_preview : ''
            const result = resultPreview
              ? `${resultPreview}${data.result_truncated === true ? '\n\n[预览已截断]' : ''}`
              : paths.length > 0
                ? `影响路径：${paths.join('、')}`
                : undefined
            markToolByEvent(
              type === 'tool.completed' ? 'completed' : 'error',
              toolCallId,
              toolName,
              result,
            )
            appendActivity(envelope, type === 'tool.completed' ? '工具完成' : '工具失败', toolName)
            return
          }
          case 'approval.required': {
            ensureApprovalCard(sessionId, assistantId, taskId, data as unknown as PendingApproval)
            return
          }
          case 'approval.resolved': {
            const approvalId = String(data.approval_id ?? '')
            const decision = String(data.decision ?? '')
            const toolCallId = approvalMapRef.current.get(approvalId)
            if (!toolCallId) return
            if (decision === 'approved') {
              updateTool(sessionId, assistantId, toolCallId, (t) => ({ ...t, status: 'running' }))
            } else {
              updateTool(sessionId, assistantId, toolCallId, (t) => ({
                ...t,
                status: 'rejected',
                result: decision === 'cancelled' ? '已取消，未执行' : '已拒绝该操作',
              }))
            }
            return
          }
          case 'message.completed': {
            const text = String(data.text ?? '')
            if (isInternalReviewDiagnostic(text)) return
            updateSessionMessages(sessionId, (messages) =>
              messages.map((m) => (m.id === assistantId ? { ...m, content: text } : m)),
            )
            return
          }
          case 'task.completed':
          case 'task.cancelled':
          case 'task.failed':
          case 'task.interrupted':
          case 'task.blocked': {
            const status = type.slice('task.'.length)
            const finalAnswer = getFinalAnswer({ ...data, status }) ?? terminalFailureMessage({
              ...data,
              status,
            })
            if (finalAnswer) {
              updateSessionMessages(sessionId, (messages) =>
                messages.map((m) => (m.id === assistantId ? { ...m, content: finalAnswer } : m)),
              )
            }
            return
          }
          case 'task.cancel_requested': {
            setStoppingForSession(sessionId, true)
            updateProgress(sessionId, (current) => current ? {
              ...current,
              phase: 'FINAL',
              nextStep: '正在停止任务并清理运行资源',
              reason: 'cancel_requested',
            } : current)
            appendActivity(envelope, '正在停止', '已发送取消请求，等待 Worker 清理')
            return
          }
          default:
            return
        }
      }

      const onFrame = (event: MessageEvent) => {
        const envelope = parse(event)
        if (!envelope) return
        const watchdog = postToolWatchdogByTaskRef.current.get(taskId)
        if (watchdog) {
          clearTimeout(watchdog)
          postToolWatchdogByTaskRef.current.delete(taskId)
        }
        handleEnvelope(envelope)
        if (envelope.type === 'tool.completed' || envelope.type === 'tool.failed') {
          postToolWatchdogByTaskRef.current.set(taskId, setTimeout(() => {
            const run = activeRunByTaskRef.current.get(taskId)
            if (!run || run.taskId !== taskId) return
            updateProgress(sessionId, (current) => ({
              phase: current?.phase || 'ANALYZE_CONTEXT',
              nextStep: 'Model still reasoning over tool results; you can wait or stop the task',
              checklist: current?.checklist ?? [],
              doneWhen: current?.doneWhen ?? [],
              completedItems: current?.completedItems ?? [],
              toolSteps: current?.toolSteps ?? 0,
              readFiles: current?.readFiles ?? 0,
              maxToolSteps: current?.maxToolSteps ?? 0,
              maxReadFiles: current?.maxReadFiles ?? 0,
              maxTotalSteps: current?.maxTotalSteps ?? 0,
              reason: 'post_tool_waiting',
            }))
          }, 45_000))
        }
        if (TERMINAL_EVENTS.has(envelope.type)) {
          finishRun(taskId, envelope.type.slice('task.'.length))
        }
      }

      // 命名事件 + 兜底 message 事件（后端帧均带 event 名）
      const NAMED = [
        'task.snapshot',
        'agent.state',
        'model.started',
        'model.completed',
        'model.retrying',
        'model.protocol_retrying',
        'model.heartbeat',
        'assistant.delta',
        'plan.created',
        'assistant.commentary',
        'assistant.thinking',
        'review.started',
        'review.completed',
        'main_loop_rebuttal',
        'tool.requested',
        'tool.started',
        'tool.completed',
        'tool.failed',
        'approval.required',
        'approval.resolved',
        'message.completed',
        'task.completed',
        'task.cancel_requested',
        'task.cancelled',
        'task.failed',
        'task.interrupted',
        'task.blocked',
      ]
      NAMED.forEach((name) => es.addEventListener(name, onFrame))
      es.addEventListener('message', onFrame)
      // 断开后 EventSource 自动重连，重连成功会重发 task.snapshot；无需额外处理
    },
    [ensureApprovalCard, finishRun, setStoppingForSession, updateProgress, updateSession, updateSessionMessages, updateTool],
  )

  // ---- 初始加载 ---------------------------------------------------------------

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [configRes, wsRes, sessionRes, skillRes, mcpRes] = await Promise.all([
          getRuntimeConfig(),
          listWorkspaces(),
          listSessions(),
          listSkills(),
          listMcpServers(),
        ])
        if (cancelled) return
        setRuntimeConfig(configRes)
        setWorkspaces(wsRes.items)
        setSkills(skillRes.items)
        setMcpServers(mcpRes.items)
        const items: Session[] = sessionRes.items
          .slice()
          .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
          .map((item) => ({
            id: item.session_id,
            title: visibleSessionTitle(item.title, item.has_started, item.message_total),
            createdAt: item.created_at,
            updatedAt: item.updated_at ?? item.created_at,
            displayNameSource: item.display_name_source,
            displayNameUpdatedAt: item.display_name_updated_at ?? item.created_at,
            draft: item.has_started === false,
            workspaceId: item.workspace_id,
            executionEnvironment: item.execution_environment,
            deviceId: item.device_id,
            model: sessionModel(
              item.workspace_id,
              item.device_id,
              item.execution_environment,
              wsRes.items,
              configRes.model,
            ),
            modelOptions: sessionModelOptions(
              item.workspace_id,
              item.device_id,
              item.execution_environment,
              wsRes.items,
              configRes.model,
            ),
            messages: [],
          }))
        const navigable = filterNavigableSessions(items, wsRes.items)
        setSessions(navigable)
        const initialSession = selectInitialNavigableSession(navigable, wsRes.items)
        if (initialSession) {
          setHistoryStatus('loading')
          activeIdRef.current = initialSession.id
          setActiveId(initialSession.id)
        } else {
          activeIdRef.current = null
          setActiveId(null)
        }
      } catch (err) {
        if (!cancelled) notify.error(friendlyMessage(err))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // ---- 会话选中：按需拉取详情；未结束的任务续接事件流 -------------------------

  useEffect(() => {
    if (!activeId || loadedRef.current.has(activeId) || historyFailures.has(activeId)) return
    let cancelled = false
    ;(async () => {
      try {
        const detail: SessionDetail = await getSession(activeId, 200)
        if (cancelled) return
        loadedRef.current.add(activeId)
        setHistoryStatus('loaded')
        setHistoryFailures((current) => {
          if (!current.has(activeId)) return current
          const next = new Set(current)
          next.delete(activeId)
          return next
        })

        const messages: Message[] = detail.messages
          .filter(
            (m): m is SessionMessage & { role: Message['role'] } =>
              (m.role === 'user' || m.role === 'assistant') &&
              !(m.role === 'assistant' && isInternalReviewDiagnostic(m.content)),
          )
          .map((m, i) => ({
            id: `m-${detail.session_id}-${i}`,
            role: m.role,
            content: m.content,
            createdAt: m.created_at,
            status: 'done' as const,
            thinking: m.thinking,
            planningThinking: m.planning_thinking ?? undefined,
            toolCalls: m.tool_calls?.map((tool) => ({
              id: tool.id,
              toolName: tool.tool_name,
              args: tool.args ?? undefined,
              result: tool.result,
              status: (tool.status === 'error' ? 'error' : 'completed') as ToolCall['status'],
            })),
            reviewEntries: m.review_entries?.map((entry) => ({
              side: entry.side,
              verdict: (entry.verdict as ReviewBattleEntry['verdict']) ?? undefined,
              feedback: entry.feedback ?? undefined,
              reason: entry.reason ?? undefined,
              obstacles: entry.obstacles ?? undefined,
              action: entry.action ?? undefined,
            })),
            commentary: m.commentary,
            blocks: m.blocks?.map((block) => {
              if (block.kind === 'commentary') {
                return { kind: 'commentary' as const, text: block.text ?? '', turn: block.turn ?? undefined }
              }
              if (block.kind === 'review') {
                return {
                  kind: 'review' as const,
                  turn: block.turn ?? undefined,
                  entries: (block.entries ?? []).map((entry) => ({
                    side: entry.side,
                    verdict: (entry.verdict as ReviewBattleEntry['verdict']) ?? undefined,
                    feedback: entry.feedback ?? undefined,
                    reason: entry.reason ?? undefined,
                    obstacles: entry.obstacles ?? undefined,
                    action: entry.action ?? undefined,
                  })),
                }
              }
              return {
                kind: 'behavior' as const,
                turn: block.turn ?? undefined,
                thinking: block.thinking,
                toolCalls: (block.toolCalls ?? []).map((tool) => ({
                  id: tool.id,
                  toolName: tool.tool_name,
                  args: tool.args ?? undefined,
                  result: tool.result,
                  status: (tool.status === 'error' ? 'error' : 'completed') as ToolCall['status'],
                })),
              }
            }),
          }))
        const lastTask = getLatestTask(detail.tasks)
        const runs: SessionRun[] = detail.tasks
          .slice()
          .sort((a, b) => a.created_at.localeCompare(b.created_at))
          .map((task) => ({
            taskId: task.task_id,
            runId: task.run_id,
            status: task.status,
            startedAt: task.created_at,
            updatedAt: task.updated_at,
            modelId: task.model_id,
            reasoningEffort: task.reasoning_effort,
            input: task.input,
            items: task.run_index ?? [],
          }))
        const loaded: Session = {
          id: detail.session_id,
          title: visibleSessionTitle(detail.title, detail.has_started, detail.message_total),
          createdAt: detail.created_at,
          updatedAt: detail.updated_at ?? detail.created_at,
          displayNameSource: detail.display_name_source,
          displayNameUpdatedAt: detail.display_name_updated_at ?? detail.created_at,
          draft: detail.has_started === false,
          workspaceId: detail.workspace_id,
          executionEnvironment: detail.execution_environment,
          deviceId: detail.device_id,
          model: sessionModel(
            detail.workspace_id,
            detail.device_id,
            detail.execution_environment,
            workspaces,
            runtimeConfig?.model ?? '未配置',
          ),
          modelOptions: sessionModelOptions(
            detail.workspace_id,
            detail.device_id,
            detail.execution_environment,
            workspaces,
            runtimeConfig?.model ?? '',
          ),
          messages,
          lastTaskId: lastTask?.task_id,
          lastRunId: lastTask?.run_id,
          runIndex: lastTask?.run_index ?? [],
          runs,
          activeRunId: lastTask?.run_id,
        }
        setSessions((prev) =>
          prev.map((s) => (s.id === detail.session_id ? { ...s, ...loaded } : s)),
        )

        // 会话里还有未结束的任务（如刷新后恢复）：补 placeholder 并续接事件流
        if (lastTask && RUNNING_STATUSES.has(lastTask.status)) {
          const assistantId = nextId('m-agent')
          setRunningForSession(detail.session_id, true)
          activeRunByTaskRef.current.set(lastTask.task_id, {
            taskId: lastTask.task_id,
            runId: lastTask.run_id,
            sessionId: detail.session_id,
            assistantId,
          })
          setSessions((prev) =>
            prev.map((s) =>
              s.id === detail.session_id
                ? {
                    ...s,
                    messages: [
                      ...s.messages,
                      {
                        id: assistantId,
                        role: 'assistant',
                        content: '',
                        createdAt: new Date().toISOString(),
                        status: 'streaming',
                        blocks: [],
                      },
                    ],
                  }
                : s,
            ),
          )
          attachEventStream(lastTask.task_id, detail.session_id, assistantId)
        }
      } catch {
        if (!cancelled) {
          setHistoryStatus('error')
          setHistoryFailures((current) => new Set(current).add(activeId))
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [activeId, attachEventStream, historyFailures, runtimeConfig, setRunningForSession, syncVersion, workspaces])

  // SSE 只覆盖当前窗口的任务；索引轮询负责把其他窗口的创建、重命名和完成状态带过来。
  useEffect(() => {
    let disposed = false
    const refresh = async () => {
      try {
        const response = await listSessions()
        if (disposed) return
        setSessions((current) => {
          const existing = new Map(current.map((session) => [session.id, session]))
          const next = response.items.map((item) => {
            const currentSession = existing.get(item.session_id)
            const updatedAt = item.updated_at ?? item.created_at
            const displayNameUpdatedAt = item.display_name_updated_at ?? item.created_at
            const title = visibleSessionTitle(item.title, item.has_started, item.message_total)
            if (currentSession?.displayNameUpdatedAt === displayNameUpdatedAt && currentSession?.createdAt === item.created_at && currentSession?.title === title && currentSession.updatedAt === updatedAt) {
              return currentSession
            }
            const recentlyFinished = recentlyFinishedRef.current.delete(item.session_id)
            if (currentSession && !findActiveRun(item.session_id) && !recentlyFinished) {
              loadedRef.current.delete(item.session_id)
            }
            return {
              ...(currentSession ?? {
                id: item.session_id,
                messages: [],
                model: runtimeConfig?.model ?? '未配置',
                modelOptions: [{
                  id: runtimeConfig?.model ?? '未配置',
                  display_name: runtimeConfig?.model ?? '未配置',
                  reasoning_efforts: ['none'] as ReasoningEffort[],
                }],
              }),
              id: item.session_id,
              title,
              createdAt: item.created_at,
              displayNameSource: item.display_name_source,
              displayNameUpdatedAt,
              draft: item.has_started === false,
              workspaceId: item.workspace_id,
              executionEnvironment: item.execution_environment,
              deviceId: item.device_id,
              updatedAt,
            } as Session & { updatedAt?: string }
          })
          return filterNavigableSessions(next, workspaces)
        })
        setSyncVersion((value) => value + 1)
      } catch {
        // Keep the current UI when the control plane is temporarily unavailable.
      }
    }
    const onFocus = () => void refresh()
    window.addEventListener('focus', onFocus)
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, 5000)
    return () => {
      disposed = true
      window.removeEventListener('focus', onFocus)
      window.clearInterval(timer)
    }
  }, [findActiveRun, runtimeConfig?.model, workspaces])

  // ---- 用户操作 ---------------------------------------------------------------

  const select = useCallback((id: string) => {
    setHistoryStatus(resolveHistoryStatus(id, loadedRef.current, historyFailures))
    activeIdRef.current = id
    setActiveId(id)
  }, [historyFailures])

  const retryHistory = useCallback(() => {
    if (!activeId) return
    loadedRef.current.delete(activeId)
    setHistoryStatus('loading')
    setHistoryFailures((current) => {
      const next = new Set(current)
      next.delete(activeId)
      return next
    })
  }, [activeId])

  const createSession = useCallback(
    (workspaceId: string, deviceId?: string) => {
      ;(async () => {
        try {
          const res = await apiCreateSession(workspaceId, undefined, deviceId)
          const session: Session = {
            id: res.session_id,
            title: visibleSessionTitle(res.title, false, 0),
            createdAt: res.created_at,
            updatedAt: res.created_at,
            displayNameSource: res.display_name_source,
            displayNameUpdatedAt: res.display_name_updated_at ?? res.created_at,
            draft: true,
            workspaceId: res.workspace_id,
            executionEnvironment: res.execution_environment,
            deviceId: res.device_id,
            model: sessionModel(
              res.workspace_id,
              res.device_id,
              res.execution_environment,
              workspaces,
              runtimeConfig?.model ?? '未配置',
            ),
            modelOptions: sessionModelOptions(
              res.workspace_id,
              res.device_id,
              res.execution_environment,
              workspaces,
              runtimeConfig?.model ?? '',
            ),
            messages: [],
          }
          loadedRef.current.add(session.id)
          setSessions((prev) => [session, ...prev])
          setHistoryStatus('loaded')
          activeIdRef.current = session.id
          setActiveId(session.id)
          broadcastSessionChange(session.id)
        } catch (err) {
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [broadcastSessionChange, runtimeConfig, workspaces],
  )

  const sendMessage = useCallback(
    (content: string, modelId?: string, reasoningEffort: ReasoningEffort = 'none', permissionMode: PermissionMode = 'default') => {
      const sessionId = activeId
      if (!sessionId || !content.trim()) return
      const activeSession = sessionsRef.current.find((session) => session.id === sessionId)
      const historyStatus = resolveHistoryStatus(sessionId, loadedRef.current, historyFailures)
      if (!activeSession || !historyAllowsSending(Boolean(activeSession.draft), historyStatus)) {
        notify.warning(historyStatus === 'error' ? '请先重新加载会话历史' : '会话历史仍在加载，请稍候')
        return
      }
      // 运行中：追加到当前 in-flight 任务（inbox 1.5），不新建任务、不新开占位回答。
      const activeRun = findActiveRun(sessionId)
      if (activeRun) {
        const now = new Date().toISOString()
        const userId = nextId('m-user')
        updateSessionMessages(sessionId, (messages) => [
          ...messages,
          { id: userId, role: 'user', content: content.trim(), createdAt: now },
        ])
        broadcastSessionChange(sessionId)
        ;(async () => {
          try {
            await appendTaskMessage(activeRun.taskId, content.trim(), true)
          } catch (err) {
            notify.error(friendlyMessage(err))
          }
        })()
        return
      }
      setRunningForSession(sessionId, true)
      setStoppingForSession(sessionId, false)
      cancelRequestedRef.current.delete(sessionId)
      updateProgress(sessionId, null)

      const now = new Date().toISOString()
      const firstRequestTitle = activeSession?.draft && activeSession.displayNameSource !== 'user'
        ? automaticSessionTitle(content)
        : undefined
      const userId = nextId('m-user')
      const assistantId = nextId('m-agent')
      updateSessionMessages(sessionId, (messages) => [
        ...messages,
        { id: userId, role: 'user', content: content.trim(), createdAt: now },
        { id: assistantId, role: 'assistant', content: '', createdAt: now, status: 'streaming', blocks: [] },
      ])
      updateSession(sessionId, (session) => ({
        ...session,
        draft: false,
        title: firstRequestTitle ?? session.title,
      }))
      broadcastSessionChange(sessionId)

      ;(async () => {
        let queuedTaskId: string | null = null
        try {
          const queued = await createTask(
            sessionId,
            content.trim(),
            undefined,
            modelId,
            reasoningEffort,
            permissionMode,
          )
          queuedTaskId = queued.task_id
          activeRunByTaskRef.current.set(queued.task_id, {
            taskId: queued.task_id,
            runId: queued.run_id,
            sessionId,
            assistantId,
          })
          updateSession(sessionId, (s) => ({
            ...s,
            lastTaskId: queued.task_id,
            lastRunId: queued.run_id,
            runIndex: [],
            runs: [
              ...(s.runs ?? []),
              {
                taskId: queued.task_id,
                runId: queued.run_id,
                status: queued.status,
                startedAt: now,
                updatedAt: now,
                modelId: modelId ?? activeSession.model,
                reasoningEffort,
                permissionMode,
                input: content.trim(),
                items: [],
              },
            ],
            activeRunId: queued.run_id,
          }))
          attachEventStream(queued.task_id, sessionId, assistantId)
          broadcastSessionChange(sessionId)
          if (cancelRequestedRef.current.has(sessionId)) {
            try {
              await cancelTask(queued.task_id)
            } catch (error) {
              setStoppingForSession(sessionId, false)
              notify.error(friendlyMessage(error))
            }
          }
        } catch (err) {
          setRunningForSession(sessionId, false)
          setStoppingForSession(sessionId, false)
          cancelRequestedRef.current.delete(sessionId)
          if (queuedTaskId) activeRunByTaskRef.current.delete(queuedTaskId)
          // 任务未创建成功：移除占位 assistant 消息，保留用户消息
          updateSessionMessages(sessionId, (messages) => messages.filter((m) => m.id !== assistantId))
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [activeId, attachEventStream, broadcastSessionChange, findActiveRun, historyFailures, setRunningForSession, setStoppingForSession, updateProgress, updateSession, updateSessionMessages],
  )

  const approveTool = useCallback(
    (messageId: string, toolCallId: string) => {
      const sessionId = activeId
      if (!sessionId) return
      const tool = findTool(sessionId, messageId, toolCallId)
      if (!tool?.approvalId || !tool.taskId) return
      ;(async () => {
        try {
          await resolveApproval(tool.taskId!, tool.approvalId!, 'approved')
          // 后端已确认决策：本地先落状态，approval.resolved 事件到达时幂等
          updateTool(sessionId, messageId, toolCallId, (t) => ({ ...t, status: 'running' }))
        } catch (err) {
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [activeId, findTool, updateTool],
  )

  const rejectTool = useCallback(
    (messageId: string, toolCallId: string) => {
      const sessionId = activeId
      if (!sessionId) return
      const tool = findTool(sessionId, messageId, toolCallId)
      if (!tool?.approvalId || !tool.taskId) return
      ;(async () => {
        try {
          await resolveApproval(tool.taskId!, tool.approvalId!, 'rejected')
          updateTool(sessionId, messageId, toolCallId, (t) => ({
            ...t,
            status: 'rejected',
            result: '已拒绝该操作',
          }))
        } catch (err) {
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [activeId, findTool, updateTool],
  )

  const stopRun = useCallback(() => {
    const sessionId = activeId
    if (!sessionId) return
    cancelRequestedRef.current.add(sessionId)
    setStoppingForSession(sessionId, true)
    const run = findActiveRun(sessionId)
    if (!run) {
      updateProgress(sessionId, (current) => current ? {
        ...current,
        phase: 'FINAL',
        nextStep: '正在停止任务',
        reason: 'cancel_requested',
      } : current)
      return
    }
    ;(async () => {
      try {
        const snapshot = await cancelTask(run.taskId)
        if (TERMINAL_STATUSES.has(snapshot.status)) {
          // 后端直接返回终态（任务已结束），事件流可能已断：本地收尾
          finishRun(run.taskId, snapshot.status)
        }
        // 否则等待 task.cancelled 事件收尾
      } catch (err) {
        setStoppingForSession(sessionId, false)
        cancelRequestedRef.current.delete(sessionId)
        notify.error(friendlyMessage(err))
      }
    })()
  }, [activeId, findActiveRun, finishRun, setStoppingForSession, updateProgress])

  const selectRun = useCallback((runId: string) => {
    if (!activeId) return
    updateSession(activeId, (session) => ({ ...session, activeRunId: runId }))
  }, [activeId, updateSession])

  const renameDevice = useCallback(async (deviceId: string, displayName: string) => {
    try {
      const expectedUpdatedAt = workspaces.find((workspace) => workspace.device_id === deviceId)
        ?.device_display_name_updated_at
      const result = await apiRenameDevice(deviceId, displayName, expectedUpdatedAt)
      setWorkspaces((current) => current.map((workspace) =>
        workspace.device_id === deviceId
          ? {
              ...workspace,
              device_name: result.display_name,
              device_display_name: result.display_name,
              device_display_name_updated_at: result.display_name_updated_at,
              display_path: `${result.display_name} / ${workspace.name}`,
            }
          : workspace,
      ))
    } catch (error) {
      notify.error(friendlyMessage(error))
      throw error
    }
  }, [workspaces])

  const renameWorkspace = useCallback(async (
    deviceId: string,
    workspaceId: string,
    displayName: string,
  ) => {
    try {
      const workspace = workspaces.find((item) =>
        item.device_id === deviceId && item.workspace_id === workspaceId)
      const result = await apiRenameWorkspace(
        deviceId,
        workspaceId,
        displayName,
        workspace?.display_name_updated_at,
      )
      setWorkspaces((current) => current.map((workspace) =>
        workspace.device_id === deviceId && workspace.workspace_id === workspaceId
          ? {
              ...workspace,
              name: result.display_name,
              display_name: result.display_name,
              display_name_updated_at: result.display_name_updated_at,
              display_path: `${workspace.device_name ?? 'Worker'} / ${result.display_name}`,
            }
          : workspace,
      ))
    } catch (error) {
      notify.error(friendlyMessage(error))
      throw error
    }
  }, [workspaces])

  const renameSession = useCallback(async (sessionId: string, displayName: string) => {
    try {
      const current = sessionsRef.current.find((session) => session.id === sessionId)
      const result = await apiRenameSession(sessionId, displayName, current?.displayNameUpdatedAt)
      updateSession(sessionId, (session) => ({
        ...session,
        title: result.display_name,
        displayNameSource: result.display_name_source,
        displayNameUpdatedAt: result.display_name_updated_at,
      }))
      broadcastSessionChange(sessionId)
    } catch (error) {
      notify.error(friendlyMessage(error))
      throw error
    }
  }, [broadcastSessionChange, updateSession])

  const removeDeletedSessions = useCallback((deletedSessionIds: string[]) => {
    const deleted = new Set(deletedSessionIds)
    if (deleted.size === 0) return
    for (const taskId of [...activeRunByTaskRef.current.keys()]) {
      const run = activeRunByTaskRef.current.get(taskId)
      if (run && deleted.has(run.sessionId)) finishRun(taskId, 'cancelled')
    }
    const remaining = sessionsRef.current.filter((session) => !deleted.has(session.id))
    for (const sessionId of deleted) loadedRef.current.delete(sessionId)
    setHistoryFailures((current) => {
      const next = new Set(current)
      for (const sessionId of deleted) next.delete(sessionId)
      return next
    })
    setSessions(remaining)
    if (activeIdRef.current && deleted.has(activeIdRef.current)) {
      const next = remaining[0] ?? null
      activeIdRef.current = next?.id ?? null
      setActiveId(next?.id ?? null)
      setHistoryStatus(next && !loadedRef.current.has(next.id) ? 'loading' : 'loaded')
    }
    broadcastSessionChange()
  }, [broadcastSessionChange, finishRun])

  const deleteSession = useCallback(async (sessionId: string) => {
    if (findActiveRun(sessionId)) {
      notify.warning('请先停止当前运行，再删除会话')
      return
    }
    try {
      const result = await apiDeleteSession(sessionId)
      removeDeletedSessions(result.deleted_session_ids)
      notify.success('会话及其本地历史已永久删除')
    } catch (error) {
      notify.error(friendlyMessage(error))
      throw error
    }
  }, [findActiveRun, removeDeletedSessions])

  const deleteWorkspace = useCallback(async (deviceId: string, workspaceId: string) => {
    const runningSession = sessionsRef.current.find((session) =>
      session.deviceId === deviceId
      && session.workspaceId === workspaceId
      && findActiveRun(session.id),
    )
    if (runningSession) {
      notify.warning('请先停止该工作区正在运行的任务，再删除工作区')
      return
    }
    try {
      const result = await apiDeleteWorkspace(deviceId, workspaceId)
      setWorkspaces((current) => current.filter((workspace) => !(
        workspace.device_id === deviceId && workspace.workspace_id === workspaceId
      )))
      removeDeletedSessions(result.deleted_session_ids)
      notify.success('工作区授权及其本地会话已删除，项目目录未被修改')
    } catch (error) {
      notify.error(friendlyMessage(error))
      throw error
    }
  }, [findActiveRun, removeDeletedSessions])

  return {
    sessions,
    activeId,
    active: activeId ? sessions.find((s) => s.id === activeId) ?? null : null,
    workspaces,
    runtimeConfig,
    skills,
    mcpServers,
    loading,
    historyStatus,
    running: activeId ? runningSessionIds.has(activeId) : false,
    stopping: activeId ? stoppingSessionIds.has(activeId) : false,
    agentProgress: activeId ? agentProgressBySession.get(activeId) ?? null : null,
    refreshWorkspaces,
    retryHistory,
    select,
    createSession,
    sendMessage,
    renameDevice,
    renameWorkspace,
    renameSession,
    deleteSession,
    deleteWorkspace,
    approveTool,
    rejectTool,
    stopRun,
    selectRun,
  }
}

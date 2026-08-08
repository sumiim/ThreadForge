import { message as notify } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelTask,
  createSession as apiCreateSession,
  createTask,
  friendlyMessage,
  getSession,
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
  ModelCapability,
  PendingApproval,
  RunEventEnvelope,
  RuntimeConfig,
  Session,
  SessionDetail,
  SessionMessage,
  ReasoningEffort,
  RunIndexItem,
  SessionRun,
  SkillMetadata,
  ToolCall,
  Workspace,
} from '../api/types'
import {
  getFinalAnswer,
  getLatestTask,
  historyAllowsSending,
  isInternalReviewDiagnostic,
  reconcileToolCalls,
  resolveHistoryStatus,
} from './session-state.ts'
import type { HistoryStatus } from './session-state.ts'
import { workspaceKey } from '../features/sessions/workspaceIdentity'

let idCounter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${idCounter++}`

const automaticSessionTitle = (request: string) => request.trim().replace(/\s+/g, ' ').slice(0, 200) || '新会话'

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

function terminalFailureMessage(data: Record<string, unknown>): string {
  const code = String(data.error_code ?? '')
  if (code && modelFailureMessages[code]) return modelFailureMessages[code]
  const status = String(data.status ?? '')
  if (status === 'cancelled') return '已停止当前任务。'
  if (status === 'interrupted') return '运行因服务重启或连接中断而终止，请重新执行。'
  if (status === 'blocked') return '运行未通过完成门禁，请根据当前提示调整后重试。'
  return status === 'failed' ? 'Agent 运行失败，请稍后重试。' : ''
}

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
  sendMessage: (content: string, modelId?: string, reasoningEffort?: ReasoningEffort) => void
  renameDevice: (deviceId: string, displayName: string) => Promise<void>
  renameWorkspace: (deviceId: string, workspaceId: string, displayName: string) => Promise<void>
  renameSession: (sessionId: string, displayName: string) => Promise<void>
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
  const [running, setRunning] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [agentProgress, setAgentProgress] = useState<AgentProgress | null>(null)
  const [syncVersion, setSyncVersion] = useState(0)

  // 已从后端拉过完整消息的会话（避免选中时重复请求覆盖本地流式状态）
  const loadedRef = useRef<Set<string>>(new Set())
  const activeIdRef = useRef<string | null>(null)
  // 当前在跑的任务（后端单活跃任务）
  const activeRunRef = useRef<ActiveRun | null>(null)
  const cancelRequestedRef = useRef(false)
  const channelRef = useRef<BroadcastChannel | null>(null)
  const esRef = useRef<EventSource | null>(null)
  const postToolWatchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null)
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
      if (postToolWatchdogRef.current) clearTimeout(postToolWatchdogRef.current)
      esRef.current?.close()
      esRef.current = null
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

  // 当前运行的会话消息流关闭：清运行态、封口 placeholder、结束事件流
  const finishRun = useCallback((terminalStatus = 'completed') => {
    const run = activeRunRef.current
    if (!run) return
    activeRunRef.current = null
    if (postToolWatchdogRef.current) {
      clearTimeout(postToolWatchdogRef.current)
      postToolWatchdogRef.current = null
    }
    setRunning(false)
    setStopping(false)
    setAgentProgress(null)
    cancelRequestedRef.current = false
    esRef.current?.close()
    esRef.current = null
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
  }, [broadcastSessionChange, updateSessionMessages])

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
      esRef.current?.close()
      const es = openTaskEventStream(taskId)
      esRef.current = es

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
            const existing = (m.toolCalls ?? []).find((t) => t.id === toolCallId)
            if (existing) {
              if (!args || Object.keys(args).length === 0 || existing.args) return m
              return {
                ...m,
                toolCalls: (m.toolCalls ?? []).map((t) => (t.id === toolCallId ? { ...t, args } : t)),
              }
            }
            return {
              ...m,
              toolCalls: [...(m.toolCalls ?? []), { id: toolCallId, toolName, args, status: 'running' }],
            }
          }),
        )
      }
      const markToolByEvent = (
        type: 'completed' | 'error',
        toolCallId: string,
        toolName: string,
        result?: string,
      ) => {
        const session = sessionsRef.current.find((s) => s.id === sessionId)
        const message = session?.messages.find((m) => m.id === assistantId)
        if (!message) return
        const tool = toolCallId
          ? findTool(sessionId, assistantId, toolCallId)
          : findLatestToolByName(sessionId, assistantId, toolName)
        if (!tool) return
        updateTool(sessionId, assistantId, tool.id, (t) => ({
          ...t,
          status: type,
          result: result ?? t.result,
        }))
      }

      const appendRunIndex = (envelope: RunEventEnvelope) => {
        const labels: Record<string, string> = {
          'plan.created': '计划已创建',
          'assistant.commentary': '过程更新',
          'review.started': '开始审查',
          'review.completed': '审查完成',
          'tool.started': '工具开始',
          'tool.completed': '工具完成',
          'tool.failed': '工具失败',
          'task.cancel_requested': '正在停止',
          'task.completed': '运行完成',
          'task.cancelled': '运行已取消',
          'task.failed': '运行失败',
          'task.interrupted': '运行已中断',
          'task.blocked': '运行受阻',
        }
        if (!labels[envelope.type]) return
        const item: RunIndexItem = {
          event_id: envelope.event_id,
          type: envelope.type,
          timestamp: envelope.timestamp,
          label: labels[envelope.type],
          tool_name: envelope.data.tool_name ? String(envelope.data.tool_name) : undefined,
          tool_call_id: envelope.data.tool_call_id ? String(envelope.data.tool_call_id) : undefined,
          intent: envelope.data.intent ? String(envelope.data.intent) : undefined,
          step_count: envelope.data.step_count == null ? undefined : Number(envelope.data.step_count),
          status: envelope.data.status ? String(envelope.data.status) : undefined,
          run_id: envelope.run_id,
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
          message.id === assistantId
            ? {
                ...message,
                activity: [...(message.activity ?? []), {
                  id: envelope.event_id,
                  type: envelope.type,
                  label,
                  detail: detail?.trim() || undefined,
                  createdAt: envelope.timestamp,
                }].slice(-50),
              }
            : message,
        ))
      }

      const handleEnvelope = (envelope: RunEventEnvelope) => {
        const { type, data } = envelope
        appendRunIndex(envelope)
        switch (type) {
          case 'task.snapshot': {
            const status = String(data.status ?? '')
            if (data.phase) setAgentProgress(parseAgentProgress(data))
            if (Array.isArray(data.run_index)) {
              updateSession(sessionId, (session) => ({
                ...session,
                runIndex: data.run_index as RunIndexItem[],
                runs: (session.runs ?? []).map((run) =>
                  run.runId === String(data.run_id ?? envelope.run_id)
                    ? {
                        ...run,
                        status,
                        updatedAt: String(data.updated_at ?? run.updatedAt),
                        items: data.run_index as RunIndexItem[],
                      }
                    : run,
                ),
              }))
            }
            const finalAnswer = getFinalAnswer(data) ?? terminalFailureMessage(data)
            if (finalAnswer) {
              updateSessionMessages(sessionId, (messages) =>
                messages.map((m) => (m.id === assistantId ? { ...m, content: finalAnswer } : m)),
              )
            }
            if (TERMINAL_STATUSES.has(status)) {
              finishRun(status)
              return
            }
            if (data.pending_approval) {
              ensureApprovalCard(sessionId, assistantId, taskId, data.pending_approval as PendingApproval)
            }
            return
          }
          case 'agent.state': {
            setAgentProgress(parseAgentProgress(data))
            return
          }
          case 'model.started':
          case 'model.completed': {
            return
          }
          case 'plan.created': {
            const steps = Array.isArray(data.steps) ? data.steps : []
            setAgentProgress({
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
          case 'assistant.commentary': {
            const text = String(data.text ?? '').trim()
            if (text) appendActivity(envelope, '过程更新', text)
            return
          }
          case 'review.started': {
            setAgentProgress((progress) => progress ? { ...progress, phase: 'REVIEW', nextStep: '正在审查结果' } : progress)
            appendActivity(envelope, '开始审查')
            return
          }
          case 'review.completed': {
            setAgentProgress((progress) => progress ? { ...progress, nextStep: `审查结果：${String(data.status ?? '')}` } : progress)
            appendActivity(envelope, '审查完成', `结果：${String(data.status ?? '')}`)
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
            const tool = toolCallId
              ? findTool(sessionId, assistantId, toolCallId)
              : findLatestToolByName(sessionId, assistantId, toolName)
            if (!tool) return
            updateTool(sessionId, assistantId, tool.id, (t) => ({ ...t, status: 'running' }))
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
            setStopping(true)
            setAgentProgress((current) => current ? {
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
        if (postToolWatchdogRef.current) {
          clearTimeout(postToolWatchdogRef.current)
          postToolWatchdogRef.current = null
        }
        handleEnvelope(envelope)
        if (envelope.type === 'tool.completed' || envelope.type === 'tool.failed') {
          postToolWatchdogRef.current = setTimeout(() => {
            const run = activeRunRef.current
            if (!run || run.taskId !== taskId) return
            setAgentProgress((current) => ({
              phase: current?.phase || 'ANALYZE_CONTEXT',
              nextStep: '模型仍在结合工具结果推理，可以继续等待或停止任务',
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
          }, 45_000)
        }
        if (TERMINAL_EVENTS.has(envelope.type)) {
          finishRun(envelope.type.slice('task.'.length))
        }
      }

      // 命名事件 + 兜底 message 事件（后端帧均带 event 名）
      const NAMED = [
        'task.snapshot',
        'agent.state',
        'model.started',
        'model.completed',
        'plan.created',
        'assistant.commentary',
        'review.started',
        'review.completed',
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
    [ensureApprovalCard, findLatestToolByName, findTool, finishRun, updateSession, updateSessionMessages, updateTool],
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
            title: item.title,
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
        setSessions(items)
        if (items.length > 0) {
          setHistoryStatus('loading')
          activeIdRef.current = items[0].id
          setActiveId(items[0].id)
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
            items: task.run_index ?? [],
          }))
        const loaded: Session = {
          id: detail.session_id,
          title: detail.title,
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
          setRunning(true)
          activeRunRef.current = {
            taskId: lastTask.task_id,
            runId: lastTask.run_id,
            sessionId: detail.session_id,
            assistantId,
          }
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
  }, [activeId, attachEventStream, historyFailures, runtimeConfig, syncVersion, workspaces])

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
            if (currentSession?.displayNameUpdatedAt === displayNameUpdatedAt && currentSession?.createdAt === item.created_at && currentSession?.title === item.title && currentSession.updatedAt === updatedAt) {
              return currentSession
            }
            if (currentSession && activeRunRef.current?.sessionId !== item.session_id) {
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
              title: item.title,
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
          return next
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
  }, [runtimeConfig?.model])

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
            title: res.title,
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
    (content: string, modelId?: string, reasoningEffort: ReasoningEffort = 'none') => {
      const sessionId = activeId
      if (!sessionId || !content.trim() || running) return
      const activeSession = sessionsRef.current.find((session) => session.id === sessionId)
      const historyStatus = resolveHistoryStatus(sessionId, loadedRef.current, historyFailures)
      if (!activeSession || !historyAllowsSending(Boolean(activeSession.draft), historyStatus)) {
        notify.warning(historyStatus === 'error' ? '请先重新加载会话历史' : '会话历史仍在加载，请稍候')
        return
      }
      setRunning(true)
      setStopping(false)
      cancelRequestedRef.current = false
      setAgentProgress(null)

      const now = new Date().toISOString()
      const firstRequestTitle = activeSession?.draft && activeSession.displayNameSource !== 'user'
        ? automaticSessionTitle(content)
        : undefined
      const userId = nextId('m-user')
      const assistantId = nextId('m-agent')
      updateSessionMessages(sessionId, (messages) => [
        ...messages,
        { id: userId, role: 'user', content: content.trim(), createdAt: now },
        { id: assistantId, role: 'assistant', content: '', createdAt: now, status: 'streaming' },
      ])
      updateSession(sessionId, (session) => ({
        ...session,
        draft: false,
        title: firstRequestTitle ?? session.title,
      }))
      broadcastSessionChange(sessionId)

      ;(async () => {
        try {
          const queued = await createTask(
            sessionId,
            content.trim(),
            undefined,
            modelId,
            reasoningEffort,
          )
          activeRunRef.current = {
            taskId: queued.task_id,
            runId: queued.run_id,
            sessionId,
            assistantId,
          }
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
                items: [],
              },
            ],
            activeRunId: queued.run_id,
          }))
          attachEventStream(queued.task_id, sessionId, assistantId)
          broadcastSessionChange(sessionId)
          if (cancelRequestedRef.current) {
            try {
              await cancelTask(queued.task_id)
            } catch (error) {
              setStopping(false)
              notify.error(friendlyMessage(error))
            }
          }
        } catch (err) {
          setRunning(false)
          setStopping(false)
          cancelRequestedRef.current = false
          activeRunRef.current = null
          // 任务未创建成功：移除占位 assistant 消息，保留用户消息
          updateSessionMessages(sessionId, (messages) => messages.filter((m) => m.id !== assistantId))
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [activeId, attachEventStream, broadcastSessionChange, historyFailures, running, updateSession, updateSessionMessages],
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
    cancelRequestedRef.current = true
    setStopping(true)
    const run = activeRunRef.current
    if (!run) {
      setAgentProgress((current) => current ? {
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
          finishRun(snapshot.status)
        }
        // 否则等待 task.cancelled 事件收尾
      } catch (err) {
        setStopping(false)
        cancelRequestedRef.current = false
        notify.error(friendlyMessage(err))
      }
    })()
  }, [finishRun])

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
    running,
    stopping,
    agentProgress,
    refreshWorkspaces,
    retryHistory,
    select,
    createSession,
    sendMessage,
    renameDevice,
    renameWorkspace,
    renameSession,
    approveTool,
    rejectTool,
    stopRun,
    selectRun,
  }
}

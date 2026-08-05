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
} from '../api/client'
import type {
  McpServerMetadata,
  Message,
  PendingApproval,
  RunEventEnvelope,
  RuntimeConfig,
  Session,
  SessionDetail,
  SkillMetadata,
  ToolCall,
  Workspace,
} from '../api/types'
import { getFinalAnswer, getLatestTask } from './session-state.ts'

let idCounter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${idCounter++}`

// 与后端 TaskStatus 对齐
const TERMINAL_STATUSES = new Set(['completed', 'cancelled', 'failed'])
const RUNNING_STATUSES = new Set(['queued', 'running', 'waiting_for_approval', 'cancel_requested'])
const TERMINAL_EVENTS = new Set(['task.completed', 'task.cancelled', 'task.failed'])

function sessionModel(workspaceId: string, workspaces: Workspace[], serverModel: string): string {
  const workspace = workspaces.find((item) => item.workspace_id === workspaceId)
  if (workspace?.execution_environment === 'local_worker') {
    return workspace.model_configured ? (workspace.model ?? '本地模型') : '本地模型未配置'
  }
  return serverModel
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
  running: boolean
  refreshWorkspaces: () => Promise<Workspace[]>
  select: (id: string) => void
  createSession: (workspaceId: string) => void
  sendMessage: (content: string) => void
  approveTool: (messageId: string, toolCallId: string) => void
  rejectTool: (messageId: string, toolCallId: string) => void
  stopRun: () => void
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
  const [running, setRunning] = useState(false)

  // 已从后端拉过完整消息的会话（避免选中时重复请求覆盖本地流式状态）
  const loadedRef = useRef<Set<string>>(new Set())
  // 当前在跑的任务（后端单活跃任务）
  const activeRunRef = useRef<ActiveRun | null>(null)
  const esRef = useRef<EventSource | null>(null)
  // approval_id -> tool call（approval.required 建立，resolved 时回查）
  const approvalMapRef = useRef<Map<string, string>>(new Map())
  // 事件回调里需要读到最新 sessions（在 effect 中同步，避免 render 期间改 ref）
  const sessionsRef = useRef<Session[]>([])
  useEffect(() => {
    sessionsRef.current = sessions
  }, [sessions])

  useEffect(
    () => () => {
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
  const finishRun = useCallback(() => {
    const run = activeRunRef.current
    if (!run) return
    activeRunRef.current = null
    setRunning(false)
    esRef.current?.close()
    esRef.current = null
    updateSessionMessages(run.sessionId, (messages) =>
      messages.map((m) =>
        m.id === run.assistantId
          ? {
              ...m,
              status: 'done' as const,
              toolCalls: m.toolCalls?.map((t) =>
                t.status === 'running'
                  ? { ...t, status: 'error' as const, result: '任务已停止，工具未完成' }
                  : t.status === 'pending'
                    ? { ...t, status: 'rejected' as const, result: '任务已结束，未执行' }
                    : t,
              ),
            }
          : m,
      ),
    )
  }, [updateSessionMessages])

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
      const ensureToolCard = (toolCallId: string, toolName: string) => {
        updateSessionMessages(sessionId, (messages) =>
          messages.map((m) => {
            if (m.id !== assistantId) return m
            if ((m.toolCalls ?? []).some((t) => t.id === toolCallId)) return m
            return { ...m, toolCalls: [...(m.toolCalls ?? []), { id: toolCallId, toolName, status: 'running' }] }
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

      const handleEnvelope = (envelope: RunEventEnvelope) => {
        const { type, data } = envelope
        switch (type) {
          case 'task.snapshot': {
            const status = String(data.status ?? '')
            const finalAnswer = getFinalAnswer(data)
            if (finalAnswer) {
              updateSessionMessages(sessionId, (messages) =>
                messages.map((m) => (m.id === assistantId ? { ...m, content: finalAnswer } : m)),
              )
            }
            if (TERMINAL_STATUSES.has(status)) {
              finishRun()
              return
            }
            if (data.pending_approval) {
              ensureApprovalCard(sessionId, assistantId, taskId, data.pending_approval as PendingApproval)
            }
            return
          }
          case 'tool.requested': {
            const toolCallId = String(data.tool_call_id ?? '')
            const toolName = String(data.tool_name ?? '')
            if (toolCallId) ensureToolCard(toolCallId, toolName)
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
            return
          }
          case 'tool.completed':
          case 'tool.failed': {
            const toolCallId = String(data.tool_call_id ?? '')
            const toolName = String(data.tool_name ?? '')
            const paths = (data.affected_paths ?? []) as string[]
            markToolByEvent(
              type === 'tool.completed' ? 'completed' : 'error',
              toolCallId,
              toolName,
              paths.length > 0 ? `影响路径：${paths.join('、')}` : undefined,
            )
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
            updateSessionMessages(sessionId, (messages) =>
              messages.map((m) => (m.id === assistantId ? { ...m, content: text } : m)),
            )
            return
          }
          case 'task.completed':
          case 'task.cancelled':
          case 'task.failed': {
            const finalAnswer = getFinalAnswer(data)
            if (finalAnswer) {
              updateSessionMessages(sessionId, (messages) =>
                messages.map((m) => (m.id === assistantId ? { ...m, content: finalAnswer } : m)),
              )
            }
            return
          }
          default:
            return
        }
      }

      const onFrame = (event: MessageEvent) => {
        const envelope = parse(event)
        if (!envelope) return
        handleEnvelope(envelope)
        if (TERMINAL_EVENTS.has(envelope.type)) finishRun()
      }

      // 命名事件 + 兜底 message 事件（后端帧均带 event 名）
      const NAMED = [
        'task.snapshot',
        'tool.requested',
        'tool.started',
        'tool.completed',
        'tool.failed',
        'approval.required',
        'approval.resolved',
        'message.completed',
        'task.completed',
        'task.cancelled',
        'task.failed',
      ]
      NAMED.forEach((name) => es.addEventListener(name, onFrame))
      es.addEventListener('message', onFrame)
      // 断开后 EventSource 自动重连，重连成功会重发 task.snapshot；无需额外处理
    },
    [ensureApprovalCard, findLatestToolByName, findTool, finishRun, updateSessionMessages, updateTool],
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
            workspaceId: item.workspace_id,
            executionEnvironment: item.execution_environment,
            deviceId: item.device_id,
            model: sessionModel(item.workspace_id, wsRes.items, configRes.model),
            messages: [],
          }))
        setSessions(items)
        if (items.length > 0) setActiveId(items[0].id)
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
    if (!activeId || loadedRef.current.has(activeId)) return
    let cancelled = false
    ;(async () => {
      try {
        const detail: SessionDetail = await getSession(activeId, 200)
        if (cancelled) return
        loadedRef.current.add(activeId)

        const messages: Message[] = detail.messages.map((m, i) => ({
          id: `m-${detail.session_id}-${i}`,
          role: m.role === 'assistant' ? 'assistant' : 'user',
          content: m.content,
          createdAt: m.created_at,
          status: 'done' as const,
        }))
        const lastTask = getLatestTask(detail.tasks)
        const loaded: Session = {
          id: detail.session_id,
          title: detail.title,
          createdAt: detail.created_at,
          workspaceId: detail.workspace_id,
          executionEnvironment: detail.execution_environment,
          deviceId: detail.device_id,
          model: sessionModel(detail.workspace_id, workspaces, runtimeConfig?.model ?? '未配置'),
          messages,
          lastTaskId: lastTask?.task_id,
          lastRunId: lastTask?.run_id,
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
      } catch (err) {
        if (!cancelled) notify.error(friendlyMessage(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [activeId, attachEventStream, runtimeConfig, workspaces])

  // ---- 用户操作 ---------------------------------------------------------------

  const select = useCallback((id: string) => setActiveId(id), [])

  const createSession = useCallback(
    (workspaceId: string) => {
      ;(async () => {
        try {
          const res = await apiCreateSession(workspaceId)
          const session: Session = {
            id: res.session_id,
            title: res.title,
            createdAt: res.created_at,
            workspaceId: res.workspace_id,
            executionEnvironment: res.execution_environment,
            deviceId: res.device_id,
            model: sessionModel(res.workspace_id, workspaces, runtimeConfig?.model ?? '未配置'),
            messages: [],
          }
          loadedRef.current.add(session.id)
          setSessions((prev) => [session, ...prev])
          setActiveId(session.id)
        } catch (err) {
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [runtimeConfig, workspaces],
  )

  const sendMessage = useCallback(
    (content: string) => {
      const sessionId = activeId
      if (!sessionId || !content.trim() || running) return
      setRunning(true)

      const now = new Date().toISOString()
      const userId = nextId('m-user')
      const assistantId = nextId('m-agent')
      updateSessionMessages(sessionId, (messages) => [
        ...messages,
        { id: userId, role: 'user', content: content.trim(), createdAt: now },
        { id: assistantId, role: 'assistant', content: '', createdAt: now, status: 'streaming' },
      ])

      ;(async () => {
        try {
          const queued = await createTask(sessionId, content.trim())
          activeRunRef.current = {
            taskId: queued.task_id,
            runId: queued.run_id,
            sessionId,
            assistantId,
          }
          updateSession(sessionId, (s) => ({ ...s, lastTaskId: queued.task_id, lastRunId: queued.run_id }))
          attachEventStream(queued.task_id, sessionId, assistantId)
        } catch (err) {
          setRunning(false)
          activeRunRef.current = null
          // 任务未创建成功：移除占位 assistant 消息，保留用户消息
          updateSessionMessages(sessionId, (messages) => messages.filter((m) => m.id !== assistantId))
          notify.error(friendlyMessage(err))
        }
      })()
    },
    [activeId, attachEventStream, running, updateSession, updateSessionMessages],
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
    const run = activeRunRef.current
    if (!run) return
    ;(async () => {
      try {
        const snapshot = await cancelTask(run.taskId)
        if (TERMINAL_STATUSES.has(snapshot.status)) {
          // 后端直接返回终态（任务已结束），事件流可能已断：本地收尾
          finishRun()
        }
        // 否则等待 task.cancelled 事件收尾
      } catch (err) {
        notify.error(friendlyMessage(err))
      }
    })()
  }, [finishRun])

  return {
    sessions,
    activeId,
    active: activeId ? sessions.find((s) => s.id === activeId) ?? null : null,
    workspaces,
    runtimeConfig,
    skills,
    mcpServers,
    loading,
    running,
    refreshWorkspaces,
    select,
    createSession,
    sendMessage,
    approveTool,
    rejectTool,
    stopRun,
  }
}

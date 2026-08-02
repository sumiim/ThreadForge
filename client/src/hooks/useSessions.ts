import { useCallback, useRef, useState } from 'react'
import { mockSessions, buildMockToolCall } from '../api/mock'
import type { Session } from '../api/types'

let idCounter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${idCounter++}`

export interface UseSessions {
  sessions: Session[]
  activeId: string
  active: Session
  running: boolean
  select: (id: string) => void
  createSession: (workspace: string) => void
  sendMessage: (content: string) => void
  approveTool: (messageId: string, toolCallId: string) => void
  rejectTool: (messageId: string, toolCallId: string) => void
  stopRun: () => void
  updateModel: (model: string) => void
}

// Session 列表、选中与模拟运行状态。后端就绪后，sendMessage/审批/停止将对接 REST 与 SSE
export function useSessions(): UseSessions {
  const [sessions, setSessions] = useState<Session[]>(mockSessions)
  const [activeId, setActiveId] = useState(mockSessions[0].id)
  const [running, setRunning] = useState(false)
  const timersRef = useRef<number[]>([])

  const schedule = useCallback((fn: () => void, delay: number) => {
    timersRef.current.push(window.setTimeout(fn, delay))
  }, [])

  const updateSessionMessages = useCallback(
    (sessionId: string, updater: (messages: Session['messages']) => Session['messages']) => {
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, messages: updater(s.messages) } : s)),
      )
    },
    [],
  )

  const stopRun = useCallback(() => {
    timersRef.current.forEach(clearTimeout)
    timersRef.current = []
    setRunning(false)
    setSessions((prev) =>
      prev.map((s) => ({
        ...s,
        messages: s.messages.map((m) =>
          m.status === 'streaming' ? { ...m, status: 'done' } : m,
        ),
      })),
    )
  }, [])

  const sendMessage = useCallback(
    (content: string) => {
      if (!content.trim() || running) return
      setRunning(true)

      const sessionId = activeId
      const userId = nextId('m-user')
      updateSessionMessages(sessionId, (messages) => [
        ...messages,
        { id: userId, role: 'user', content: content.trim(), createdAt: new Date().toISOString() },
      ])

      // 以下为模拟 Agent 运行，将来替换为 SSE 事件流
      const assistantId = nextId('m-agent')
      const toolId = nextId('t')

      // 1. Agent 先给出过渡文本，同时发起一个待审批的工具调用
      schedule(() => {
        updateSessionMessages(sessionId, (messages) => [
          ...messages,
          {
            id: assistantId,
            role: 'assistant',
            content: '我先执行一个只读命令确认现状，需要你的批准：',
            createdAt: new Date().toISOString(),
            status: 'streaming',
            toolCalls: [{ ...buildMockToolCall(toolId), status: 'running' }],
          },
        ])
      }, 700)

      // 2. 工具执行完成，追加结果
      schedule(() => {
        updateSessionMessages(sessionId, (messages) =>
          messages.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  toolCalls: m.toolCalls?.map((t) =>
                    t.id === toolId
                      ? { ...t, status: 'completed', result: '共发现 3 处 TODO，位于 src/features/ 下。' }
                      : t,
                  ),
                }
              : m,
          ),
        )
      }, 2600)

      // 3. 最终回答
      schedule(() => {
        updateSessionMessages(sessionId, (messages) =>
          messages.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: `已找到 3 处 TODO。需要我逐个处理吗？\n\n（此为模拟运行结果，接入 api-server 后由真实 Agent 回答）`,
                  status: 'done',
                }
              : m,
          ),
        )
        setRunning(false)
      }, 3600)
    },
    [activeId, running, schedule, updateSessionMessages],
  )

  const approveTool = useCallback(
    (messageId: string, toolCallId: string) => {
      updateSessionMessages(activeId, (messages) =>
        messages.map((m) =>
          m.id === messageId
            ? {
                ...m,
                toolCalls: m.toolCalls?.map((t) =>
                  t.id === toolCallId
                    ? { ...t, status: 'completed', result: '已批准执行，修改已应用。' }
                    : t,
                ),
              }
            : m,
        ),
      )
    },
    [activeId, updateSessionMessages],
  )

  const rejectTool = useCallback(
    (messageId: string, toolCallId: string) => {
      updateSessionMessages(activeId, (messages) =>
        messages.map((m) =>
          m.id === messageId
            ? {
                ...m,
                toolCalls: m.toolCalls?.map((t) =>
                  t.id === toolCallId ? { ...t, status: 'rejected', result: '已拒绝该操作。' } : t,
                ),
              }
            : m,
        ),
      )
    },
    [activeId, updateSessionMessages],
  )

  const updateModel = useCallback(
    (model: string) => {
      setSessions((prev) => prev.map((s) => (s.id === activeId ? { ...s, model } : s)))
    },
    [activeId],
  )

  const createSession = useCallback(
    (workspace: string) => {
      const id = nextId('s')
      const session: Session = {
        id,
        title: '新会话',
        createdAt: new Date().toISOString(),
        workspace,
        model: mockSessions[0].model,
        messages: [],
      }
      setSessions((prev) => [session, ...prev])
      setActiveId(id)
    },
    [],
  )

  const select = useCallback((id: string) => setActiveId(id), [])

  return {
    sessions,
    activeId,
    active: sessions.find((s) => s.id === activeId) ?? sessions[0],
    running,
    select,
    createSession,
    sendMessage,
    approveTool,
    rejectTool,
    stopRun,
    updateModel,
  }
}

import type { RunArtifacts, Session, ToolCall, TraceEvent } from './types'

// 占位数据：展示 Session 列表、工具调用与审批交互；api-server 就绪后移除
export const MODEL_OPTIONS = ['claude-sonnet-5', 'claude-opus-5', 'deepseek-v4-flash']

// 允许的工作区：对应 GET /api/v1/workspaces
export const WORKSPACE_OPTIONS = [
  'd:\\pro\\ThreadForge',
  'd:\\pro\\demo-project',
  'd:\\pro\\sandbox',
]

// 侧边栏 Skills 栏（V1 仅展示，执行能力后续版本接入）
export const SKILL_OPTIONS = [
  { id: 'code-review', name: 'code-review', desc: '代码评审' },
  { id: 'security-review', name: 'security-review', desc: '安全审查' },
  { id: 'simplify', name: 'simplify', desc: '代码简化' },
] as const

// 侧边栏 MCP 栏（V1 仅展示连接状态，实际接入规划在后续版本）
export const MCP_OPTIONS = [
  { id: 'filesystem', name: 'filesystem', desc: '本地文件系统访问', status: 'connected' },
  { id: 'github', name: 'github', desc: 'GitHub 仓库与 Issue 操作', status: 'connected' },
  { id: 'fetch', name: 'fetch', desc: 'HTTP 抓取与网页内容读取', status: 'disconnected' },
] as const

export const mockSessions: Session[] = [
  {
    id: 's-1',
    title: '分析当前项目结构',
    createdAt: '2026-08-02T10:24:00+08:00',
    workspace: 'd:\\pro\\ThreadForge',
    model: 'claude-sonnet-5',
    messages: [
      {
        id: 'm-1-1',
        role: 'user',
        content: '分析当前项目结构，告诉我各模块的职责',
        createdAt: '2026-08-02T10:24:00+08:00',
      },
      {
        id: 'm-1-2',
        role: 'assistant',
        content: '我先看一下仓库根目录的结构。',
        createdAt: '2026-08-02T10:24:01+08:00',
        status: 'done',
        toolCalls: [
          {
            id: 't-1-1',
            toolName: 'run_shell',
            args: { command: 'ls -la && cat README.md | head -40' },
            status: 'completed',
            result: 'Agent Orchestrator（LangGraph）、API Server（FastAPI）、Client（React）等模块均已就位。',
            requiresApproval: false,
          },
        ],
      },
      {
        id: 'm-1-3',
        role: 'assistant',
        content:
          '当前仓库是一个 monorepo：\n\n- **client/**：React 19 + antd v6 Web 工作台\n- **api-server/**：FastAPI REST 与 SSE 服务（规划中）\n- **agent-orchestrator/**：LangGraph 编排与意图路由\n- **pico-legacy-runtime/**：已归档的 Pico Runtime 与评测体系\n\n需要我深入分析某个模块吗？',
        createdAt: '2026-08-02T10:24:05+08:00',
        status: 'done',
      },
    ],
  },
  {
    id: 's-2',
    title: '实现 session 列表 API',
    createdAt: '2026-08-02T11:02:00+08:00',
    workspace: 'd:\\pro\\ThreadForge',
    model: 'claude-sonnet-5',
    messages: [
      {
        id: 'm-2-1',
        role: 'user',
        content: '在 api-server 中实现 session 列表 API，支持按创建时间倒序',
        createdAt: '2026-08-02T11:02:00+08:00',
      },
      {
        id: 'm-2-2',
        role: 'assistant',
        content: '好的，我先确认 api-server 的现有结构，然后编写实现。',
        createdAt: '2026-08-02T11:02:01+08:00',
        status: 'done',
        toolCalls: [
          {
            id: 't-2-1',
            toolName: 'write_file',
            args: { path: 'api-server/src/routes/sessions.py', content: '...（新文件 86 行）' },
            status: 'pending',
            requiresApproval: true,
          },
        ],
      },
      {
        id: 'm-2-3',
        role: 'assistant',
        content: '已完成 API 实现：`GET /api/sessions` 返回按创建时间倒序的会话列表。',
        createdAt: '2026-08-02T11:03:12+08:00',
        status: 'done',
        toolCalls: [
          {
            id: 't-2-2',
            toolName: 'patch_file',
            args: { path: 'api-server/src/routes/sessions.py', patches: 3 },
            status: 'rejected',
            result: '用户拒绝了本次修改',
            requiresApproval: true,
          },
        ],
      },
    ],
  },
  {
    id: 's-3',
    title: '新会话',
    createdAt: '2026-08-02T11:40:00+08:00',
    workspace: 'd:\\pro\\ThreadForge',
    model: 'claude-sonnet-5',
    messages: [],
  },
]

// 模拟运行序列：依次触发工具调用与最终回答（将来由 SSE 事件流替代）
export function buildMockToolCall(id: string): ToolCall {
  return {
    id,
    toolName: 'run_shell',
    args: { command: 'grep -r "TODO" src --include="*.ts" | head -10' },
    status: 'pending',
    requiresApproval: true,
  }
}

// 由 Session 消息派生运行结果 artifacts（后端就绪后改由 GET /runs/{id}/artifacts 提供）
export function buildMockArtifacts(session: Session): RunArtifacts | null {
  const toolCalls = session.messages.flatMap((m) => m.toolCalls ?? [])
  if (toolCalls.length === 0) return null

  const pending = toolCalls.filter((t) => t.status === 'pending')
  const rejected = toolCalls.filter((t) => t.status === 'rejected')
  const status = pending.length > 0 ? 'awaiting_approval' : rejected.length > 0 ? 'rejected' : 'completed'

  const trace: TraceEvent[] = session.messages.flatMap((m, mi) => {
    const events: TraceEvent[] = [
      {
        seq: mi * 10 + 1,
        type: 'message.start',
        detail: `${m.role} message`,
        ts: m.createdAt,
      },
    ]
    for (const t of m.toolCalls ?? []) {
      events.push({
        seq: mi * 10 + 2,
        type: `tool.${t.status === 'rejected' ? 'rejected' : 'started'}`,
        detail: `${t.toolName} ${t.status === 'rejected' ? '(rejected)' : ''}`.trim(),
        ts: m.createdAt,
      })
    }
    events.push({ seq: mi * 10 + 9, type: 'message.completed', detail: m.content.slice(0, 40), ts: m.createdAt })
    return events
  })

  const report = [
    `# Run Report - ${session.title}`,
    '',
    `- Workspace: ${session.workspace}`,
    `- Model: ${session.model}`,
    `- 消息数: ${session.messages.length}`,
    `- 工具调用: ${toolCalls.length}（${toolCalls.filter((t) => t.status === 'completed').length} 完成 / ${rejected.length} 拒绝 / ${pending.length} 待审批）`,
    '',
    '本报告由 mock 数据生成，接入 api-server 后返回真实 task_state / trace / report。',
  ].join('\n')

  return {
    taskState: {
      runId: `run-${session.id}`,
      status,
      steps: toolCalls.length,
      toolCalls: toolCalls.map((t) => ({
        toolName: t.toolName,
        status: t.status,
        approvals: t.requiresApproval ? (t.status === 'pending' ? 0 : 1) : 0,
      })),
    },
    trace,
    report,
  }
}

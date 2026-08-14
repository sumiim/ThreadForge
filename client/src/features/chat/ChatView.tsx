import { Button, Spin } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import Logo from '../../components/Logo'
import type { AgentProgress, Session } from '../../api/types'
import type { HistoryStatus } from '../../hooks/session-state'
import ApprovalNotice from './ApprovalNotice'
import MessageList from './MessageList'
import Composer from './Composer'
import RunTimeline from './RunTimeline'

interface ChatViewProps {
  session: Session
  historyStatus: HistoryStatus
  running: boolean
  stopping: boolean
  agentProgress: AgentProgress | null
  onSend: Parameters<typeof Composer>[0]['onSend']
  onRetryHistory: () => void
  onStop: () => void
  onSelectRun: (runId: string) => void
  onApprove: (messageId: string, toolCallId: string) => void
  onReject: (messageId: string, toolCallId: string) => void
}

const suggestions = ['分析当前项目结构', 'Review 最近一次提交', '实现一个 HTTP 服务示例']

const phaseLabels: Record<string, string> = {
  PLANNING: '规划任务',
  UNDERSTAND_REQUEST: '理解需求',
  GATHER_CONTEXT: '收集上下文',
  ANALYZE_CONTEXT: '分析证据',
  ACT_OR_ANSWER: '执行或回答',
  VERIFY: '验证结果',
  REVIEW: '审查结果',
  FINAL: '已完成',
}

export default function ChatView({ session, historyStatus, running, stopping, agentProgress, onSend, onRetryHistory, onStop, onSelectRun, onApprove, onReject }: ChatViewProps) {
  const empty = session.messages.length === 0

  // 待审批的工具调用（per_call_only，逐次审批）
  const pendingApprovals = session.messages.flatMap((m) =>
    (m.toolCalls ?? [])
      .filter((t) => t.requiresApproval && t.status === 'pending')
      .map((t) => t.id),
  )

  const locatePending = () => {
    const first = pendingApprovals[0]
    if (!first) return
    document.getElementById(`tool-call-${first}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {agentProgress ? (
        <div className="border-b border-stone-100 bg-white px-5 py-3 text-xs text-stone-600">
          <div className="flex items-center gap-2">
            <span
              className={`h-2 w-2 rounded-full ${agentProgress.reason === 'post_tool_waiting' ? 'bg-amber-500' : running ? 'bg-blue-500' : 'bg-emerald-500'}`}
              aria-hidden
            />
            <span className="font-medium text-stone-800">阶段：{phaseLabels[agentProgress.phase] ?? agentProgress.phase}</span>
            <span className="ml-auto text-stone-400">
              工具 {agentProgress.toolSteps}/{agentProgress.maxToolSteps || '-'}
              {' · '}读取 {agentProgress.readFiles}/{agentProgress.maxReadFiles || '-'}
            </span>
          </div>
          {agentProgress.nextStep ? <div className="mt-1 truncate text-stone-500">下一步：{agentProgress.nextStep}</div> : null}
          {agentProgress.reason === 'post_tool_waiting' ? (
            <div className="mt-1 text-amber-700">工具已返回，模型仍在继续推理；你可以等待或停止当前任务。</div>
          ) : null}
          {agentProgress.checklist.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-stone-400">
              {agentProgress.checklist.map((item) => (
                <span key={item} className={agentProgress.completedItems.includes(item) ? 'text-emerald-600' : undefined}>
                  {agentProgress.completedItems.includes(item) ? '✓' : '○'} {item}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      <RunTimeline
        runs={session.runs ?? []}
        activeRunId={session.activeRunId ?? session.lastRunId}
        onSelectRun={onSelectRun}
        inputs={session.messages
          .filter((message) => message.role === 'user')
          .map((message) => ({ id: message.id, content: message.content, createdAt: message.createdAt }))}
      />
      <div className="flex min-h-0 flex-1">
      {historyStatus === 'loading' && !session.draft ? (
        <div id="run-scroll-container" className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 text-sm text-stone-500" role="status">
          <Spin size="large" />
          <span>正在读取历史记录...</span>
          <span className="text-xs text-stone-400">正在加载会话消息和运行记录</span>
        </div>
      ) : historyStatus === 'error' && !session.draft ? (
        <div id="run-scroll-container" className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-6 text-center" role="alert">
          <div className="text-sm font-medium text-stone-800">历史记录加载失败</div>
          <div className="text-xs text-stone-500">当前会话尚未就绪，为避免消息发往错误会话，发送功能已暂停。</div>
          <Button icon={<ReloadOutlined />} onClick={onRetryHistory}>重新加载</Button>
        </div>
      ) : empty ? (
        // 空态：引导用户开始，无装饰元素
        <div id="run-scroll-container" className="flex min-h-0 flex-1 flex-col items-center justify-center gap-5 px-6">
          <div className="flex items-center gap-2.5">
            <Logo size={26} />
            <span className="text-lg font-semibold tracking-tight text-stone-900">ThreadForge</span>
          </div>
          <div className="text-center">
            <div className="text-sm text-stone-500">开始一个新的会话</div>
            <div className="mt-1 text-sm text-stone-500">描述你想让 Agent 完成的任务</div>
          </div>
          <div className="flex flex-wrap justify-center gap-2">
            {suggestions.map((s) => (
              <Button
                key={s}
                shape="round"
                disabled={historyStatus !== 'loaded'}
                onClick={() => onSend(s)}
                className="border-stone-200 text-stone-500 hover:!border-blue-500 hover:!text-blue-700"
              >
                {s}
              </Button>
            ))}
          </div>
        </div>
      ) : (
        <MessageList messages={session.messages} onApprove={onApprove} onReject={onReject} />
      )}
      </div>

      <ApprovalNotice count={pendingApprovals.length} onLocate={locatePending} />

      <Composer
        key={session.id}
        model={session.model}
        modelOptions={session.modelOptions}
        running={running}
        stopping={stopping}
        disabled={historyStatus !== 'loaded'}
        onSend={onSend}
        onStop={onStop}
      />
    </div>
  )
}

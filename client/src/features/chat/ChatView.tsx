import { Button } from 'antd'
import Logo from '../../components/Logo'
import type { Session } from '../../api/types'
import ApprovalNotice from './ApprovalNotice'
import MessageList from './MessageList'
import Composer from './Composer'

interface ChatViewProps {
  session: Session
  running: boolean
  onSend: (content: string) => void
  onStop: () => void
  onApprove: (messageId: string, toolCallId: string) => void
  onReject: (messageId: string, toolCallId: string) => void
}

const suggestions = ['分析当前项目结构', 'Review 最近一次提交', '实现一个 HTTP 服务示例']

export default function ChatView({ session, running, onSend, onStop, onApprove, onReject }: ChatViewProps) {
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
      {empty ? (
        // 空态：引导用户开始，无装饰元素
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-5 px-6">
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

      <ApprovalNotice count={pendingApprovals.length} onLocate={locatePending} />

      <Composer model={session.model} running={running} onSend={onSend} onStop={onStop} />
    </div>
  )
}

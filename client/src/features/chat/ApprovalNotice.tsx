import { Button } from 'antd'
import { AlertOutlined, CheckOutlined, CloseOutlined } from '@ant-design/icons'
import type { ToolCall } from '../../api/types'

interface ApprovalNoticeProps {
  pending: ToolCall[]
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}

function summarize(toolCall: ToolCall): string | null {
  const args = toolCall.args
  if (!args) return null
  const priority = ['path', 'file_path', 'command', 'query', 'pattern', 'url', 'description', 'code']
  for (const key of priority) {
    const value = args[key]
    if (typeof value === 'string' && value !== '') return value
  }
  for (const value of Object.values(args)) {
    if (typeof value === 'string' && value !== '') return value
  }
  return null
}

// 输入框上方的待审批面板：列出全部等待审批的工具，就地批准/拒绝
// （V1 为 per_call_only 逐次审批，无自动允许）
export default function ApprovalNotice({ pending, onApprove, onReject }: ApprovalNoticeProps) {
  if (pending.length === 0) return null

  return (
    <div className="mx-auto w-full max-w-4xl shrink-0 px-6 pb-2 lg:px-10">
      <div className="rounded-2xl border border-amber-200 bg-amber-50 px-3.5 py-2.5 dark:border-amber-700/50 dark:bg-amber-900/20">
        <div className="flex items-center gap-2 text-xs text-amber-800 dark:text-amber-300">
          <AlertOutlined />
          <span className="font-medium">{pending.length} 个工具等待你的审批</span>
        </div>
        <div className="mt-2 space-y-1.5">
          {pending.map((toolCall) => {
            const summary = summarize(toolCall)
            return (
              <div
                key={toolCall.id}
                id={`approval-${toolCall.id}`}
                className="flex items-center gap-2 rounded-xl border border-amber-200/80 bg-white/80 px-2.5 py-2 dark:border-amber-700/40 dark:bg-stone-900/40"
              >
                <span className="shrink-0 font-mono text-xs font-medium text-stone-700 dark:text-stone-200">
                  {toolCall.toolName}
                </span>
                {summary && (
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-stone-400 dark:text-stone-500">
                    {summary}
                  </span>
                )}
                <span className="flex shrink-0 items-center gap-1">
                  <Button
                    size="small"
                    className="!h-6 !px-2 !text-[11px]"
                    icon={<CloseOutlined />}
                    onClick={() => onReject(toolCall.id)}
                  >
                    拒绝
                  </Button>
                  <Button
                    size="small"
                    type="primary"
                    className="!h-6 !px-2 !text-[11px]"
                    icon={<CheckOutlined />}
                    onClick={() => onApprove(toolCall.id)}
                  >
                    批准
                  </Button>
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

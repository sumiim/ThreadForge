import { Button } from 'antd'
import { CheckOutlined, CloseOutlined, ToolOutlined } from '@ant-design/icons'
import type { ToolCall } from '../../api/types'
import { toolStatusMeta } from './toolStatus'

interface ToolCallCardProps {
  toolCall: ToolCall
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}

// 状态语义色条（与 toolStatus.ts 的文本色对应）
const barClass: Record<string, string> = {
  pending: 'bg-amber-500',
  running: 'bg-slate-400',
  completed: 'bg-green-600',
  rejected: 'bg-red-500',
  error: 'bg-red-500',
}

// 工具调用卡：状态色条替代外框，mono 参数区，无模板化卡片感
export default function ToolCallCard({ toolCall, onApprove, onReject }: ToolCallCardProps) {
  const status = toolStatusMeta[toolCall.status]
  const needsApproval = toolCall.requiresApproval && toolCall.status === 'pending'

  return (
    <div id={`tool-call-${toolCall.id}`} className="flex gap-2.5">
      <span className={`w-[3px] shrink-0 rounded-full ${barClass[toolCall.status]}`} aria-hidden />
      <div className="min-w-0 flex-1 rounded-xl border border-stone-100 bg-stone-50 px-3.5 py-3">
        <div className="flex items-baseline gap-2">
          <ToolOutlined className="text-xs text-stone-500" />
          <span className="font-mono text-xs font-medium text-stone-700">{toolCall.toolName}</span>
          <span className={`ml-auto font-mono text-[11px] ${status.className}`}>{status.label}</span>
        </div>

        <pre className="mt-2.5 overflow-x-auto rounded-lg border border-stone-100 bg-white p-2.5 font-mono text-[11px] leading-relaxed text-stone-500">
          {JSON.stringify(toolCall.args ?? {}, null, 2)}
        </pre>

        {toolCall.result && (
          <div className="mt-2.5 text-xs leading-relaxed text-stone-500">{toolCall.result}</div>
        )}

        {needsApproval && (
          <div className="mt-3 flex items-center justify-between border-t border-stone-100 pt-2.5">
            <span className="font-mono text-[11px] uppercase tracking-wide text-amber-700">
              危险操作
            </span>
            <div className="flex gap-2">
              <Button size="small" icon={<CloseOutlined />} onClick={() => onReject(toolCall.id)}>
                拒绝
              </Button>
              <Button size="small" type="primary" icon={<CheckOutlined />} onClick={() => onApprove(toolCall.id)}>
                批准
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

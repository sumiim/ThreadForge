import { Button } from 'antd'
import { CheckOutlined, CloseOutlined } from '@ant-design/icons'
import type { ToolCall } from '../../api/types'
import { toolStatusMeta } from './toolStatus'

interface ToolListProps {
  toolCalls: ToolCall[]
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}

// DSH 风格工具列表：紧凑行（非卡片），状态色点 + mono 工具名 + 智能摘要，
// 点击展开参数/结果详情；审批按钮内联在行尾。
export default function ToolList({ toolCalls, onApprove, onReject }: ToolListProps) {
  if (toolCalls.length === 0) return null

  return (
    <div className="mt-2 space-y-1 overflow-hidden rounded-lg border border-stone-200/80 bg-stone-50/60 dark:border-stone-700/60 dark:bg-stone-800/40">
      {toolCalls.map((toolCall, index) => (
        <ToolRow
          key={toolCall.id}
          toolCall={toolCall}
          divider={index > 0}
          onApprove={onApprove}
          onReject={onReject}
        />
      ))}
    </div>
  )
}

function deriveSummary(_toolName: string, args: Record<string, unknown> | undefined): string | null {
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

const statusDot: Record<string, string> = {
  pending: 'bg-amber-500',
  running: 'bg-blue-500 animate-pulse',
  completed: 'bg-green-600',
  rejected: 'bg-red-500',
  error: 'bg-red-500',
}

function ToolRow({
  toolCall,
  divider,
  onApprove,
  onReject,
}: {
  toolCall: ToolCall
  divider: boolean
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}) {
  const status = toolStatusMeta[toolCall.status]
  const needsApproval = toolCall.requiresApproval && toolCall.status === 'pending'
  const summary = deriveSummary(toolCall.toolName, toolCall.args)
  const hasDetails =
    (toolCall.args && Object.keys(toolCall.args).length > 0) || Boolean(toolCall.result)

  return (
    <div
      id={`tool-call-${toolCall.id}`}
      className={`scroll-mt-4 px-3 py-1.5 ${divider ? 'border-t border-stone-200/70 dark:border-stone-700/50' : ''}`}
    >
      <div className="flex items-center gap-2">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusDot[toolCall.status]}`} aria-hidden />
        <span className="truncate font-mono text-xs font-medium text-stone-700 dark:text-stone-200">
          {toolCall.toolName}
        </span>
        {summary && <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-stone-400 dark:text-stone-500">{summary}</span>}
        <span className={`shrink-0 font-mono text-[11px] ${status.className}`}>{status.label}</span>
        {needsApproval && (
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
        )}
      </div>
      {hasDetails && (
        <details className="mt-0.5">
          <summary className="cursor-pointer select-none pl-3.5 font-mono text-[10px] text-stone-400 transition-colors hover:text-stone-600 dark:hover:text-stone-300">
            详情
          </summary>
          <div className="mt-1 space-y-1.5 pl-3.5">
            {toolCall.args && Object.keys(toolCall.args).length > 0 && (
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md border border-stone-200/70 bg-white p-2 font-mono text-[11px] leading-relaxed text-stone-500 dark:border-stone-700/60 dark:bg-stone-900/60 dark:text-stone-400">
                {JSON.stringify(toolCall.args, null, 2)}
              </pre>
            )}
            {toolCall.result && (
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-stone-200/70 bg-white p-2 font-mono text-[11px] leading-relaxed text-stone-600 dark:border-stone-700/60 dark:bg-stone-900/60 dark:text-stone-300">
                {toolCall.result}
              </pre>
            )}
          </div>
        </details>
      )}
    </div>
  )
}

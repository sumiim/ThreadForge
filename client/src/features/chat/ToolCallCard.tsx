import { Button } from 'antd'
import { CheckOutlined, CloseOutlined, ToolOutlined } from '@ant-design/icons'
import type { ToolCall } from '../../api/types'
import { toolStatusMeta } from './toolStatus'

interface ToolCallCardProps {
  toolCall: ToolCall
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}

// ---- 工具 variant 分类（参考 deepseek-harness tool-call-model） ----

type ToolVariant = 'search' | 'read' | 'bash' | 'write' | 'edit' | 'code' | 'others'

const TOOL_VARIANTS: Record<string, ToolVariant> = {
  bash: 'bash',
  pwsh: 'bash',
  read: 'read',
  web_fetch: 'read',
  web_search: 'search',
  grep: 'search',
  glob: 'search',
  write: 'write',
  edit: 'edit',
  run_code: 'code',
}

const VARIANT_TITLES: Record<ToolVariant, string> = {
  search: 'Search',
  read: 'Read',
  bash: 'Bash',
  write: 'Write',
  edit: 'Edit',
  code: '代码',
  others: '工具调用',
}

const SUMMARY_KEYS: Record<ToolVariant, readonly string[]> = {
  bash: ['description', 'command'],
  read: ['path', 'file_path', 'url'],
  search: ['query', 'pattern', 'url'],
  write: ['path', 'file_path'],
  edit: ['path', 'file_path'],
  code: ['description'],
  others: [],
}

function classifyTool(toolName: string): ToolVariant {
  return TOOL_VARIANTS[toolName] ?? 'others'
}

function deriveSummary(variant: ToolVariant, args: Record<string, unknown> | undefined): string | null {
  if (!args) return null
  const keys = SUMMARY_KEYS[variant]
  for (const key of keys) {
    const v = args[key]
    if (typeof v === 'string' && v !== '') return v
  }
  // fallback：取第一个非空字符串值
  for (const v of Object.values(args)) {
    if (typeof v === 'string' && v !== '') return v
  }
  return null
}

// 状态语义色条（与 toolStatus.ts 的文本色对应）
const barClass: Record<string, string> = {
  pending: 'bg-amber-500',
  running: 'bg-slate-400',
  completed: 'bg-green-600',
  rejected: 'bg-red-500',
  error: 'bg-red-500',
}

/** 工具调用卡：variant 分类 + 智能摘要，保持现有视觉风格 */
export default function ToolCallCard({ toolCall, onApprove, onReject }: ToolCallCardProps) {
  const status = toolStatusMeta[toolCall.status]
  const needsApproval = toolCall.requiresApproval && toolCall.status === 'pending'
  const hasArgs = Object.keys(toolCall.args ?? {}).length > 0

  const variant = classifyTool(toolCall.toolName)
  // 已知工具显示 variant 标题，未知工具（MCP 等）显示原始工具名
  const title = variant === 'others' ? toolCall.toolName : VARIANT_TITLES[variant]
  const summary = deriveSummary(variant, toolCall.args)

  // code variant 直接展示 code 字段，不包 JSON
  const codeBody =
    variant === 'code' &&
    toolCall.args?.code &&
    typeof toolCall.args.code === 'string'
      ? toolCall.args.code
      : null

  return (
    <div id={`tool-call-${toolCall.id}`} className="flex gap-2.5">
      <span className={`w-[3px] shrink-0 rounded-full ${barClass[toolCall.status]}`} aria-hidden />
      <div className="min-w-0 flex-1 rounded-xl border border-stone-100 bg-stone-50 px-3.5 py-3">
        <div className="flex items-baseline gap-2">
          <ToolOutlined className="text-xs text-stone-500" />
          <span className="font-mono text-xs font-medium text-stone-700">{title}</span>
          <span className={`ml-auto font-mono text-[11px] ${status.className}`}>{status.label}</span>
        </div>

        {summary && (
          <div className="mt-1.5 truncate font-mono text-[11px] text-stone-400">{summary}</div>
        )}

        {(codeBody || hasArgs || toolCall.result) ? (
          <details className="mt-2">
            <summary className="cursor-pointer select-none font-mono text-[11px] text-stone-400 transition-colors hover:text-stone-600">
              查看详情
            </summary>
            <div className="mt-1.5 space-y-2">
              {codeBody ? (
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-stone-100 bg-white p-2.5 font-mono text-[11px] leading-relaxed text-stone-500">
                  {codeBody}
                </pre>
              ) : hasArgs ? (
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-stone-100 bg-white p-2.5 font-mono text-[11px] leading-relaxed text-stone-500">
                  {JSON.stringify(toolCall.args, null, 2)}
                </pre>
              ) : null}
              {toolCall.result && (
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-stone-100 bg-white p-2.5 font-mono text-[11px] leading-relaxed text-stone-600">
                  {toolCall.result}
                </pre>
              )}
            </div>
          </details>
        ) : null}

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
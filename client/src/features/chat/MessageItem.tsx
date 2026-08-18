import { useEffect, useState } from 'react'
import { DownOutlined, RightOutlined, ToolOutlined } from '@ant-design/icons'
import Markdown from '../../components/Markdown'
import type { Message, ToolCall } from '../../api/types'
import ToolList from './ToolList'

interface MessageItemProps {
  message: Message
  onApprove: (messageId: string, toolCallId: string) => void
  onReject: (messageId: string, toolCallId: string) => void
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 格式化毫秒为 mm:ss */
function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/** 流式输出状态指示器：ping 点 + "Deep diving..." 扫光文字 + 15s 后显示时常 */
function StreamingStatus() {
  const [mountedAt] = useState(() => Date.now())
  const [elapsedMs, setElapsedMs] = useState(0)

  useEffect(() => {
    const tick = () => { setElapsedMs(Math.max(0, Date.now() - mountedAt)) }
    tick()
    const id = setInterval(tick, 1000)
    return () => { clearInterval(id) }
  }, [mountedAt])

  const showClock = elapsedMs >= 15_000

  return (
    <span className="turn-status flex items-center gap-2 text-stone-400">
      <span className="relative inline-flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
      </span>
      <span className="bg-gradient-to-r from-blue-500 via-blue-400 to-blue-500 bg-[length:200%_100%] bg-clip-text text-transparent animate-shimmer">
        Deep diving...
      </span>
      {showClock && (
        <span className="font-mono text-stone-400 tabular-nums" aria-hidden>
          {formatElapsed(elapsedMs)}
        </span>
      )}
    </span>
  )
}

/** 思考折叠区：灰色 + 等宽滚动条。无 effect：open = 手动 ?? 流式中展开。 */
function ThinkingFold({ text, streaming }: { text: string; streaming: boolean }) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const open = manualOpen ?? (streaming && text.length > 0)

  return (
    <div>
      <button
        type="button"
        onClick={() => setManualOpen((current) => !(current ?? (streaming && text.length > 0)))}
        className="flex w-full cursor-pointer select-none items-center gap-2 rounded-md px-2 py-1 text-left text-[11px] text-stone-500 transition-colors hover:bg-stone-100/80 dark:text-stone-400 dark:hover:bg-stone-700/40"
        aria-expanded={open}
      >
        <span aria-hidden>🧠</span>
        <span className="font-medium">Thinking</span>
        <span className="ml-auto font-mono text-stone-400">{text.length} chars</span>
        <span className="text-stone-400" aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="mt-1 max-h-72 overflow-y-auto rounded-md border border-stone-200/70 bg-stone-100/40 p-2.5 dark:border-stone-700/50 dark:bg-stone-800/40">
          <div className="whitespace-pre-wrap text-xs leading-relaxed text-stone-500 dark:text-stone-400">
            {text}
          </div>
        </div>
      )}
    </div>
  )
}

/** 工具二级目录：行为 → Tool → 具体工具调用。 */
function ToolFold({ toolCalls, streaming, onApprove, onReject }: {
  toolCalls: ToolCall[]
  streaming: boolean
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const open = manualOpen ?? streaming
  const runningCount = toolCalls.filter((toolCall) => toolCall.status === 'running').length
  const pendingCount = toolCalls.filter((toolCall) => toolCall.status === 'pending').length
  const statusSummary = runningCount > 0
    ? `${runningCount} 运行中`
    : pendingCount > 0
      ? `${pendingCount} 待处理`
      : `${toolCalls.length} tool${toolCalls.length > 1 ? 's' : ''}`

  return (
    <div>
      <button
        type="button"
        onClick={() => setManualOpen((current) => !(current ?? streaming))}
        className="flex w-full cursor-pointer select-none items-center gap-2 rounded-md px-2 py-1 text-left text-[11px] text-stone-500 transition-colors hover:bg-stone-100/80 dark:text-stone-400 dark:hover:bg-stone-700/40"
        aria-expanded={open}
      >
        <ToolOutlined className="text-[12px]" aria-hidden />
        <span className="font-medium">Tool</span>
        <span className="ml-auto font-mono text-stone-400 dark:text-stone-500">{statusSummary}</span>
        <span className="text-stone-400" aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="ml-3 border-l border-stone-200/80 pb-1 pl-2 pr-2 pt-1 dark:border-stone-700/60">
          <ToolList
            toolCalls={toolCalls}
            onApprove={onApprove}
            onReject={onReject}
          />
        </div>
      )}
    </div>
  )
}

/**
 * 每轮行为折叠面板（仿左侧 RunTimeline：折叠窄条 ↔ 展开面板，默认折叠）。
 * 内容按三级组织：思考 → 工具（工具内并行最小行动）→ 中途对话。
 */
function BehaviorFold({ thinking, toolCalls, streaming, onApprove, onReject }: {
  thinking?: string
  toolCalls: ToolCall[]
  streaming: boolean
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const hasThinking = Boolean(thinking)
  const hasTools = toolCalls.length > 0
  // 中途对话（commentary）不进面板——与 final 同级直接展示。
  if (!hasThinking && !hasTools) return null

  const summary = [
    hasThinking ? `${thinking!.length} chars` : null,
    hasTools ? `${toolCalls.length} tool${toolCalls.length > 1 ? 's' : ''}` : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="mb-2 max-w-full overflow-hidden rounded-lg border border-stone-200/80 bg-stone-50/60 dark:border-stone-700/50 dark:bg-stone-800/40">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-stone-600 transition-colors hover:bg-stone-100/80 dark:text-stone-300 dark:hover:bg-stone-700/40"
        aria-expanded={open}
      >
        <span className="text-stone-400" aria-hidden>{open ? <DownOutlined className="text-[9px]" /> : <RightOutlined className="text-[9px]" />}</span>
        <span className="font-medium">行为</span>
        {summary && <span className="ml-auto truncate font-mono text-stone-400 dark:text-stone-500">{summary}</span>}
      </button>
      {open && (
        <div className="space-y-1 border-t border-stone-200/70 py-1.5 dark:border-stone-700/50">
          {hasThinking && <ThinkingFold text={thinking!} streaming={streaming} />}
          {hasTools && (
            <ToolFold
              toolCalls={toolCalls}
              streaming={streaming}
              onApprove={onApprove}
              onReject={onReject}
            />
          )}
        </div>
      )}
    </div>
  )
}

// 无头像设计：靠对齐与底色区分角色，时间戳为 mono 元信息
export default function MessageItem({ message, onApprove, onReject }: MessageItemProps) {
  if (message.role === 'user') {
    return (
      <div id={`message-${message.id}`} data-message-created-at={message.createdAt} className="message-enter flex justify-end">
        <div className="max-w-[75%]">
          <div className="rounded-2xl rounded-tr-sm bg-blue-50 px-4 py-2.5 text-sm leading-relaxed text-stone-800 dark:bg-blue-900/40 dark:text-stone-100">
            {message.content}
          </div>
          <div className="mt-1 text-right font-mono text-[11px] text-stone-500">
            {formatTime(message.createdAt)}
          </div>
        </div>
      </div>
    )
  }

  const streaming = message.status === 'streaming'
  const blocks = message.blocks

  return (
    <div
      id={`message-${message.id}`}
      data-message-created-at={message.createdAt}
      className="message-enter flex justify-start"
    >
      <div className="min-w-0 max-w-[90%]">
        {blocks && blocks.length > 0 ? (
          // 交替块：commentary 与行为按事件到达顺序出现，而非行为全堆顶部。
          blocks.map((block, index) =>
            block.kind === 'commentary' ? (
              <div
                key={index}
                className="whitespace-pre-wrap py-1 text-sm italic text-stone-500 dark:text-stone-400"
              >
                {block.text}
              </div>
            ) : (
              <BehaviorFold
                key={index}
                thinking={block.thinking}
                toolCalls={block.toolCalls ?? []}
                streaming={streaming}
                onApprove={(toolCallId) => onApprove(message.id, toolCallId)}
                onReject={(toolCallId) => onReject(message.id, toolCallId)}
              />
            ),
          )
        ) : (
          // 历史消息（无 blocks）回退：行为 + 顶层 commentary。
          <>
            <BehaviorFold
              thinking={message.thinking}
              toolCalls={message.toolCalls ?? []}
              streaming={streaming}
              onApprove={(toolCallId) => onApprove(message.id, toolCallId)}
              onReject={(toolCallId) => onReject(message.id, toolCallId)}
            />
            {message.commentary && (
              <div className="whitespace-pre-wrap py-1 text-sm italic text-stone-500 dark:text-stone-400">
                {message.commentary}
              </div>
            )}
          </>
        )}
        {message.content && (
          <div className="py-1">
            <Markdown content={message.content} />
          </div>
        )}
        <div className="mt-1 font-mono text-[11px] text-stone-500 dark:text-stone-400">
          {streaming ? (
            <StreamingStatus />
          ) : (
            formatTime(message.createdAt)
          )}
        </div>
      </div>
    </div>
  )
}

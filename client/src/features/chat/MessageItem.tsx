import { useEffect, useState } from 'react'
import Markdown from '../../components/Markdown'
import type { Message } from '../../api/types'
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

/** 思考过程折叠区：灰色 + 等宽滚动条，流式中默认展开、结束后折叠。
 *  无 effect：open = 用户手动状态 ?? 流式中自动展开。 */
function ThinkingFold({ text, streaming }: { text: string; streaming: boolean }) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const open = manualOpen ?? (streaming && text.length > 0)

  return (
    <div className="mb-2 max-w-full">
      <button
        type="button"
        onClick={() => setManualOpen((current) => !(current ?? (streaming && text.length > 0)))}
        className="flex w-full cursor-pointer select-none items-center gap-2 rounded-lg border border-stone-200/80 bg-stone-100/80 px-3 py-1.5 text-left text-[11px] text-stone-500 transition-colors hover:bg-stone-200/70 dark:border-stone-700/60 dark:bg-stone-800/70 dark:text-stone-400 dark:hover:bg-stone-700/60"
        aria-expanded={open}
      >
        <span className="text-stone-400" aria-hidden>🧠</span>
        <span className="font-medium">思考过程</span>
        <span className="ml-auto font-mono text-stone-400">{text.length} 字</span>
        <span className="text-stone-400" aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="mt-1 max-h-72 overflow-y-auto rounded-lg border border-stone-200/80 bg-stone-100/50 p-3 dark:border-stone-700/60 dark:bg-stone-800/40">
          <div className="whitespace-pre-wrap text-xs leading-relaxed text-stone-500 dark:text-stone-400">
            {text}
          </div>
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

  return (
    <div
      id={`message-${message.id}`}
      data-message-created-at={message.createdAt}
      className="message-enter flex justify-start"
    >
      <div className="min-w-0 max-w-[90%]">
        {message.activity && message.activity.length > 0 ? (
          <details className="mb-2 max-w-full rounded-lg border border-stone-100 bg-stone-50/70 px-3 py-2 text-xs text-stone-500 dark:border-stone-700/50 dark:bg-stone-800/50 dark:text-stone-400" open={message.status === 'streaming'}>
            <summary className="cursor-pointer select-none text-[11px] text-stone-500 dark:text-stone-400">
              已记录 {message.activity.length} 条运行事件
            </summary>
            <div className="mt-2 space-y-1.5 border-l border-stone-200 pl-2 dark:border-stone-600">
              {message.activity.map((activity) => (
                <div key={activity.id} id={`run-event-${activity.id}`} className="scroll-mt-4">
                  <div className="font-medium text-stone-600 dark:text-stone-300">{activity.label}</div>
                  {activity.detail ? <div className="mt-0.5 whitespace-pre-wrap text-stone-400 dark:text-stone-500">{activity.detail}</div> : null}
                </div>
              ))}
            </div>
          </details>
        ) : null}
        {message.thinking ? (
          <ThinkingFold text={message.thinking} streaming={message.status === 'streaming'} />
        ) : null}
        {message.commentary && (
          <div className="py-1 whitespace-pre-wrap text-sm italic text-stone-500 dark:text-stone-400">
            {message.commentary}
          </div>
        )}
        {(message.toolCalls?.length ?? 0) > 0 && (
          <ToolList
            toolCalls={message.toolCalls ?? []}
            onApprove={(toolCallId) => onApprove(message.id, toolCallId)}
            onReject={(toolCallId) => onReject(message.id, toolCallId)}
          />
        )}
        {message.content && (
          <div className="py-1">
            <Markdown content={message.content} />
          </div>
        )}
        <div className="mt-1 font-mono text-[11px] text-stone-500 dark:text-stone-400">
          {message.status === 'streaming' ? (
            <StreamingStatus />
          ) : (
            formatTime(message.createdAt)
          )}
        </div>
      </div>
    </div>
  )
}

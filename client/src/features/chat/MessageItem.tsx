import { useEffect, useRef, useState } from 'react'
import Markdown from '../../components/Markdown'
import type { Message } from '../../api/types'
import ToolCallCard from './ToolCallCard'

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

// 无头像设计：靠对齐与底色区分角色，时间戳为 mono 元信息
export default function MessageItem({ message, onApprove, onReject }: MessageItemProps) {
  if (message.role === 'user') {
    return (
      <div id={`message-${message.id}`} data-message-created-at={message.createdAt} className="message-enter flex justify-end">
        <div className="max-w-[75%]">
          <div className="rounded-2xl rounded-tr-sm bg-blue-50 px-4 py-2.5 text-sm leading-relaxed text-stone-800">
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
      <div className="min-w-0 max-w-[85%]">
        {message.activity && message.activity.length > 0 ? (
          <details className="mb-2 max-w-full rounded-lg border border-stone-100 bg-stone-50/70 px-3 py-2 text-xs text-stone-500" open={message.status === 'streaming'}>
            <summary className="cursor-pointer select-none text-[11px] text-stone-500">
              已记录 {message.activity.length} 条运行事件
            </summary>
            <div className="mt-2 space-y-1.5 border-l border-stone-200 pl-2">
              {message.activity.map((activity) => (
                <div key={activity.id} id={`run-event-${activity.id}`} className="scroll-mt-4">
                  <div className="font-medium text-stone-600">{activity.label}</div>
                  {activity.detail ? <div className="mt-0.5 whitespace-pre-wrap text-stone-400">{activity.detail}</div> : null}
                </div>
              ))}
            </div>
          </details>
        ) : null}
        {message.content && (
          <div className="py-1">
            <Markdown content={message.content} />
          </div>
        )}
        {message.toolCalls?.map((toolCall) => (
          <div key={toolCall.id} className="mt-3 max-w-full">
            <ToolCallCard
              toolCall={toolCall}
              onApprove={(toolCallId) => onApprove(message.id, toolCallId)}
              onReject={(toolCallId) => onReject(message.id, toolCallId)}
            />
          </div>
        ))}
        <div className="mt-1 font-mono text-[11px] text-stone-500">
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

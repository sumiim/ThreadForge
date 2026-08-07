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
              已完成 {message.activity.length} 个过程步骤
            </summary>
            <div className="mt-2 space-y-1.5 border-l border-stone-200 pl-2">
              {message.activity.map((activity) => (
                <div key={activity.id}>
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
            <span className="flex items-center gap-1.5 text-stone-500">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-600" />
              agent working
            </span>
          ) : (
            formatTime(message.createdAt)
          )}
        </div>
      </div>
    </div>
  )
}

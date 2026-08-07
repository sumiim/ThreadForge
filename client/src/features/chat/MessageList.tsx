import { useEffect, useRef } from 'react'
import type { Message } from '../../api/types'
import MessageItem from './MessageItem'

interface MessageListProps {
  messages: Message[]
  onApprove: (messageId: string, toolCallId: string) => void
  onReject: (messageId: string, toolCallId: string) => void
}

export default function MessageList({ messages, onApprove, onReject }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // 新消息或运行状态变化时滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <div id="run-scroll-container" className="min-h-0 flex-1 overflow-y-auto px-6 py-6 lg:px-10">
      <div className="mx-auto max-w-4xl space-y-6">
        {messages.map((message) => (
          <MessageItem
            key={message.id}
            message={message}
            onApprove={onApprove}
            onReject={onReject}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Message } from '../../api/types'
import MessageItem from './MessageItem'

interface MessageListProps {
  messages: Message[]
  onApprove: (messageId: string, toolCallId: string) => void
  onReject: (messageId: string, toolCallId: string) => void
}

const FOLLOW_THRESHOLD = 48 // px from bottom to consider "at bottom"

export default function MessageList({ messages, onApprove, onReject }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = useState(true)
  const userScrolledRef = useRef(false)

  // 滚动侦听
  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    const isAtBottom = distance <= FOLLOW_THRESHOLD
    if (isAtBottom) userScrolledRef.current = false
    setAtBottom(isAtBottom)
  }, [])

  // 到底部
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    userScrolledRef.current = false
    setAtBottom(true)
  }, [])

  // 自动跟随：仅当用户已在底部时
  useEffect(() => {
    if (atBottom && messages.length > 0) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, atBottom])

  return (
    <div
      id="run-scroll-container"
      ref={scrollRef}
      onScroll={handleScroll}
      className="relative min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 sm:py-6 lg:px-10"
    >
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

      {/* 回到底部浮动按钮 */}
      {!atBottom && (
        <div className="sticky bottom-4 z-10 flex justify-end pointer-events-none">
          <button
            type="button"
            onClick={scrollToBottom}
            className="pointer-events-auto flex h-9 w-9 items-center justify-center rounded-full border border-stone-200 bg-white text-stone-600 shadow-md transition-all hover:bg-stone-50 hover:text-stone-800 active:scale-95 dark:border-stone-600 dark:bg-stone-800 dark:text-stone-400 dark:hover:bg-stone-700 dark:hover:text-stone-200"
            aria-label="回到底部"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
              <path
                d="M8 3v10M4 9l4 4 4-4"
                stroke="currentColor" strokeWidth="1.5"
                strokeLinecap="round" strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      )}
    </div>
  )
}

import { useEffect, useState } from 'react'
import { DownOutlined, RightOutlined, ToolOutlined } from '@ant-design/icons'
import Markdown from '../../components/Markdown'
import type { Message, MessageBlock, ReviewBattleEntry, ToolCall } from '../../api/types'
import ToolList from './ToolList'

interface MessageItemProps {
  message: Message
  onApprove: (messageId: string, toolCallId: string) => void
  onReject: (messageId: string, toolCallId: string) => void
}

/** 按 turn 聚合的块：一个 turn 一个收纳抽屉（思考/工具/review/中途话）。 */
type TurnGroup = {
  turn?: number
  thinking?: string
  toolCalls?: ToolCall[]
  reviewEntries?: ReviewBattleEntry[]
  commentary?: string
}

function extractEntries(block: { entries?: ReviewBattleEntry[] }): ReviewBattleEntry[] {
  return block.entries ?? []
}

/** 把交替 blocks 按 turn 分组：同 turn 的 behavior + review + commentary 合并成一个
 *  TurnGroup（commentary 归入所属 turn,在思考/工具/review 之下、下一 turn 之上）;
 *  无 turn 标注的 comment 单独成条目（保持顺序）。 */
function groupBlocksByTurn(blocks: MessageBlock[]): Array<TurnGroup | { commentary: string }> {
  const output: Array<TurnGroup | { commentary: string }> = []
  for (const block of blocks) {
    if (block.kind === 'commentary') {
      const turn = block.turn
      const last = output[output.length - 1]
      const sameTurn = turn != null && last && 'turn' in last && last.turn === turn ? last : undefined
      if (sameTurn) {
        sameTurn.commentary = (sameTurn.commentary ?? '') + (sameTurn.commentary ? '\n' : '') + block.text
      } else {
        output.push({ commentary: block.text })
      }
      continue
    }
    const turn = block.turn
    const last = output[output.length - 1]
    const sameTurn = last && 'turn' in last && last.turn === turn ? last : undefined
    if (block.kind === 'review') {
      const entries = extractEntries(block)
      if (sameTurn) {
        sameTurn.reviewEntries = [...(sameTurn.reviewEntries ?? []), ...entries]
      } else {
        output.push({ turn, reviewEntries: [...entries] })
      }
      continue
    }
    // behavior
    const tools = block.toolCalls ?? []
    const thinking = block.thinking
    if (sameTurn) {
      if (thinking) sameTurn.thinking = (sameTurn.thinking ?? '') + (sameTurn.thinking ? '\n' : '') + thinking
      if (tools.length) sameTurn.toolCalls = [...(sameTurn.toolCalls ?? []), ...tools]
    } else {
      output.push({ turn, thinking, toolCalls: tools.length ? tools : undefined })
    }
  }
  return output
}

/** Turn N 收纳抽屉：行为 → 思考 / 工具 / 中途话 / 审查(review)。默认折叠。 */
function TurnFold({ turn, thinking, toolCalls, reviewEntries, commentary, streaming, onApprove, onReject }: {
  turn?: number
  thinking?: string
  toolCalls?: ToolCall[]
  reviewEntries?: ReviewBattleEntry[]
  commentary?: string
  streaming: boolean
  onApprove: (toolCallId: string) => void
  onReject: (toolCallId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const hasThinking = Boolean(thinking)
  const hasTools = (toolCalls ?? []).length > 0
  const hasReview = (reviewEntries ?? []).length > 0
  const hasCommentary = Boolean(commentary)
  const hasContent = hasThinking || hasTools || hasReview || hasCommentary
  // 默认折叠（§7.8.9 修正 2026-08-19：此前流式时自动展开,用户要求改为折叠）。
  const expanded = open
  if (!hasContent) return null
  const summary = [
    hasThinking ? `${thinking!.length} chars` : null,
    hasTools ? `${toolCalls!.length} tool${toolCalls!.length > 1 ? 's' : ''}` : null,
    hasReview ? `${reviewEntries!.length} review` : null,
  ].filter(Boolean).join(' · ')
  const title = turn != null ? `Turn ${turn}` : '行为'
  return (
    <div className="mb-2 max-w-full overflow-hidden rounded-lg border border-stone-200/80 bg-stone-50/60 dark:border-stone-700/50 dark:bg-stone-800/40">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-stone-600 transition-colors hover:bg-stone-100/80 dark:text-stone-300 dark:hover:bg-stone-700/40"
        aria-expanded={expanded}
      >
        <span className="text-stone-400" aria-hidden>{expanded ? <DownOutlined className="text-[9px]" /> : <RightOutlined className="text-[9px]" />}</span>
        <span className="font-medium">{title}</span>
        {summary && <span className="ml-auto truncate font-mono text-stone-400 dark:text-stone-500">{summary}</span>}
      </button>
      {expanded && (
        <div className="space-y-1 border-t border-stone-200/70 py-1.5 dark:border-stone-700/50">
          {hasThinking && <ThinkingFold text={thinking!} streaming={streaming} />}
          {hasTools && (
            <ToolFold
              toolCalls={toolCalls!}
              streaming={streaming}
              onApprove={onApprove}
              onReject={onReject}
            />
          )}
          {hasCommentary && (
            <div className="whitespace-pre-wrap px-1 py-0.5 text-sm leading-relaxed text-stone-800 dark:text-stone-100">
              {commentary!}
            </div>
          )}
          {hasReview && <ReviewBattle entries={reviewEntries!} />}
        </div>
      )}
    </div>
  )
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
function ThinkingFold({ text, streaming, label = 'Thinking' }: { text: string; streaming: boolean; label?: string }) {
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
        <span className="font-medium">{label}</span>
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

/** §7.8.9 决策（2026-08-18）：审查对抗块——双向对抗协议的前端展示。
 * 渲染在行为块下方（blocks 第三分支）：谁发的、各什么理由、最终结果。 */
function ReviewBattle({ entries, thinking }: { entries: ReviewBattleEntry[]; thinking?: string }) {
  const [open, setOpen] = useState(false)
  const [thinkOpen, setThinkOpen] = useState(false)
  const latest = entries[entries.length - 1]
  const resultLabel = latest?.result === 'passed' ? '通过' : latest?.result === 'rejected' ? '驳回' : latest?.result === 'continue' ? '继续' : ''
  return (
    <div className="mb-2 max-w-full overflow-hidden rounded-lg border border-orange-200/80 bg-orange-50/50 dark:border-orange-700/40 dark:bg-orange-900/20">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-orange-700 transition-colors hover:bg-orange-100/60 dark:text-orange-300 dark:hover:bg-orange-800/30"
        aria-expanded={open}
      >
        <span aria-hidden>{open ? '▾' : '▸'}</span>
        <span className="font-medium">审查对抗</span>
        <span className="ml-auto truncate font-mono text-orange-500 dark:text-orange-400">
          {entries.length} 回合{resultLabel ? ` · ${resultLabel}` : ''}
        </span>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-orange-200/70 py-1.5 dark:border-orange-700/40">
          {thinking ? (
            <div className="px-2">
              <button
                type="button"
                onClick={() => setThinkOpen((value) => !value)}
                className="flex w-full cursor-pointer select-none items-center gap-2 rounded px-1 py-0.5 text-left text-[10px] text-orange-600 transition-colors hover:bg-orange-100/60 dark:text-orange-400 dark:hover:bg-orange-800/30"
                aria-expanded={thinkOpen}
              >
                <span aria-hidden>🧠</span>
                <span className="font-medium">Review thinking</span>
                <span className="ml-auto font-mono text-orange-400">{thinking.length} chars</span>
                <span aria-hidden>{thinkOpen ? '▾' : '▸'}</span>
              </button>
              {thinkOpen && (
                <div className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded bg-orange-100/40 p-2 text-[10px] leading-relaxed text-stone-600 dark:bg-orange-900/20 dark:text-stone-300">
                  {thinking}
                </div>
              )}
            </div>
          ) : null}
          {entries.map((entry, index) => (
            <div key={index} className="px-2 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300">
              <div className="flex items-center gap-1.5">
                <span className="font-medium">{entry.side === 'review' ? '🧐 Review' : '🤖 主循环'}</span>
                {entry.verdict ? (
                  <span className={
                    entry.verdict === 'finalize'
                      ? 'rounded bg-emerald-100 px-1 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
                      : entry.verdict === 'redirect'
                        ? 'rounded bg-orange-100 px-1 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300'
                        : 'rounded bg-stone-100 px-1 text-stone-600 dark:bg-stone-700 dark:text-stone-300'
                  }>
                    {entry.verdict}
                  </span>
                ) : null}
                {entry.action ? <span className="font-mono text-stone-400">{entry.action}</span> : null}
              </div>
              {entry.obstacles && entry.obstacles.length > 0 ? (
                <div className="mt-0.5 text-stone-500">障碍：{entry.obstacles.join('、')}</div>
              ) : null}
              {entry.feedback ? <div className="mt-0.5 whitespace-pre-wrap text-stone-500">{entry.feedback}</div> : null}
              {entry.reason ? <div className="mt-0.5 font-mono text-[10px] text-stone-400">{entry.reason}</div> : null}
            </div>
          ))}
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
        {message.planningThinking ? (
          <div className="mb-2">
            <ThinkingFold text={message.planningThinking} streaming={streaming} label="Planning thinking" />
          </div>
        ) : null}
        {blocks && blocks.length > 0 ? (
          // 每个 turn 一个收纳抽屉（思想/工具/review），commentary 保持顺序独立。
          groupBlocksByTurn(blocks).map((group, index) => (
            'commentary' in group ? (
              <div
                key={index}
                className="whitespace-pre-wrap py-1 text-sm leading-relaxed text-stone-800 dark:text-stone-100"
              >
                {group.commentary}
              </div>
            ) : (
              <TurnFold
                key={index}
                turn={group.turn}
                thinking={group.thinking}
                toolCalls={group.toolCalls ?? []}
                reviewEntries={group.reviewEntries ?? []}
                commentary={group.commentary}
                streaming={streaming}
                onApprove={(toolCallId) => onApprove(message.id, toolCallId)}
                onReject={(toolCallId) => onReject(message.id, toolCallId)}
              />
            )
          ))
        ) : (
          // 历史消息（无 blocks）回退：行为 + 审查对抗 + 顶层 commentary。
          <>
            {message.reviewEntries && message.reviewEntries.length > 0 ? (
              <ReviewBattle entries={message.reviewEntries} />
            ) : null}
            <TurnFold
              thinking={message.thinking}
              toolCalls={message.toolCalls ?? []}
              reviewEntries={message.reviewEntries ?? []}
              streaming={streaming}
              onApprove={(toolCallId) => onApprove(message.id, toolCallId)}
              onReject={(toolCallId) => onReject(message.id, toolCallId)}
            />
            {message.commentary && (
              <div className="whitespace-pre-wrap py-1 text-sm leading-relaxed text-stone-800 dark:text-stone-100">
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

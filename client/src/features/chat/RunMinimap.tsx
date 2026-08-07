import { useEffect, useMemo, useState } from 'react'
import type { RunIndexItem, SessionRun } from '../../api/types'

interface RunMinimapProps {
  runs: SessionRun[]
  activeRunId?: string
  onSelectRun?: (runId: string) => void
}

// Codex 风格的淡色运行索引：每一条线对应一个可定位的阶段事件。
export default function RunMinimap({ runs, activeRunId, onSelectRun }: RunMinimapProps) {
  const [activeKey, setActiveKey] = useState('')
  const items = useMemo(
    () => runs.flatMap((run) => run.items.map((item) => ({ run, item }))),
    [runs],
  )

  useEffect(() => {
    const container = document.getElementById('run-scroll-container')
    if (!container || items.length === 0) return
    const update = () => {
      const maximum = Math.max(1, container.scrollHeight - container.clientHeight)
      const ratio = Math.min(1, Math.max(0, container.scrollTop / maximum))
      const index = Math.round(ratio * Math.max(0, items.length - 1))
      setActiveKey(items[index]?.item.event_id ?? '')
    }
    update()
    container.addEventListener('scroll', update, { passive: true })
    return () => container.removeEventListener('scroll', update)
  }, [items])

  if (runs.length === 0) return null

  const jump = (runId: string, item: RunIndexItem) => {
    setActiveKey(item.event_id)
    onSelectRun?.(runId)
    const target = item.tool_call_id
      ? document.getElementById(`tool-call-${item.tool_call_id}`)
      : document.getElementById(`run-event-${item.event_id}`)
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const messages = Array.from(container.querySelectorAll<HTMLElement>('[data-message-created-at]'))
    if (messages.length > 0) {
      const eventTime = new Date(item.timestamp).getTime()
      const nearest = messages.reduce((best, message) => {
        const distance = Math.abs(new Date(message.dataset.messageCreatedAt ?? '').getTime() - eventTime)
        return distance < best.distance ? { message, distance } : best
      }, { message: messages[0], distance: Number.POSITIVE_INFINITY })
      nearest.message.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    container.scrollTo({ top: 0, behavior: 'smooth' })
  }

  return (
    <nav
      aria-label="运行快速索引"
      className="flex w-4 shrink-0 flex-col items-center overflow-hidden border-r border-stone-100 bg-white/70 py-3"
    >
      <div className="flex min-h-0 flex-1 flex-col items-center gap-1 overflow-y-auto py-1">
        {runs.map((run) => (
          <div key={run.runId} className="flex w-full flex-col items-center gap-1 py-0.5" title={`Run ${run.runId}`}>
            {run.items.map((item) => {
              const active = activeKey
                ? activeKey === item.event_id
                : activeRunId === run.runId && item === run.items.at(-1)
              const failed = item.type === 'tool.failed' || ['failed', 'blocked', 'interrupted', 'needs_fix'].includes(item.status ?? '')
              return (
                <button
                  key={item.event_id}
                  type="button"
                  title={`${item.label}${item.tool_name ? ` · ${item.tool_name}` : ''}`}
                  aria-label={item.label}
                  onClick={() => jump(run.runId, item)}
                  className={`h-1 w-2.5 shrink-0 rounded-full transition-all hover:w-3.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 ${
                    failed ? 'bg-red-300 hover:bg-red-500' : active ? 'bg-blue-500' : 'bg-stone-300 hover:bg-blue-400'
                  }`}
                />
              )
            })}
          </div>
        ))}
      </div>
    </nav>
  )
}

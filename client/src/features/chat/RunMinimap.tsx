import { useEffect, useState } from 'react'
import type { RunIndexItem } from '../../api/types'

interface RunMinimapProps {
  items: RunIndexItem[]
}

export default function RunMinimap({ items }: RunMinimapProps) {
  const [activeIndex, setActiveIndex] = useState(0)

  useEffect(() => {
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const update = () => {
      const maximum = Math.max(1, container.scrollHeight - container.clientHeight)
      const ratio = Math.min(1, Math.max(0, container.scrollTop / maximum))
      setActiveIndex(Math.max(0, Math.min(items.length - 1, Math.round(ratio * (items.length - 1)))))
    }
    update()
    container.addEventListener('scroll', update, { passive: true })
    return () => container.removeEventListener('scroll', update)
  }, [items.length])

  if (items.length === 0) return null

  const jump = (item: RunIndexItem, index: number) => {
    setActiveIndex(index)
    const toolCallId = item.tool_call_id
    const target = toolCallId ? document.getElementById(`tool-call-${toolCallId}`) : null
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    const container = document.getElementById('run-scroll-container')
    container?.scrollTo({
      top: item.type === 'plan.created' ? 0 : container.scrollHeight,
      behavior: 'smooth',
    })
  }

  return (
    <nav
      aria-label="本次运行索引"
      className="flex w-7 shrink-0 flex-col items-center gap-1 overflow-y-auto border-l border-stone-100 bg-white py-3"
    >
      {items.map((item, index) => {
        const failed = item.type === 'tool.failed' || item.status === 'needs_fix'
        const active = index === activeIndex
        return (
          <button
            key={item.event_id}
            type="button"
            title={`${item.label}${item.tool_name ? ` · ${item.tool_name}` : ''}`}
            aria-label={item.label}
            onClick={() => jump(item, index)}
            aria-current={active ? 'step' : undefined}
            className={`h-1.5 w-4 shrink-0 rounded-sm transition-transform hover:scale-y-150 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 ${
              failed
                ? `border border-red-600 ${active ? 'bg-red-100 ring-1 ring-red-300' : 'bg-white'}`
                : active
                  ? 'bg-blue-600'
                  : 'bg-stone-400 hover:bg-blue-600'
            }`}
          />
        )
      })}
    </nav>
  )
}

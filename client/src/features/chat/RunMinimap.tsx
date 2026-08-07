import { HistoryOutlined } from '@ant-design/icons'
import { Dropdown } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import type { RunIndexItem, SessionRun } from '../../api/types'

interface RunMinimapProps {
  runs: SessionRun[]
  activeRunId?: string
}

export default function RunMinimap({ runs, activeRunId }: RunMinimapProps) {
  const [selection, setSelection] = useState({
    runId: activeRunId ?? '',
    activeRunId: activeRunId ?? '',
  })
  const [activeIndex, setActiveIndex] = useState(0)

  const selectedRunId =
    activeRunId && selection.activeRunId !== activeRunId ? activeRunId : selection.runId

  const selectedRun = useMemo(
    () => runs.find((run) => run.runId === selectedRunId) ?? runs.at(-1),
    [runs, selectedRunId],
  )
  const items = selectedRun?.items ?? []

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
  }, [items.length, selectedRun?.runId])

  if (runs.length === 0) return null

  const jump = (item: RunIndexItem, index: number) => {
    setActiveIndex(index)
    const target = item.tool_call_id
      ? document.getElementById(`tool-call-${item.tool_call_id}`)
      : document.getElementById(`run-event-${item.event_id}`)
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    const container = document.getElementById('run-scroll-container')
    if (!container) return
    const ratio = items.length <= 1 ? 0 : index / (items.length - 1)
    container.scrollTo({
      top: ratio * Math.max(0, container.scrollHeight - container.clientHeight),
      behavior: 'smooth',
    })
  }

  const runNumber = Math.max(1, runs.findIndex((run) => run.runId === selectedRun?.runId) + 1)
  const menuItems = runs.map((run, index) => ({
    key: run.runId,
    label: `Run ${index + 1} · ${new Date(run.startedAt).toLocaleString()} · ${run.status}`,
  }))

  return (
    <nav
      aria-label="运行快速索引"
      className="flex w-9 shrink-0 flex-col items-center overflow-hidden border-l border-stone-100 bg-white py-2"
    >
      <Dropdown
        trigger={['click']}
        menu={{
          items: menuItems,
          selectable: true,
          selectedKeys: selectedRun ? [selectedRun.runId] : [],
          onClick: ({ key }) => {
            setSelection({ runId: key, activeRunId: activeRunId ?? '' })
            setActiveIndex(0)
          },
        }}
      >
        <button
          type="button"
          title="切换运行"
          aria-label={`切换运行，当前 Run ${runNumber}`}
          className="mb-2 flex h-7 w-7 flex-col items-center justify-center rounded-lg text-stone-500 hover:bg-stone-100 hover:text-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600"
        >
          <HistoryOutlined className="text-xs" />
          <span className="text-[9px] leading-none">R{runNumber}</span>
        </button>
      </Dropdown>
      <div className="flex min-h-0 flex-1 flex-col items-center gap-1 overflow-y-auto py-1">
        {items.map((item, index) => {
          const failed = item.type === 'tool.failed' || ['failed', 'blocked', 'interrupted', 'needs_fix'].includes(item.status ?? '')
          const active = index === activeIndex
          const terminal = item.type.startsWith('task.')
          return (
            <button
              key={item.event_id}
              type="button"
              title={`${item.label}${item.tool_name ? ` · ${item.tool_name}` : ''}`}
              aria-label={item.label}
              onClick={() => jump(item, index)}
              aria-current={active ? 'step' : undefined}
              className={`${terminal ? 'h-2.5 w-2.5 rounded-full' : 'h-1.5 w-4 rounded-sm'} shrink-0 transition-transform hover:scale-y-150 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 ${
                failed
                  ? `border border-red-600 ${active ? 'bg-red-100 ring-1 ring-red-300' : 'bg-white'}`
                  : active
                    ? 'bg-blue-600'
                    : 'bg-stone-400 hover:bg-blue-600'
              }`}
            />
          )
        })}
      </div>
    </nav>
  )
}

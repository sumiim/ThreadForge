import { useMemo, useState, type MouseEvent } from 'react'
import { Button, Empty, Input, Select, Tag } from 'antd'
import { CloseOutlined, DownloadOutlined, SearchOutlined } from '@ant-design/icons'
import type { RunIndexItem, Session, SessionRun } from '../../api/types'
import { eventLabel, eventTimeOf, isFailed, LANE_ORDER, LANE_TITLE, laneOf, sortTimelineItems, timelineBounds, timelineRange, type Lane } from './traceModel'

interface RunTracePanelProps {
  open: boolean
  session: Session | null
  activeRunId?: string
  provider?: string
  onClose: () => void
}

interface TraceRow extends RunIndexItem {
  lane: Lane
  failed: boolean
}

interface RangeSelection {
  start: number
  end: number
}

const EMPTY_RUNS: SessionRun[] = []

const LANE_COLOR: Record<Lane, string> = {
  talk: 'blue',
  plan: 'purple',
  model: 'cyan',
  execute: 'geekblue',
  approval: 'orange',
  review: 'magenta',
  final: 'green',
  system: 'default',
}

function formatDurationMs(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '—'
  if (milliseconds < 1_000) return `${Math.round(milliseconds)} ms`
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`
  return `${Math.floor(milliseconds / 60_000)}m ${Math.round((milliseconds % 60_000) / 1_000)}s`
}

function durationOf(startedAt?: string, endedAt?: string): number {
  if (!startedAt || !endedAt) return Number.NaN
  return new Date(endedAt).getTime() - new Date(startedAt).getTime()
}

function downloadJson(run: SessionRun | undefined) {
  if (!run) return
  const payload = {
    schema_version: 1,
    run_id: run.runId,
    task_id: run.taskId,
    status: run.status,
    model_id: run.modelId,
    reasoning_effort: run.reasoningEffort,
    input: run.input ?? '',
    events: run.items,
    exported_at: new Date().toISOString(),
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `trace-${run.runId}.json`
  anchor.click()
  URL.revokeObjectURL(url)
}

function AuditTimeline({ rows, selectedEventId, onSelect, onRangeChange }: {
  rows: TraceRow[]
  selectedEventId?: string
  onSelect: (eventId: string) => void
  onRangeChange: (eventIds: string[] | null) => void
}) {
  const [dragging, setDragging] = useState(false)
  const [selection, setSelection] = useState<RangeSelection | null>(null)
  const bounds = timelineBounds(rows)
  const visibleLanes = LANE_ORDER.filter((lane) => rows.some((row) => row.lane === lane))

  const ratioAt = (clientY: number, target: HTMLDivElement): number => {
    const rect = target.getBoundingClientRect()
    return Math.min(1, Math.max(0, (clientY - rect.top) / Math.max(1, rect.height)))
  }
  const beginSelection = (event: MouseEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button')) return
    const ratio = ratioAt(event.clientY, event.currentTarget)
    setDragging(true)
    setSelection({ start: ratio, end: ratio })
    onRangeChange(null)
  }
  const moveSelection = (event: MouseEvent<HTMLDivElement>) => {
    if (!dragging || !selection) return
    const next = { ...selection, end: ratioAt(event.clientY, event.currentTarget) }
    setSelection(next)
    const rangeStart = Math.min(next.start, next.end)
    const rangeEnd = Math.max(next.start, next.end)
    onRangeChange(rangeEnd - rangeStart > 0.005
      ? rows.filter((row) => {
          const ratio = (eventTimeOf(row) - bounds.start) / bounds.span
          return ratio >= rangeStart && ratio <= rangeEnd
        }).map((row) => row.event_id)
      : null)
  }
  const stopSelection = () => setDragging(false)
  const normalizedRange = selection
    ? { start: Math.min(selection.start, selection.end), end: Math.max(selection.start, selection.end) }
    : null
  const selectedRows = normalizedRange && normalizedRange.end - normalizedRange.start > 0.005
    ? rows.filter((row) => {
        const ratio = (eventTimeOf(row) - bounds.start) / bounds.span
        return ratio >= normalizedRange.start && ratio <= normalizedRange.end
      })
    : rows

  return (
    <section className="flex min-h-0 min-w-0 flex-col border-r border-stone-200 bg-white">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-stone-100 px-3 text-[11px] text-stone-500">
        <span className="font-medium text-stone-700">时间</span>
        {normalizedRange && normalizedRange.end - normalizedRange.start > 0.005 ? (
          <button type="button" className="ml-auto truncate text-blue-600 hover:text-blue-800" onClick={() => { setSelection(null); onRangeChange(null) }}>
            清除 · {selectedRows.length}
          </button>
        ) : null}
      </div>
      <div
        className="relative min-h-[320px] min-w-0 flex-1 cursor-crosshair select-none overflow-hidden px-2 py-3"
        onMouseDown={beginSelection}
        onMouseMove={moveSelection}
        onMouseUp={stopSelection}
        onMouseLeave={stopSelection}
      >
        <div className="absolute inset-x-2 bottom-3 top-3 rounded-md bg-stone-50/70">
          {visibleLanes.map((lane, laneIndex) => (
            <div
              key={lane}
              className="absolute bottom-0 top-0 w-px bg-stone-200/70"
              style={{ left: `${((laneIndex + 1) / (visibleLanes.length + 1)) * 100}%` }}
              title={LANE_TITLE[lane]}
            />
          ))}
          {normalizedRange && normalizedRange.end - normalizedRange.start > 0.005 ? (
            <div
              className="pointer-events-none absolute inset-x-0 z-10 border-y-2 border-blue-500 bg-blue-100/45"
              style={{ top: `${normalizedRange.start * 100}%`, height: `${(normalizedRange.end - normalizedRange.start) * 100}%` }}
            />
          ) : null}
          {rows.map((row, index) => {
            const range = timelineRange(rows, index, bounds)
            const laneIndex = Math.max(0, visibleLanes.indexOf(row.lane))
            const left = ((laneIndex + 1) / (visibleLanes.length + 1)) * 100
            return (
              <button
                key={row.event_id}
                type="button"
                className={`absolute z-20 -translate-x-1/2 -translate-y-0.5 ${range.point ? 'h-3 w-3 rounded-full' : 'w-2 rounded-full'} ${row.failed ? 'bg-red-400' : row.event_id === selectedEventId ? 'bg-blue-600' : 'bg-stone-400 hover:bg-blue-400'}`}
                style={{ left: `${left}%`, top: `${range.top}%`, height: range.point ? undefined : `${range.height}%` }}
                title={`${eventLabel(row.type)} · ${new Date(row.timestamp).toLocaleTimeString()}`}
                onMouseDown={(event) => event.stopPropagation()}
                onClick={() => onSelect(row.event_id)}
              />
            )
          })}
        </div>
      </div>
    </section>
  )
}

export default function RunTracePanel({ open, session, activeRunId, provider, onClose }: RunTracePanelProps) {
  const runs = session?.runs ?? EMPTY_RUNS
  const [selectedRunId, setSelectedRunId] = useState<string | undefined>(activeRunId)
  const [selectedEventId, setSelectedEventId] = useState<string | undefined>()
  const [query, setQuery] = useState('')
  const [rangeEventIds, setRangeEventIds] = useState<string[] | null>(null)

  const activeRun = useMemo(() => {
    const id = selectedRunId ?? activeRunId
    return runs.find((run) => run.runId === id) ?? runs[runs.length - 1]
  }, [runs, selectedRunId, activeRunId])

  const rows = useMemo<TraceRow[]>(() => sortTimelineItems(
    (activeRun?.items ?? []).map((item) => ({ ...item, lane: laneOf(item.type), failed: isFailed(item) })),
  ), [activeRun])

  const filteredRows = useMemo(() => {
    const range = rangeEventIds ? new Set(rangeEventIds) : null
    const rangeRows = range ? rows.filter((row) => range.has(row.event_id)) : rows
    const needle = query.trim().toLowerCase()
    if (!needle) return rangeRows
    return rangeRows.filter((row) => [row.type, eventLabel(row.type), row.tool_name, row.intent, row.summary, row.status]
      .some((value) => String(value ?? '').toLowerCase().includes(needle)))
  }, [query, rangeEventIds, rows])

  const effectiveSelectedEventId = rows.some((row) => row.event_id === selectedEventId)
    ? selectedEventId
    : rows[0]?.event_id
  const selectedEvent = rows.find((row) => row.event_id === effectiveSelectedEventId)
  const firstTime = rows.length > 0 ? eventTimeOf(rows[0]) : Number.NaN
  const lastTime = rows.length > 0 ? Math.max(...rows.map((row) => {
    const ended = row.ended_at ? new Date(row.ended_at).getTime() : Number.NaN
    return Number.isNaN(ended) ? eventTimeOf(row) : ended
  })) : Number.NaN
  const modelTurns = rows.filter((row) => row.type === 'model.started').length
  const toolCalls = new Set(rows.filter((row) => row.type === 'tool.requested' && row.tool_call_id).map((row) => row.tool_call_id)).size

  const selectAndJump = (eventId: string) => {
    setSelectedEventId(eventId)
    window.setTimeout(() => document.getElementById(`audit-event-${eventId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 0)
  }

  if (!open) return null

  return (
    <div className="flex h-full min-h-0 flex-col bg-white" aria-label="运行审计界面">
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-stone-200 px-4 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-stone-800">{session?.title || '新对话'} · 运行审计</div>
          <div className="mt-0.5 font-mono text-[10px] text-stone-400">{activeRun?.runId ?? '无运行记录'}</div>
        </div>
        <Select
          size="small"
          value={activeRun?.runId}
          onChange={(value) => { setSelectedRunId(value); setSelectedEventId(undefined); setRangeEventIds(null) }}
          options={runs.map((run, index) => ({ value: run.runId, label: `R${index + 1} · ${run.status}` }))}
          className="ml-auto w-36"
          aria-label="选择运行"
        />
        <Button icon={<DownloadOutlined />} onClick={() => downloadJson(activeRun)} disabled={!activeRun || rows.length === 0}>
          导出 JSON
        </Button>
        <Button type="text" icon={<CloseOutlined />} onClick={onClose} aria-label="关闭运行审计" />
      </header>

      {!activeRun || rows.length === 0 ? (
        <div className="flex flex-1 items-center justify-center"><Empty description="该运行没有可审计的事件" /></div>
      ) : (
        <>
          <div className="grid shrink-0 grid-cols-3 divide-x divide-stone-100 border-b border-stone-200 bg-stone-50/40">
            <div className="px-5 py-2"><div className="text-[10px] uppercase text-stone-400">Duration</div><div className="font-mono text-sm text-stone-800">{formatDurationMs(lastTime - firstTime)}</div></div>
            <div className="px-5 py-2"><div className="text-[10px] uppercase text-stone-400">Turns</div><div className="font-mono text-sm text-stone-800">{modelTurns}</div></div>
            <div className="px-5 py-2"><div className="text-[10px] uppercase text-stone-400">Calls</div><div className="font-mono text-sm text-stone-800">{toolCalls}</div></div>
          </div>

          <div className="grid min-h-0 flex-1 grid-cols-[136px_minmax(0,1fr)] overflow-y-auto lg:grid-cols-[152px_minmax(0,1fr)_340px] lg:overflow-hidden">
            <AuditTimeline rows={rows} selectedEventId={effectiveSelectedEventId} onSelect={selectAndJump} onRangeChange={setRangeEventIds} />

            <section className="flex min-h-0 min-w-0 flex-col border-r border-stone-200">
              <div className="shrink-0 border-b border-stone-100 p-2">
                <Input
                  size="small"
                  allowClear
                  prefix={<SearchOutlined className="text-stone-400" />}
                  placeholder="搜索事件、工具或状态"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
              <div className="max-h-80 min-h-0 flex-1 overflow-y-auto lg:max-h-none">
                {filteredRows.map((row, index) => (
                  <button
                    id={`audit-event-${row.event_id}`}
                    key={row.event_id}
                    type="button"
                    onClick={() => setSelectedEventId(row.event_id)}
                    className={`grid w-full grid-cols-[64px_72px_minmax(0,1fr)] items-center gap-2 border-b border-stone-100 px-3 py-2 text-left text-xs sm:grid-cols-[72px_88px_minmax(0,1fr)_80px] ${row.event_id === effectiveSelectedEventId ? 'bg-blue-50' : 'hover:bg-stone-50'}`}
                  >
                    <span className="font-mono text-[10px] text-stone-400">{new Date(row.timestamp).toLocaleTimeString()}</span>
                    <Tag color={LANE_COLOR[row.lane]} className="!m-0 w-fit">{LANE_TITLE[row.lane]}</Tag>
                    <span className="min-w-0 truncate text-stone-700">{eventLabel(row.type)}{row.tool_name ? ` · ${row.tool_name}` : ''}</span>
                    <span className={`hidden truncate text-right font-mono text-[10px] sm:block ${row.failed ? 'text-red-600' : 'text-stone-400'}`}>
                      {Number.isNaN(durationOf(row.started_at, row.ended_at)) ? `#${index + 1}` : formatDurationMs(durationOf(row.started_at, row.ended_at))}
                    </span>
                  </button>
                ))}
              </div>
            </section>

            <aside className="col-span-2 min-h-0 overflow-y-auto border-t border-stone-200 p-4 lg:col-span-1 lg:border-t-0">
              {selectedEvent ? (
                <div className="space-y-5">
                  <div>
                    <div className="text-[10px] uppercase text-stone-400">Request / Event</div>
                    <div className="mt-1 text-sm font-semibold text-stone-800">{eventLabel(selectedEvent.type)}</div>
                    <div className="mt-1 break-all font-mono text-[10px] text-stone-400">{selectedEvent.event_id}</div>
                  </div>
                  <div className="border-b border-stone-200 pb-2 text-xs font-medium text-blue-700">Summary</div>
                  <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
                    <dt className="text-stone-400">Status</dt><dd className="text-stone-700">{selectedEvent.status ?? (selectedEvent.failed ? 'failed' : 'completed')}</dd>
                    <dt className="text-stone-400">Phase</dt><dd className="text-stone-700">{selectedEvent.phase ?? selectedEvent.lane}</dd>
                    <dt className="text-stone-400">Model</dt><dd className="break-all text-stone-700">{activeRun.modelId ?? session?.model ?? '未记录'}</dd>
                    <dt className="text-stone-400">Provider</dt><dd className="text-stone-700">{provider ?? '未记录'}</dd>
                    <dt className="text-stone-400">Reasoning</dt><dd className="text-stone-700">{activeRun.reasoningEffort ?? 'not set'}</dd>
                    <dt className="text-stone-400">审批模式</dt><dd className="text-stone-700">{activeRun.permissionMode ?? 'default'}</dd>
                    <dt className="text-stone-400">Tool</dt><dd className="break-all text-stone-700">{selectedEvent.tool_name ?? '—'}</dd>
                    {selectedEvent.args_preview ? (
                      <>
                        <dt className="text-stone-400">参数</dt><dd className="break-all font-mono text-[10px] text-stone-700">{JSON.stringify(selectedEvent.args_preview)}</dd>
                      </>
                    ) : null}
                    {selectedEvent.result_preview ? (
                      <>
                        <dt className="text-stone-400">结果</dt>
                        <dd className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded border border-stone-200/70 bg-stone-50 p-1.5 font-mono text-[10px] leading-relaxed text-stone-700">
                          {selectedEvent.result_preview}
                          {selectedEvent.result_truncated ? '\n\n[预览已截断]' : ''}
                        </dd>
                      </>
                    ) : null}
                    <dt className="text-stone-400">Attempt</dt><dd className="text-stone-700">{selectedEvent.attempt ?? '—'}</dd>
                  </dl>
                  <div>
                    <div className="mb-2 text-xs font-medium text-stone-700">Usage</div>
                    <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
                      <dt className="text-stone-400">Input</dt><dd className="text-stone-700">{selectedEvent.usage?.input_tokens?.toLocaleString() ?? '—'} tok</dd>
                      <dt className="text-stone-400">Cached</dt><dd className="text-stone-700">{selectedEvent.usage?.cached_tokens?.toLocaleString() ?? '—'} tok</dd>
                      <dt className="text-stone-400">Output</dt><dd className="text-stone-700">{selectedEvent.usage?.output_tokens?.toLocaleString() ?? '—'} tok</dd>
                    </dl>
                  </div>
                  <div>
                    <div className="mb-2 text-xs font-medium text-stone-700">Timing</div>
                    <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
                      <dt className="text-stone-400">Started</dt><dd className="break-all font-mono text-[10px] text-stone-700">{selectedEvent.started_at ?? selectedEvent.timestamp}</dd>
                      <dt className="text-stone-400">Ended</dt><dd className="break-all font-mono text-[10px] text-stone-700">{selectedEvent.ended_at ?? '—'}</dd>
                      <dt className="text-stone-400">Duration</dt><dd className="text-stone-700">{formatDurationMs(durationOf(selectedEvent.started_at, selectedEvent.ended_at))}</dd>
                    </dl>
                  </div>
                  {selectedEvent.summary ? <div className="rounded bg-stone-50 p-3 text-xs leading-relaxed text-stone-600">{selectedEvent.summary}</div> : null}
                </div>
              ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择事件查看详情" />}
            </aside>
          </div>
        </>
      )}
    </div>
  )
}

import { useMemo, useState, type ReactNode } from 'react'
import { Button, Drawer, Empty, Select, Table, Tag } from 'antd'
import { DownloadOutlined, NodeIndexOutlined } from '@ant-design/icons'
import type { RunIndexItem, Session, SessionRun } from '../../api/types'
import { eventLabel, isFailed, LANE_TITLE, laneOf, type Lane } from './traceModel'

interface RunTracePanelProps {
  open: boolean
  session: Session | null
  activeRunId?: string
  onClose: () => void
}

interface TraceRow extends RunIndexItem {
  key: string
  lane: Lane
  laneTitle: string
  failed: boolean
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

function formatDuration(startedAt?: string, endedAt?: string): string {
  if (!startedAt || !endedAt) return '—'
  const start = new Date(startedAt).getTime()
  const end = new Date(endedAt).getTime()
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '—'
  const ms = end - start
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

function downloadJson(run: SessionRun | undefined) {
  if (!run) return
  const payload = {
    schema_version: 1,
    run_id: run.runId,
    task_id: run.taskId,
    status: run.status,
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

export default function RunTracePanel({ open, session, activeRunId, onClose }: RunTracePanelProps) {
  const runs = session?.runs ?? EMPTY_RUNS
  const [selectedRunId, setSelectedRunId] = useState<string | undefined>(activeRunId)
  const [selectedEventId, setSelectedEventId] = useState<string | undefined>(undefined)

  const activeRun = useMemo(() => {
    const id = selectedRunId ?? activeRunId
    return runs.find((run) => run.runId === id) ?? runs[runs.length - 1]
  }, [runs, selectedRunId, activeRunId])

  const rows = useMemo<TraceRow[]>(
    () =>
      (activeRun?.items ?? []).map((item) => ({
        ...item,
        key: item.event_id,
        lane: laneOf(item.type),
        laneTitle: LANE_TITLE[laneOf(item.type)],
        failed: isFailed(item),
      })),
    [activeRun],
  )

  const summary = useMemo(() => {
    const failedCount = rows.filter((row) => row.failed).length
    const byLane = new Map<Lane, number>()
    for (const row of rows) byLane.set(row.lane, (byLane.get(row.lane) ?? 0) + 1)
    const first = rows[0]?.timestamp
    const last = rows[rows.length - 1]?.timestamp
    return { failedCount, byLane, first, last }
  }, [rows])

  const selectedEvent = rows.find((row) => row.event_id === selectedEventId)

  // 因果图（简化）：按 parent_event_id 归组，展示模型轮 -> 工具/审批的父子链。
  const tree = useMemo(() => {
    const children = new Map<string, RunIndexItem[]>()
    const roots: RunIndexItem[] = []
    for (const row of rows) {
      const parent = row.parent_event_id
      if (parent && rows.some((candidate) => candidate.event_id === parent || candidate.parent_event_id === parent || candidate.type.startsWith('model.'))) {
        children.set(parent, [...(children.get(parent) ?? []), row])
      } else if (!parent) {
        roots.push(row)
      }
    }
    return { children, roots }
  }, [rows])

  const renderCausal = (parent: RunIndexItem, depth: number): ReactNode => {
    const kids = tree.children.get(parent.event_id) ?? []
    return (
      <div key={parent.event_id} style={{ marginLeft: depth * 16 }}>
        <div className="flex items-center gap-1.5 py-0.5">
          <NodeIndexOutlined className="text-stone-400" />
          <span className="font-mono text-[11px] text-stone-600">{eventLabel(parent.type)}</span>
          <Tag color={LANE_COLOR[laneOf(parent.type)]} className="!mr-0">{LANE_TITLE[laneOf(parent.type)]}</Tag>
        </div>
        {kids.map((kid) => renderCausal(kid, depth + 1))}
      </div>
    )
  }

  return (
    <Drawer
      title={`运行审计 · ${activeRun?.runId ?? '无运行记录'}`}
      open={open}
      onClose={onClose}
      width={720}
      extra={
        <Button
          icon={<DownloadOutlined />}
          onClick={() => downloadJson(activeRun)}
          disabled={!activeRun || rows.length === 0}
        >
          导出脱敏 JSON
        </Button>
      }
    >
      {!activeRun || rows.length === 0 ? (
        <Empty description="该运行没有可审计的事件" />
      ) : (
        <div className="space-y-5">
          {/* 诊断摘要 */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-lg border border-stone-100 bg-stone-50/60 px-3 py-2">
              <div className="text-[11px] text-stone-500">事件总数</div>
              <div className="text-lg font-semibold text-stone-800">{rows.length}</div>
            </div>
            <div className="rounded-lg border border-stone-100 bg-stone-50/60 px-3 py-2">
              <div className="text-[11px] text-stone-500">失败/受阻</div>
              <div className={`text-lg font-semibold ${summary.failedCount > 0 ? 'text-red-600' : 'text-stone-800'}`}>
                {summary.failedCount}
              </div>
            </div>
            <div className="rounded-lg border border-stone-100 bg-stone-50/60 px-3 py-2">
              <div className="text-[11px] text-stone-500">终态</div>
              <div className="text-lg font-semibold text-stone-800">{activeRun.status}</div>
            </div>
            <div className="rounded-lg border border-stone-100 bg-stone-50/60 px-3 py-2">
              <div className="text-[11px] text-stone-500">总耗时</div>
              <div className="text-sm font-semibold text-stone-800">{formatDuration(summary.first, summary.last)}</div>
            </div>
          </div>

          {/* 分层泳道统计 */}
          <div className="flex flex-wrap gap-1.5">
            {[...summary.byLane.entries()].map(([lane, count]) => (
              <Tag key={lane} color={LANE_COLOR[lane]}>
                {LANE_TITLE[lane]} · {count}
              </Tag>
            ))}
          </div>

          {/* Run 选择 + 因果图 */}
          <div className="space-y-2">
            <Select
              size="small"
              value={activeRun.runId}
              onChange={setSelectedRunId}
              options={runs.map((run) => ({ value: run.runId, label: `${run.runId} · ${run.status}` }))}
              className="w-full max-w-md"
              aria-label="选择运行"
            />
            <details className="rounded-lg border border-stone-100 px-3 py-2">
              <summary className="cursor-pointer text-xs font-medium text-stone-600">因果图（父子事件链）</summary>
              <div className="mt-2 space-y-0.5">
                {tree.roots.map((root) => renderCausal(root, 0))}
              </div>
            </details>
          </div>

          {/* 审计表 */}
          <div className="overflow-x-auto">
            <Table<TraceRow>
              size="small"
              dataSource={rows}
              rowKey="event_id"
              pagination={false}
              scroll={{ x: 720, y: 320 }}
              rowClassName={(row) => (row.event_id === selectedEventId ? 'bg-blue-50' : '')}
              onRow={(row) => ({
                onClick: () => setSelectedEventId(row.event_id),
                className: 'cursor-pointer',
              })}
              columns={[
                {
                  title: '时间',
                  dataIndex: 'timestamp',
                  width: 96,
                  render: (value: string) => <span className="font-mono text-[11px] text-stone-500">{new Date(value).toLocaleTimeString()}</span>,
                },
                { title: '事件', dataIndex: 'type', width: 150, render: (_: unknown, row) => eventLabel(row.type) },
                {
                  title: '泳道',
                  dataIndex: 'lane',
                  width: 84,
                  render: (_: unknown, row) => <Tag color={LANE_COLOR[row.lane]} className="!mr-0">{row.laneTitle}</Tag>,
                },
                {
                  title: '状态',
                  dataIndex: 'status',
                  width: 96,
                  render: (_: unknown, row) => (row.status ? <span className={row.failed ? 'text-red-600' : 'text-stone-600'}>{row.status}</span> : '—'),
                },
                {
                  title: '耗时',
                  dataIndex: 'duration',
                  width: 90,
                  render: (_: unknown, row) => <span className="font-mono text-[11px] text-stone-500">{formatDuration(row.started_at, row.ended_at)}</span>,
                },
                { title: '摘要', dataIndex: 'tool_name', render: (_: unknown, row) => row.tool_name ?? row.intent ?? '—' },
              ]}
            />
          </div>

          {/* 事件详情（脱敏后的公开属性） */}
          {selectedEvent ? (
            <details open className="rounded-lg border border-stone-100 px-3 py-2">
              <summary className="cursor-pointer text-xs font-medium text-stone-600">
                事件详情 · {selectedEvent.event_id}
              </summary>
              <pre className="mt-2 overflow-x-auto rounded bg-stone-50 p-3 text-[11px] leading-relaxed text-stone-700">
                {JSON.stringify(selectedEvent, null, 2)}
              </pre>
            </details>
          ) : null}
        </div>
      )}
    </Drawer>
  )
}

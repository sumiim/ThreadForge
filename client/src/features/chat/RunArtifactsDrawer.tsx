import { useEffect, useState } from 'react'
import { Drawer, Empty, Select, Spin, Tabs } from 'antd'
import { friendlyMessage, getArtifactText, listArtifacts } from '../../api/client'
import type { Session } from '../../api/types'

interface RunArtifactsDrawerProps {
  open: boolean
  session: Session | null
  onClose: () => void
  activeRunId?: string
}

interface DrawerData {
  runId: string
  items: string[]
  taskState: string | null
  trace: TraceLine[]
  report: string | null
}

interface TraceLine {
  seq: number
  event: string
  created_at: string
  detail: string
}

// 解析 trace NDJSON 行 -> 展示行；trace 事件为 {event, created_at, ...payload} 结构
function parseTraceLine(line: string, seq: number): TraceLine {
  try {
    const obj = JSON.parse(line) as Record<string, unknown>
    const event = String(obj.event ?? obj.type ?? 'trace')
    const created_at = String(obj.created_at ?? obj.ts ?? '')
    const rest = { ...obj }
    delete rest.event
    delete rest.created_at
    delete rest.type
    delete rest.ts
    const detail = Object.keys(rest).length > 0 ? JSON.stringify(rest, null, 0).slice(0, 120) : ''
    return { seq, event, created_at, detail }
  } catch {
    return { seq, event: 'raw', created_at: '', detail: line.slice(0, 120) }
  }
}

function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// 运行结果：task_state / trace / report，来自 GET /api/v1/runs/{run_id}/artifacts
export default function RunArtifactsDrawer({ open, session, onClose, activeRunId }: RunArtifactsDrawerProps) {
  const defaultRunId = activeRunId ?? session?.activeRunId ?? session?.lastRunId
  const [selection, setSelection] = useState<{ sessionId: string; runId?: string }>({ sessionId: '' })
  const selectedRunId = selection.sessionId === session?.id ? selection.runId : undefined
  const runId = selectedRunId ?? defaultRunId
  const [data, setData] = useState<DrawerData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // runId 变化（切换会话）时，避免旧数据闪一下：只展示与当前 run 匹配的缓存
  const current = data && data.runId === runId ? data : null

  useEffect(() => {
    if (!open || !runId) return
    let cancelled = false
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const { items } = await listArtifacts(runId)
        const names = new Set(items.map((item) => item.name))
        const [stateText, traceText, reportText] = await Promise.all([
          names.has('task_state') ? getArtifactText(runId, 'task_state') : null,
          names.has('trace') ? getArtifactText(runId, 'trace') : null,
          names.has('report') ? getArtifactText(runId, 'report') : null,
        ])
        if (cancelled) return
        setData({
          runId,
          items: items.map((item) => item.name),
          taskState: stateText,
          trace: traceText
            ? traceText
                .split('\n')
                .filter(Boolean)
                .map((line, i) => parseTraceLine(line, i + 1))
            : [],
          report: reportText,
        })
      } catch (err) {
        if (!cancelled) setError(friendlyMessage(err))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, runId])

  const hasAny = current && (current.taskState || current.trace.length > 0 || current.report)

  return (
    <Drawer title="运行结果" open={open} onClose={onClose} size={440}>
      {loading ? (
        <div className="flex h-40 items-center justify-center">
          <Spin size="small" />
        </div>
      ) : !runId ? (
        <Empty description="当前会话还没有运行记录" />
      ) : error ? (
        <Empty description={error} />
      ) : !hasAny ? (
        <Empty description="运行尚未产生制品" />
      ) : (
        <div>
          <p className="mb-3 text-xs leading-relaxed text-stone-500">
            状态记录任务预算和终态；Trace 是按时间排列的运行事件；报告汇总本次运行结果。
          </p>
          {session?.runs && session.runs.length > 1 ? (
            <Select
              className="mb-3 w-full"
              value={runId}
              onChange={(value) => setSelection({ sessionId: session.id, runId: value })}
              options={session.runs.slice().reverse().map((run, index) => ({
                value: run.runId,
                label: `Run ${session.runs!.length - index} · ${run.status}`,
              }))}
              aria-label="当前运行"
            />
          ) : null}
        <Tabs
          items={[
            {
              key: 'state',
              label: '状态',
              children: current.taskState ? (
                <pre className="m-0 whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-stone-600">
                  {prettyJson(current.taskState)}
                </pre>
              ) : (
                <Empty description="无 task_state 制品" />
              ),
            },
            {
              key: 'trace',
              label: `Trace${current.trace.length > 0 ? ` (${current.trace.length})` : ''}`,
              children:
                current.trace.length > 0 ? (
                  <div className="space-y-0.5">
                    {current.trace.map((e) => (
                      <div
                        key={e.seq}
                        className="flex items-baseline gap-3 rounded-lg px-2 py-1.5 font-mono text-[11px] hover:bg-stone-50"
                      >
                        <span className="w-8 shrink-0 text-right text-stone-400">{e.seq}</span>
                        <span className="w-40 shrink-0 text-blue-700">{e.event}</span>
                        <span className="w-16 shrink-0 text-right text-stone-400">{formatTime(e.created_at)}</span>
                        <span className="truncate text-stone-500" title={e.detail}>
                          {e.detail}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty description="无 trace 制品" />
                ),
            },
            {
              key: 'report',
              label: '报告',
              children: current.report ? (
                <pre className="m-0 whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-stone-600">
                  {prettyJson(current.report)}
                </pre>
              ) : (
                <Empty description="无 report 制品" />
              ),
            },
          ]}
        />
        </div>
      )}
    </Drawer>
  )
}

function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

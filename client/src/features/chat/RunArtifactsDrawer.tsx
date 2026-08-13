import { useEffect, useState } from 'react'
import { Drawer, Empty, Select, Spin, Tabs, Tooltip } from 'antd'
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
  payload: Record<string, unknown>
  detail: string
}

// 解析 trace NDJSON 行 -> 展示行；trace 事件为 {event, created_at, ...payload} 结构。
// payload 保留完整字段，供诊断摘要与审计表读取（stop_reason/intent/tool_name/attempts…）。
function parseTraceLine(line: string, seq: number): TraceLine {
  try {
    const obj = JSON.parse(line) as Record<string, unknown>
    const event = String(obj.event ?? obj.type ?? 'trace')
    const created_at = String(obj.created_at ?? obj.ts ?? '')
    const payload = { ...obj }
    delete payload.event
    delete payload.created_at
    delete payload.type
    delete payload.ts
    const detail = Object.keys(payload).length > 0 ? JSON.stringify(payload).slice(0, 160) : ''
    return { seq, event, created_at, payload, detail }
  } catch {
    return { seq, event: 'raw', created_at: '', payload: {}, detail: line.slice(0, 160) }
  }
}

function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// 事件类型 -> 颜色（审计表视觉分层）
function traceColor(event: string): string {
  if (/failed|rejected|exhausted|violated|error/i.test(event)) return 'text-red-600'
  if (event.startsWith('intent') || event.startsWith('route')) return 'text-blue-700'
  if (event.startsWith('plan')) return 'text-violet-700'
  if (event.startsWith('review')) return 'text-amber-700'
  if (event.startsWith('tool')) return 'text-emerald-700'
  if (event.startsWith('model') || event.startsWith('conversation')) return 'text-cyan-700'
  return 'text-stone-600'
}

function firstPayload(trace: TraceLine[], eventName: string): Record<string, unknown> | undefined {
  return trace.find((line) => line.event === eventName)?.payload
}

// 诊断摘要：只基于事实事件，不猜测根因。
function computeDiagnostic(trace: TraceLine[]) {
  const runFinished = firstPayload(trace, 'run_finished') ?? firstPayload(trace, 'run_started')
  const intentClassified = firstPayload(trace, 'intent_classified')
  const reviewCompleted = firstPayload(trace, 'review_completed')
  const toolExecuted = trace.filter((line) => line.event === 'tool_executed').length
  const modelRounds = trace.filter((line) =>
    line.event === 'model_requested' || line.event === 'plan_requested' || line.event === 'intent_classification_requested' || line.event === 'conversation_model_requested',
  ).length
  const failures = trace.filter((line) =>
    /failed|rejected|exhausted|violated|error/i.test(line.event) || String(line.payload.status ?? '') === 'failed',
  ).length

  const durationMs = Number(runFinished?.run_duration_ms ?? runFinished?.duration_ms ?? 0)
  const stopReason = String(runFinished?.stop_reason ?? '')
  const status = String(runFinished?.status ?? '')
  const intent = String(intentClassified?.resolved_intent ?? '')
  const reviewStatus = String(reviewCompleted?.status ?? '')

  return {
    durationMs: Number.isFinite(durationMs) ? durationMs : 0,
    eventCount: trace.length,
    modelRounds,
    toolExecuted,
    failures,
    stopReason,
    status,
    intent,
    reviewStatus,
  }
}

function formatDuration(ms: number): string {
  if (ms <= 0) return '-'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m${s % 60}s`
}

function Metric({ label, value, tone = 'stone' }: { label: string; value: string; tone?: 'stone' | 'red' | 'emerald' }) {
  const color = tone === 'red' ? 'text-red-600' : tone === 'emerald' ? 'text-emerald-600' : 'text-stone-800'
  return (
    <div className="rounded-lg bg-stone-50 px-2 py-1.5">
      <div className="text-[10px] text-stone-400">{label}</div>
      <div className={`text-sm font-medium ${color}`}>{value}</div>
    </div>
  )
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
  const diagnostic = current ? computeDiagnostic(current.trace) : null

  return (
    <Drawer title="运行结果" open={open} onClose={onClose} size={520}>
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

          {diagnostic ? (
            <div className="mb-3">
              <div className="mb-1.5 text-[11px] font-medium text-stone-500">诊断摘要（据事实事件）</div>
              <div className="grid grid-cols-4 gap-1.5">
                <Metric label="总耗时" value={formatDuration(diagnostic.durationMs)} />
                <Metric label="事件数" value={String(diagnostic.eventCount)} />
                <Metric label="模型轮次" value={diagnostic.modelRounds > 0 ? String(diagnostic.modelRounds) : '-'} />
                <Metric label="工具调用" value={diagnostic.toolExecuted > 0 ? String(diagnostic.toolExecuted) : '-'} />
                <Metric label="意图" value={diagnostic.intent || '-'} />
                <Metric label="审查" value={diagnostic.reviewStatus || '-'} />
                <Metric label="终态" value={diagnostic.status || diagnostic.stopReason || '-'} />
                <Metric label="异常" value={String(diagnostic.failures)} tone={diagnostic.failures > 0 ? 'red' : 'emerald'} />
              </div>
              {diagnostic.stopReason ? (
                <div className="mt-1.5 text-[11px] text-stone-500">
                  停止原因：<span className="text-stone-700">{diagnostic.stopReason}</span>
                </div>
              ) : null}
            </div>
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
                label: `审计 Trace${current.trace.length > 0 ? ` (${current.trace.length})` : ''}`,
                children:
                  current.trace.length > 0 ? (
                    <div className="space-y-0.5">
                      {current.trace.map((e) => (
                        <Tooltip key={e.seq} title={e.detail || e.event} placement="left">
                          <div className="flex items-baseline gap-3 rounded-lg px-2 py-1.5 font-mono text-[11px] hover:bg-stone-50">
                            <span className="w-8 shrink-0 text-right text-stone-400">{e.seq}</span>
                            <span className={`w-40 shrink-0 ${traceColor(e.event)}`}>{e.event}</span>
                            <span className="w-16 shrink-0 text-right text-stone-400">{formatTime(e.created_at)}</span>
                            <span className="min-w-0 flex-1 truncate text-stone-500">{e.detail}</span>
                          </div>
                        </Tooltip>
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

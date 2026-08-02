import { Drawer, Empty, Tabs } from 'antd'
import { buildMockArtifacts } from '../../api/mock'
import type { RunArtifacts, Session } from '../../api/types'
import { toolStatusMeta } from './toolStatus'

interface RunArtifactsDrawerProps {
  open: boolean
  session: Session
  onClose: () => void
}

// 任务级状态（比 ToolStatus 多 awaiting_approval，running 用品牌蓝）
const taskStatusMeta: Record<string, { label: string; className: string }> = {
  ...toolStatusMeta,
  awaiting_approval: { label: '等待审批', className: 'text-amber-600' },
  running: { label: '运行中', className: 'text-blue-600' },
}

// 运行结果：task_state / trace / report，对应 GET /api/v1/runs/{run_id}/artifacts
export default function RunArtifactsDrawer({ open, session, onClose }: RunArtifactsDrawerProps) {
  const artifacts = buildMockArtifacts(session)

  return (
    <Drawer title="运行结果" open={open} onClose={onClose} width={440}>
      {!artifacts ? (
        <Empty description="当前会话还没有运行记录" />
      ) : (
        <Tabs
          items={[
            {
              key: 'state',
              label: '状态',
              children: <TaskStateView artifacts={artifacts} />,
            },
            {
              key: 'trace',
              label: 'Trace',
              children: <TraceView artifacts={artifacts} />,
            },
            {
              key: 'report',
              label: '报告',
              children: (
                <pre className="m-0 whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-stone-600">
                  {artifacts.report}
                </pre>
              ),
            },
          ]}
        />
      )}
    </Drawer>
  )
}

function TaskStateView({ artifacts }: { artifacts: RunArtifacts }) {
  const s = artifacts.taskState
  const st = taskStatusMeta[s.status] ?? { label: s.status, className: 'text-stone-500' }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-stone-100 bg-stone-50 p-3.5">
        <div className="flex items-center justify-between">
          <span className="font-mono text-xs text-stone-500">{s.runId}</span>
          <span className={`font-mono text-xs ${st.className}`}>{st.label}</span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div>
            <div className="text-xs text-stone-400">步骤数</div>
            <div className="mt-0.5 font-mono text-base text-stone-800">{s.steps}</div>
          </div>
          <div>
            <div className="text-xs text-stone-400">工具调用</div>
            <div className="mt-0.5 font-mono text-base text-stone-800">{s.toolCalls.length}</div>
          </div>
        </div>
      </div>

      <div className="space-y-1.5">
        {s.toolCalls.map((t, i) => {
          const ts = toolStatusMeta[t.status]
          return (
            <div
              key={i}
              className="flex items-center justify-between rounded-lg border border-stone-100 px-3 py-2"
            >
              <span className="font-mono text-xs text-stone-700">{t.toolName}</span>
              <span className="flex items-center gap-2 font-mono text-[11px] text-stone-500">
                <span className={ts.className}>{ts.label}</span>
                <span>审批 {t.approvals}</span>
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TraceView({ artifacts }: { artifacts: RunArtifacts }) {
  return (
    <div className="space-y-0.5">
      {artifacts.trace.map((e) => (
        <div
          key={e.seq}
          className="flex items-baseline gap-3 rounded-lg px-2 py-1.5 font-mono text-[11px] hover:bg-stone-50"
        >
          <span className="w-8 shrink-0 text-right text-stone-400">{e.seq}</span>
          <span className="w-36 shrink-0 text-blue-700">{e.type}</span>
          <span className="truncate text-stone-500" title={e.detail}>
            {e.detail}
          </span>
        </div>
      ))}
    </div>
  )
}

import type { ToolStatus } from '../../api/types'

// 工具状态语义色（独立于品牌 accent）：ToolList 与运行结果抽屉共用
export const toolStatusMeta: Record<ToolStatus, { label: string; className: string }> = {
  pending: { label: '待审批', className: 'text-amber-600' },
  running: { label: '运行中', className: 'text-slate-500' },
  completed: { label: '已完成', className: 'text-green-700' },
  rejected: { label: '已拒绝', className: 'text-red-600' },
  error: { label: '出错', className: 'text-red-600' },
}

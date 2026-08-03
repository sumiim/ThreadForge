import { Button } from 'antd'
import { AlertOutlined } from '@ant-design/icons'

interface ApprovalNoticeProps {
  count: number
  onLocate: () => void
}

// 输入框上方的待审批提醒：工具等审批时提示并定位到卡片
// V1 为 per_call_only 逐次审批（无自动允许），此条仅做提醒，不含审批模式切换
export default function ApprovalNotice({ count, onLocate }: ApprovalNoticeProps) {
  if (count === 0) return null

  return (
    <div className="mx-auto w-full max-w-4xl shrink-0 px-6 pb-2 lg:px-10">
      <div className="flex items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-3.5 py-2.5">
        <span className="flex items-center gap-2 text-xs text-amber-800">
          <AlertOutlined />
          {count} 个工具等待你的审批
        </span>
        <Button
          size="small"
          onClick={onLocate}
          className="text-amber-700 hover:!border-amber-500 hover:!text-amber-700"
        >
          定位
        </Button>
      </div>
    </div>
  )
}

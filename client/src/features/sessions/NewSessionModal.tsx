import { Modal, Radio } from 'antd'
import { WORKSPACE_OPTIONS } from '../../api/mock'

interface NewSessionModalProps {
  open: boolean
  selected: string
  onSelect: (workspace: string) => void
  onCreate: () => void
  onCancel: () => void
}

// 新建会话：选择工作区（对应 GET /api/v1/workspaces + 路径边界校验）
export default function NewSessionModal({
  open,
  selected,
  onSelect,
  onCreate,
  onCancel,
}: NewSessionModalProps) {
  return (
    <Modal
      title="新建会话"
      open={open}
      onOk={onCreate}
      onCancel={onCancel}
      okText="创建"
      cancelText="取消"
    >
      <div className="mb-2 text-sm font-medium text-stone-800">工作区</div>
      <Radio.Group
        value={selected}
        onChange={(e) => onSelect(e.target.value)}
        className="flex w-full flex-col gap-2"
      >
        {WORKSPACE_OPTIONS.map((w) => (
          <Radio key={w} value={w} className="w-full rounded-lg border border-stone-200 px-3 py-2">
            <span className="font-mono text-xs text-stone-700">{w}</span>
          </Radio>
        ))}
      </Radio.Group>
      <p className="mt-3 text-xs text-stone-500">Agent 的工具调用仅限所选工作区内的路径。</p>
    </Modal>
  )
}

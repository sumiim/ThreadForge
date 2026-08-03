import { Modal, Radio } from 'antd'
import type { Workspace } from '../../api/types'

interface NewSessionModalProps {
  open: boolean
  workspaces: Workspace[]
  selected: string | null
  onSelect: (workspaceId: string) => void
  onCreate: () => void
  onCancel: () => void
}

// 新建会话：选择工作区（GET /api/v1/workspaces 返回的可用工作区）
export default function NewSessionModal({
  open,
  workspaces,
  selected,
  onSelect,
  onCreate,
  onCancel,
}: NewSessionModalProps) {
  const selectable = workspaces.filter((w) => w.available)
  const value = selected && selectable.some((w) => w.workspace_id === selected) ? selected : undefined

  return (
    <Modal
      title="新建会话"
      open={open}
      onOk={onCreate}
      onCancel={onCancel}
      okText="创建"
      cancelText="取消"
      okButtonProps={{ disabled: !value }}
    >
      <div className="mb-2 text-sm font-medium text-stone-800">工作区</div>
      {selectable.length === 0 ? (
        <div className="rounded-lg border border-stone-200 px-3 py-4 text-center text-xs text-stone-500">
          后端未返回可用工作区，请检查 api-server 配置
        </div>
      ) : (
        <Radio.Group
          value={value}
          onChange={(e) => onSelect(e.target.value as string)}
          className="flex w-full flex-col gap-2"
        >
          {selectable.map((w) => (
            <Radio key={w.workspace_id} value={w.workspace_id} className="w-full rounded-lg border border-stone-200 px-3 py-2">
              <span className="font-mono text-xs text-stone-700">{w.display_path}</span>
            </Radio>
          ))}
        </Radio.Group>
      )}
      <p className="mt-3 text-xs text-stone-500">Agent 的工具调用仅限所选工作区内的路径。</p>
    </Modal>
  )
}

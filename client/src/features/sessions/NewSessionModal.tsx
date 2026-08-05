import { Button, Modal, Radio } from 'antd'
import type { Workspace } from '../../api/types'

interface NewSessionModalProps {
  open: boolean
  workspaces: Workspace[]
  selected: string | null
  onSelect: (workspaceId: string) => void
  onCreate: () => void
  onCancel: () => void
  onOpenSettings: () => void
}

// 新建会话：选择工作区（GET /api/v1/workspaces 返回的可用工作区）
export default function NewSessionModal({
  open,
  workspaces,
  selected,
  onSelect,
  onCreate,
  onCancel,
  onOpenSettings,
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
        <div className="flex flex-col items-center gap-3 rounded-lg border border-stone-200 px-3 py-4 text-center text-xs text-stone-500">
          <span>暂无可用工作区，请先连接 Worker 并授权一个本地目录。</span>
          <Button
            size="small"
            onClick={() => {
              onCancel()
              onOpenSettings()
            }}
          >
            打开 Worker 设置
          </Button>
        </div>
      ) : (
        <Radio.Group
          value={value}
          onChange={(e) => onSelect(e.target.value as string)}
          className="flex w-full flex-col gap-2"
        >
          {selectable.map((w) => (
            <Radio key={w.workspace_id} value={w.workspace_id} className="w-full rounded-lg border border-stone-200 px-3 py-2">
              <span className="flex min-w-0 flex-col">
                <span className="truncate font-mono text-xs text-stone-700">{w.display_path}</span>
                <span className="text-[11px] text-stone-400">
                  {w.execution_environment === 'local_worker'
                    ? `${w.device_name ?? '本地 Worker'}${w.device_platform ? ` · ${w.device_platform}` : ''}${w.model_configured ? '' : ' · 模型未配置'}`
                    : '服务器工作区'}
                </span>
              </span>
            </Radio>
          ))}
        </Radio.Group>
      )}
      <p className="mt-3 text-xs text-stone-500">
        本地 Worker 工作区的真实路径只保存在对应设备；Agent 工具调用仅限所选目录。
      </p>
    </Modal>
  )
}

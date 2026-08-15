import { useMemo, useState } from 'react'
import { Button, Input, Select } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'

import type { ModelCapability, ReasoningEffort } from '../../api/types'

interface ComposerProps {
  model: string
  modelOptions: ModelCapability[]
  running: boolean
  stopping?: boolean
  disabled?: boolean
  onSend: (content: string, modelId?: string, reasoningEffort?: ReasoningEffort) => void
  onStop: () => void
}

// 输入区：16px 圆角容器 + focus 时 accent ring；模型名显示在右下角
const effortLabels: Record<ReasoningEffort, string> = {
  none: '无',
  minimal: '最小',
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '极高',
}

export default function Composer({ model, modelOptions, running, stopping = false, disabled = false, onSend, onStop }: ComposerProps) {
  const [value, setValue] = useState('')
  const [modelId, setModelId] = useState(modelOptions[0]?.id ?? model)
  const activeModel = useMemo(
    () => modelOptions.find((item) => item.id === modelId) ?? modelOptions[0],
    [modelId, modelOptions],
  )
  const efforts: ReasoningEffort[] = activeModel?.reasoning_efforts?.length
    ? activeModel.reasoning_efforts
    : ['none']
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>(efforts[0])
  const activeReasoningEffort = efforts.includes(reasoningEffort) ? reasoningEffort : efforts[0]

  const handleSend = () => {
    if (!value.trim() || running || disabled) return
    onSend(value, activeModel?.id ?? model, activeReasoningEffort)
    setValue('')
  }

  return (
    <div className="shrink-0 px-3 pb-4 sm:px-6 sm:pb-6 lg:px-10">
      <div className="mx-auto max-w-4xl">
        <div className="rounded-2xl border border-stone-200 bg-white shadow-sm transition-shadow focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-100">
          <div className="px-4 pb-1 pt-3">
            <Input.TextArea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault()
                  handleSend()
                }
              }}
              placeholder={running ? 'Agent 正在运行…' : disabled ? '会话历史尚未就绪' : '描述任务，Enter 发送，Shift + Enter 换行'}
              autoSize={{ minRows: 1, maxRows: 8 }}
              disabled={running || disabled}
              variant="borderless"
              className="text-sm"
              // 压掉 antd borderless 的 focus-visible 内描边（键盘焦点可见性由容器 ring 承担）
              style={{ padding: 0, border: 'none', boxShadow: 'none', outline: 'none' }}
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 border-t border-stone-100 px-4 py-2">
            <span className="hidden font-mono text-[11px] text-stone-500 sm:inline">Enter 发送 · Shift + Enter 换行</span>
            <div className="ml-auto flex items-center gap-3">
              <div className="flex h-8 items-center gap-1">
                <Select
                  size="small"
                  value={activeModel?.id ?? model}
                  disabled={running || disabled}
                  onChange={setModelId}
                  options={modelOptions.map((item) => ({ value: item.id, label: item.display_name }))}
                  className="min-w-28 max-w-44"
                  aria-label="模型"
                />
                <Select
                  size="small"
                  value={activeReasoningEffort}
                  disabled={running || disabled}
                  onChange={(value) => setReasoningEffort(value as ReasoningEffort)}
                  options={efforts.map((effort) => ({ value: effort, label: effortLabels[effort] }))}
                  className="w-20"
                  aria-label="推理强度"
                />
              </div>
              {running ? (
                <Button
                  danger
                  type="primary"
                  icon={<StopOutlined />}
                  onClick={onStop}
                  loading={stopping}
                  disabled={stopping}
                  className="transition-transform active:scale-95"
                >
                  {stopping ? '正在停止' : '停止'}
                </Button>
              ) : (
                <Button
                  type="primary"
                  shape="circle"
                  icon={<SendOutlined />}
                  disabled={disabled || !value.trim()}
                  onClick={handleSend}
                  aria-label="发送"
                  className="transition-transform active:scale-95"
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

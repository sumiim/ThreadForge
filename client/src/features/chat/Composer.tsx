import { useEffect, useMemo, useState } from 'react'
import { Button, Input, Modal, Select, message } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'

import { activateProvider, listProviders } from '../../api/client'
import type { ModelCapability, PermissionMode, Provider, ReasoningEffort } from '../../api/types'

interface ComposerProps {
  model: string
  modelOptions: ModelCapability[]
  running: boolean
  stopping?: boolean
  disabled?: boolean
  onSend: (content: string, modelId?: string, reasoningEffort?: ReasoningEffort, permissionMode?: PermissionMode) => void
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
  max: '最高',
}

const permissionModeLabels: Record<PermissionMode, string> = {
  plan: '只读规划',
  acceptEdits: '接受编辑',
  default: '逐次审批',
  bypass: '免审批',
}

// reasoning 档位记忆：按 model 记最后选择，刷新/切模型后恢复（P3 再升级到 provider:model 维度）。
const REASONING_EFFORT_KEY = 'threadforge.reasoningEffort'

function readPersistedEffort(modelId: string, efforts: ReasoningEffort[]): ReasoningEffort {
  try {
    const raw = localStorage.getItem(REASONING_EFFORT_KEY)
    if (!raw) return efforts[0]
    const map = JSON.parse(raw) as Record<string, unknown>
    const value = map[`model:${modelId}`]
    if (typeof value === 'string' && efforts.includes(value as ReasoningEffort)) {
      return value as ReasoningEffort
    }
  } catch {
    // 忽略损坏的 localStorage
  }
  return efforts[0]
}

function writePersistedEffort(modelId: string, effort: ReasoningEffort): void {
  try {
    const raw = localStorage.getItem(REASONING_EFFORT_KEY)
    const map: Record<string, unknown> = raw ? JSON.parse(raw) : {}
    map[`model:${modelId}`] = effort
    localStorage.setItem(REASONING_EFFORT_KEY, JSON.stringify(map))
  } catch {
    // 忽略存储失败（隐私模式等）
  }
}

// 审批模式记忆：default/plan/acceptEdits 持久化；bypass 不持久化（每次需显式二次确认）。
const PERMISSION_MODE_KEY = 'threadforge.permissionMode'

function readPersistedPermissionMode(): PermissionMode {
  try {
    const value = localStorage.getItem(PERMISSION_MODE_KEY)
    if (value === 'plan' || value === 'acceptEdits' || value === 'default') {
      return value
    }
  } catch {
    // 忽略
  }
  return 'default'
}

function writePersistedPermissionMode(mode: PermissionMode): void {
  try {
    if (mode === 'bypass') {
      localStorage.removeItem(PERMISSION_MODE_KEY)
      return
    }
    localStorage.setItem(PERMISSION_MODE_KEY, mode)
  } catch {
    // 忽略
  }
}

export default function Composer({ model, modelOptions, running, stopping = false, disabled = false, onSend, onStop }: ComposerProps) {
  const [value, setValue] = useState('')
  const [providers, setProviders] = useState<Provider[]>([])
  useEffect(() => {
    listProviders()
      .then(({ providers: list }) => setProviders(list))
      .catch(() => {})
  }, [])
  const [modelId, setModelId] = useState(modelOptions[0]?.id ?? model)
  // 默认 provider 已「测试连接」发现模型时，用其模型列表 + 推理档位替代 env 单模型。
  const defaultProvider = providers.find((item) => item.is_default) ?? providers[0]
  const effectiveModelOptions: ModelCapability[] = useMemo(() => {
    if (!defaultProvider?.models?.length) return modelOptions
    const efforts: ReasoningEffort[] = defaultProvider.reasoning_efforts?.length
      ? defaultProvider.reasoning_efforts
      : ['none']
    return defaultProvider.models.map((id) => ({ id, display_name: id, reasoning_efforts: efforts }))
  }, [defaultProvider, modelOptions])
  const activeModel = useMemo(
    () => effectiveModelOptions.find((item) => item.id === modelId) ?? effectiveModelOptions[0],
    [modelId, effectiveModelOptions],
  )
  const efforts: ReasoningEffort[] = activeModel?.reasoning_efforts?.length
    ? activeModel.reasoning_efforts
    : ['none']
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>(() => readPersistedEffort(modelId, efforts))
  const activeReasoningEffort = efforts.includes(reasoningEffort) ? reasoningEffort : efforts[0]
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(() => readPersistedPermissionMode())

  const handleSend = () => {
    if (!value.trim() || disabled) return
    onSend(value, activeModel?.id ?? model, activeReasoningEffort, permissionMode)
    setValue('')
  }

  const handleProviderChange = async (nextProviderId: string) => {
    try {
      await activateProvider(nextProviderId)
      const { providers: list } = await listProviders()
      setProviders(list)
      setModelId('')
    } catch {
      message.warning('切换供应商失败')
    }
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
              placeholder={running ? 'Agent 正在运行，可继续补充要求（Enter 追加）' : disabled ? '会话历史尚未就绪' : '描述任务，Enter 发送，Shift + Enter 换行'}
              autoSize={{ minRows: 1, maxRows: 8 }}
              disabled={disabled}
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
                {providers.length > 0 ? (
                  <Select
                    size="small"
                    value={defaultProvider?.provider_id}
                    disabled={running || disabled}
                    onChange={handleProviderChange}
                    options={providers.map((p) => ({ value: p.provider_id, label: p.name }))}
                    className="min-w-24 max-w-32"
                    aria-label="供应商"
                  />
                ) : null}
                <Select
                  size="small"
                  value={activeModel?.id ?? model}
                  disabled={running || disabled}
                  onChange={(value) => {
                    const nextModelId = value
                    setModelId(nextModelId)
                    const nextModel = effectiveModelOptions.find((item) => item.id === nextModelId)
                    const nextEfforts: ReasoningEffort[] = nextModel?.reasoning_efforts?.length
                      ? nextModel.reasoning_efforts
                      : ['none']
                    setReasoningEffort(readPersistedEffort(nextModelId, nextEfforts))
                  }}
                  options={effectiveModelOptions.map((item) => ({ value: item.id, label: item.display_name }))}
                  className="min-w-28 max-w-44"
                  aria-label="模型"
                />
                <Select
                  size="small"
                  value={activeReasoningEffort}
                  disabled={running || disabled}
                  onChange={(value) => {
                    const effort = value as ReasoningEffort
                    setReasoningEffort(effort)
                    writePersistedEffort(activeModel?.id ?? model, effort)
                  }}
                  options={efforts.map((effort) => ({ value: effort, label: effortLabels[effort] }))}
                  className="w-20"
                  aria-label="推理强度"
                />
                <Select
                  size="small"
                  value={permissionMode}
                  disabled={running || disabled}
                  onChange={(value) => {
                    const mode = value as PermissionMode
                    if (mode === 'bypass') {
                      Modal.confirm({
                        title: '确认免审批？',
                        content: '免审批模式下，所有危险工具（写文件 / Shell）将不再逐次请求批准。',
                        okText: '确认免审批',
                        okButtonProps: { danger: true },
                        cancelText: '取消',
                        onOk: () => setPermissionMode('bypass'),
                      })
                    } else {
                      setPermissionMode(mode)
                      writePersistedPermissionMode(mode)
                    }
                  }}
                  options={(Object.keys(permissionModeLabels) as PermissionMode[]).map((mode) => ({ value: mode, label: permissionModeLabels[mode] }))}
                  className="w-24"
                  aria-label="审批模式"
                />
              </div>
              {running ? (
                <>
                  <Button
                    type="primary"
                    shape="circle"
                    icon={<SendOutlined />}
                    disabled={!value.trim()}
                    onClick={handleSend}
                    aria-label="追加"
                    title="追加到当前任务"
                    className="transition-transform active:scale-95"
                  />
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
                </>
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

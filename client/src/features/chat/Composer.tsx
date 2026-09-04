import { useEffect, useMemo, useState } from 'react'
import { Button, Input, Modal, Popover } from 'antd'
import { CheckOutlined, DownOutlined, RightOutlined, SendOutlined, StopOutlined } from '@ant-design/icons'

import { listProviders } from '../../api/client'
import type { ModelCapability, PermissionMode, Provider, ReasoningEffort } from '../../api/types'
import { providerModelIds } from './model-options'
import { loadSessionSettings, saveSessionSettings, type SessionSettings } from './session-settings'

interface ComposerProps {
  model: string
  modelOptions: ModelCapability[]
  running: boolean
  stopping?: boolean
  disabled?: boolean
  /** 当前会话绑定的本地 Worker；用于把供应商列表和“设为默认”限定到这台设备。 */
  deviceId?: string
  /** 当前会话 id；用于按会话持久化 主循环/review/审批 选择。 */
  sessionId?: string
  onSend: (content: string, modelId?: string, reasoningEffort?: ReasoningEffort, permissionMode?: PermissionMode, providerId?: string, reviewProviderId?: string, reviewModelId?: string, reviewReasoningEffort?: ReasoningEffort) => void
  onStop: () => void
}

// 推理档位标签
const effortLabels: Record<ReasoningEffort, string> = {
  none: 'None',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra High',
  max: 'Max',
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

// 下拉面板的导航层级：根（模型/推理等级/审批模式）→ 各自展开成列表。
type PanelView = 'root' | 'model' | 'effort' | 'approval'

interface ModelOption {
  id: string
  display_name: string
  reasoning_efforts: ReasoningEffort[]
  /** 该模型属于哪个供应商；选择时用于激活对应供应商。 */
  providerId?: string
}

interface ModelGroup {
  key: string
  label: string
  options: ModelOption[]
}

function modelOptionsFor(provider: Provider | undefined, fallback: ModelCapability[]): ModelOption[] {
  const modelIds = providerModelIds(provider)
  if (provider && modelIds.length) {
    const efforts: ReasoningEffort[] = provider.reasoning_efforts?.length
      ? provider.reasoning_efforts
      : ['none']
    return modelIds.map((id) => ({
      id,
      display_name: id,
      reasoning_efforts: efforts,
      providerId: provider.provider_id,
    }))
  }
  return fallback.map((m) => ({ id: m.id, display_name: m.display_name, reasoning_efforts: m.reasoning_efforts }))
}

export default function Composer({ model, modelOptions, running, stopping = false, disabled = false, deviceId, sessionId, onSend, onStop }: ComposerProps) {
  const [value, setValue] = useState('')
  const [providers, setProviders] = useState<Provider[]>([])
  useEffect(() => {
    listProviders()
      .then(({ providers: list }) => setProviders(deviceId ? list.filter((item) => item.device_id === deviceId) : list))
      .catch(() => {})
  }, [deviceId])
  // §会话级持久化（2026-09-03）：启动时按 session 恢复上次的主循环/review/审批选择。
  const initialSettings = useMemo(() => loadSessionSettings(sessionId), [sessionId])
  const [modelId, setModelId] = useState(initialSettings.modelId ?? modelOptions[0]?.id ?? model)
  // 默认 provider 已「测试连接」发现模型时，用其模型列表 + 推理档位替代 env 单模型。
  const defaultProvider = providers.find((item) => item.is_default) ?? providers[0]
  // §review 双 provider（2026-09-03）：会话级主循环 provider + 独立 review provider/model。
  // 不再 activateProvider（设备级 active provider），改为本会话记住选择并在发送时下发。
  const [selectedProviderId, setSelectedProviderId] = useState<string | undefined>(initialSettings.providerId)
  const activeProviderId = selectedProviderId ?? defaultProvider?.provider_id
  const [reviewProviderId, setReviewProviderId] = useState<string | null>(initialSettings.reviewProviderId ?? null)
  const [reviewModelId, setReviewModelId] = useState<string | null>(initialSettings.reviewModelId ?? null)
  // §review 推理等级（2026-09-03）：review 也可单独选推理档（默认 none）。
  const [reviewReasoningEffort, setReviewReasoningEffort] = useState<ReasoningEffort>(initialSettings.reviewReasoningEffort ?? 'none')
  const reviewProvider = providers.find((item) => item.provider_id === reviewProviderId)
  const reviewEfforts: ReasoningEffort[] = (reviewProvider?.reasoning_efforts?.length
    ? reviewProvider.reasoning_efforts
    : (['none'] as ReasoningEffort[]))
  const activeReviewEffort = reviewEfforts.includes(reviewReasoningEffort) ? reviewReasoningEffort : ('none' as ReasoningEffort)
  // 模型列表跟随会话选中 provider（activeProviderId），而非设备 default——
  // 选了非 default provider 的模型也能正确展示/校验。
  const activeProviderForModels = providers.find((item) => item.provider_id === activeProviderId)
  const effectiveModelOptions: ModelCapability[] = useMemo(() => {
    const modelIds = providerModelIds(activeProviderForModels)
    if (!modelIds.length) return modelOptions
    const efforts: ReasoningEffort[] = activeProviderForModels?.reasoning_efforts?.length
      ? activeProviderForModels.reasoning_efforts
      : ['none']
    return modelIds.map((id) => ({ id, display_name: id, reasoning_efforts: efforts }))
  }, [activeProviderForModels, modelOptions])
  const activeModel = useMemo(
    () => effectiveModelOptions.find((item) => item.id === modelId) ?? effectiveModelOptions[0],
    [modelId, effectiveModelOptions],
  )
  const efforts: ReasoningEffort[] = activeModel?.reasoning_efforts?.length
    ? activeModel.reasoning_efforts
    : ['none']
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>(initialSettings.reasoningEffort ?? readPersistedEffort(modelId, efforts))
  const activeReasoningEffort = efforts.includes(reasoningEffort) ? reasoningEffort : efforts[0]
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(initialSettings.permissionMode ?? readPersistedPermissionMode())

  // 下拉面板开关 + 当前层级。
  const [panelOpen, setPanelOpen] = useState(false)
  const [panelView, setPanelView] = useState<PanelView>('root')
  // §审批独立（2026-09-03）：审批模式从模型下拉拆出，成为独立控件。
  const [approvalOpen, setApprovalOpen] = useState(false)
  // §review 双 provider（2026-09-03）：review 模型下拉开关。
  const [reviewOpen, setReviewOpen] = useState(false)

  // 供应商分组模型列表：每个有模型的供应商一组，用于「模型」展开页分组展示。
  const modelGroups: ModelGroup[] = useMemo(() => {
    const withModels = providers.filter((p) => providerModelIds(p).length)
    if (withModels.length) {
      return withModels.map((p) => ({
        key: p.provider_id,
        label: p.name,
        options: modelOptionsFor(p, modelOptions),
      }))
    }
    // 无供应商带模型时，回退到会话的 env 模型列表。
    return [{ key: 'default', label: '', options: modelOptionsFor(undefined, modelOptions) }]
  }, [providers, modelOptions])

  const handleSend = () => {
    if (!value.trim() || disabled) return
    onSend(value, activeModel?.id ?? model, activeReasoningEffort, permissionMode, activeProviderId, reviewProviderId ?? undefined, reviewModelId ?? undefined, activeReviewEffort)
    setValue('')
  }

  const persist = (patch: Partial<SessionSettings>) => saveSessionSettings(sessionId, patch)

  const handleSelectModel = async (option: ModelOption) => {
    // §review 双 provider（2026-09-03）：session 级——只记住选择，不再激活设备级 provider。
    setSelectedProviderId(option.providerId)
    setModelId(option.id)
    const nextEfforts = option.reasoning_efforts?.length ? option.reasoning_efforts : (['none'] as ReasoningEffort[])
    setReasoningEffort(readPersistedEffort(option.id, nextEfforts))
    persist({ providerId: option.providerId, modelId: option.id, reasoningEffort: readPersistedEffort(option.id, nextEfforts) })
    setPanelView('root')
    setPanelOpen(false)
  }

  const handleSelectEffort = (effort: ReasoningEffort) => {
    setReasoningEffort(effort)
    writePersistedEffort(activeModel?.id ?? model, effort)
    persist({ reasoningEffort: effort })
    setPanelView('root')
    setPanelOpen(false)
  }

  const handleSelectPermission = (mode: PermissionMode) => {
    if (mode === 'bypass') {
      Modal.confirm({
        title: '确认免审批？',
        content: '免审批模式下，所有危险工具（写文件 / Shell）将不再逐次请求批准。',
        okText: '确认免审批',
        okButtonProps: { danger: true },
        cancelText: '取消',
        onOk: () => {
          setPermissionMode('bypass')
          persist({ permissionMode: 'bypass' })
          setPanelView('root')
          setPanelOpen(false)
        },
      })
      return
    }
    setPermissionMode(mode)
    writePersistedPermissionMode(mode)
    persist({ permissionMode: mode })
    setPanelView('root')
    setPanelOpen(false)
  }

  const rootRows = [
    { view: 'model' as PanelView, label: '模型', value: activeModel?.display_name ?? model },
    { view: 'effort' as PanelView, label: '推理等级', value: effortLabels[activeReasoningEffort] },
  ]

  const panelContent = () => {
    if (panelView === 'model') {
      return (
        <div className="flex max-h-80 w-72 flex-col overflow-hidden">
          <PanelHeader label="模型" onBack={() => setPanelView('root')} />
          <div className="flex-1 overflow-y-auto p-1">
            {modelGroups.length === 0 ? (
              <div className="px-3 py-6 text-center text-xs text-stone-400">暂无可用模型</div>
            ) : (
              modelGroups.map((group) => (
                <div key={group.key}>
                  {group.label ? (
                    <div className="px-3 pb-1 pt-2 text-[11px] font-medium text-stone-400">{group.label}</div>
                  ) : null}
                  {group.options.map((opt) => {
                    // 勾选判定 = 同供应商 + 同模型，而不是只看模型 id。
                    // 不同供应商（如 ac gpt特惠 / 西牧 gpt）下可能有同名模型
                    // （如 gpt-5.6-sol），只比 id 会导致两个组都打勾。
                    // env 兜底组没有 providerId，此时按模型 id 匹配即可。
                    const selected = opt.providerId
                      ? opt.providerId === activeProviderId && opt.id === (activeModel?.id ?? model)
                      : opt.id === (activeModel?.id ?? model)
                    return (
                      <button
                        key={`${opt.providerId ?? 'env'}:${opt.id}`}
                        type="button"
                        onClick={() => handleSelectModel(opt)}
                        disabled={running || disabled}
                        className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 disabled:opacity-50"
                      >
                        <span className="min-w-0 flex-1 truncate">{opt.display_name}</span>
                        {selected ? <CheckOutlined className="shrink-0 text-blue-600" /> : null}
                      </button>
                    )
                  })}
                </div>
              ))
            )}
          </div>
        </div>
      )
    }

    if (panelView === 'effort') {
      return (
        <div className="flex w-72 flex-col">
          <PanelHeader label="推理等级" onBack={() => setPanelView('root')} />
          <div className="p-1">
            {efforts.map((effort) => (
              <button
                key={effort}
                type="button"
                onClick={() => handleSelectEffort(effort)}
                disabled={running || disabled}
                className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 disabled:opacity-50"
              >
                <span className="min-w-0 flex-1 truncate">{effortLabels[effort]}</span>
                {effort === activeReasoningEffort ? <CheckOutlined className="shrink-0 text-blue-600" /> : null}
              </button>
            ))}
          </div>
        </div>
      )
    }

    if (panelView === 'approval') {
      // 审批模式已拆成独立控件（approvalOpen Popover），此分支不再触发，保留防御。
      return null
    }

    // root：模型 / 推理等级 两个可展开行。
    return (
      <div className="flex w-72 flex-col">
        {rootRows.map((row, idx) => (
          <div key={row.view}>
            {idx > 0 ? <div className="mx-2 my-1 border-t border-stone-100" /> : null}
            <button
              type="button"
              onClick={() => setPanelView(row.view)}
              disabled={running || disabled}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-stone-100 disabled:opacity-50"
            >
              <span className="min-w-0 flex-1 truncate text-stone-400">{row.label}</span>
              <span className="max-w-40 min-w-0 shrink-0 truncate text-stone-700">{row.value}</span>
              <RightOutlined className="shrink-0 text-[11px] text-stone-300" />
            </button>
          </div>
        ))}
      </div>
    )
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
              <Popover
                trigger="click"
                placement="topLeft"
                open={approvalOpen}
                onOpenChange={setApprovalOpen}
                content={
                  <div className="flex w-56 flex-col p-1">
                    <div className="border-b border-stone-100 px-3 py-2 text-xs font-medium text-stone-500">审批模式</div>
                    {(Object.keys(permissionModeLabels) as PermissionMode[]).map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => {
                          handleSelectPermission(mode)
                          setApprovalOpen(false)
                        }}
                        disabled={running || disabled}
                        className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 disabled:opacity-50"
                      >
                        <span className="min-w-0 flex-1 truncate">{permissionModeLabels[mode]}</span>
                        {mode === permissionMode ? <CheckOutlined className="shrink-0 text-blue-600" /> : null}
                      </button>
                    ))}
                  </div>
                }
                arrow={false}
                styles={{ container: { padding: 0 } }}
              >
                <button
                  type="button"
                  disabled={running || disabled}
                  aria-label="审批模式"
                  aria-expanded={approvalOpen}
                  className="flex h-8 items-center gap-1.5 rounded-full border border-stone-200 bg-white px-3 text-xs text-stone-700 shadow-sm transition-colors hover:border-stone-300 hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="shrink-0 text-stone-400">审批</span>
                  <span className="max-w-32 min-w-0 truncate font-medium">{permissionModeLabels[permissionMode]}</span>
                </button>
              </Popover>
              <Popover
                trigger="click"
                placement="topLeft"
                open={panelOpen}
                onOpenChange={(open) => {
                  setPanelOpen(open)
                  if (!open) setPanelView('root')
                }}
                content={panelContent()}
                arrow={false}
                styles={{ container: { padding: 0 } }}
              >
                <button
                  type="button"
                  disabled={running || disabled}
                  aria-label="模型与推理设置"
                  aria-expanded={panelOpen}
                  className="flex h-8 items-center gap-1.5 rounded-full border border-stone-200 bg-white px-3 text-xs text-stone-700 shadow-sm transition-colors hover:border-stone-300 hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="max-w-44 min-w-0 truncate font-medium">{activeModel?.display_name ?? model}</span>
                  <span className="shrink-0 text-stone-400">{effortLabels[activeReasoningEffort]}</span>
                  <DownOutlined className={`shrink-0 text-[10px] text-stone-400 transition-transform ${panelOpen ? 'rotate-180' : ''}`} />
                </button>
              </Popover>
              <Popover
                trigger="click"
                placement="topLeft"
                open={reviewOpen}
                onOpenChange={setReviewOpen}
                content={
                  <div className="flex max-h-80 w-72 flex-col overflow-hidden">
                    <div className="border-b border-stone-100 px-3 py-2 text-xs font-medium text-stone-500">review模型</div>
                    <div className="flex-1 overflow-y-auto p-1">
                      <button
                        type="button"
                        onClick={() => {
                          setReviewProviderId(null)
                          setReviewModelId(null)
                          persist({ reviewProviderId: null, reviewModelId: null, reviewReasoningEffort: 'none' })
                          setReviewOpen(false)
                        }}
                        disabled={running || disabled}
                        className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 disabled:opacity-50"
                      >
                        <span className="min-w-0 flex-1 truncate">跟随主循环</span>
                        {!reviewProviderId ? <CheckOutlined className="shrink-0 text-blue-600" /> : null}
                      </button>
                      {modelGroups.map((group) => (
                        <div key={group.key}>
                          {group.label ? <div className="px-3 pb-1 pt-2 text-[11px] font-medium text-stone-400">{group.label}</div> : null}
                          {group.options.map((opt) => {
                            const selected = reviewProviderId === opt.providerId && reviewModelId === opt.id
                            return (
                              <button
                                key={`review:${opt.providerId ?? 'env'}:${opt.id}`}
                                type="button"
                                onClick={() => {
                                  setReviewProviderId(opt.providerId ?? null)
                                  setReviewModelId(opt.id)
                                  persist({ reviewProviderId: opt.providerId ?? null, reviewModelId: opt.id })
                                  setReviewOpen(false)
                                }}
                                disabled={running || disabled}
                                className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 disabled:opacity-50"
                              >
                                <span className="min-w-0 flex-1 truncate">{opt.display_name}</span>
                                {selected ? <CheckOutlined className="shrink-0 text-blue-600" /> : null}
                              </button>
                            )
                          })}
                        </div>
                      ))}
                      {reviewProviderId ? (
                        <div className="mt-1 border-t border-stone-100">
                          <div className="px-3 pt-2 pb-1 text-[11px] font-medium text-stone-400">推理等级</div>
                          {reviewEfforts.map((effort) => (
                            <button
                              key={`review-effort:${effort}`}
                              type="button"
                              onClick={() => {
                                setReviewReasoningEffort(effort)
                                persist({ reviewReasoningEffort: effort })
                                setReviewOpen(false)
                              }}
                              disabled={running || disabled}
                              className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-stone-100 disabled:opacity-50"
                            >
                              <span className="min-w-0 flex-1 truncate">{effortLabels[effort]}</span>
                              {effort === activeReviewEffort ? <CheckOutlined className="shrink-0 text-blue-600" /> : null}
                            </button>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  </div>
                }
                arrow={false}
                styles={{ container: { padding: 0 } }}
              >
                <button
                  type="button"
                  disabled={running || disabled}
                  aria-label="review模型"
                  aria-expanded={reviewOpen}
                  className="flex h-8 items-center gap-1.5 rounded-full border border-stone-200 bg-white px-3 text-xs text-stone-700 shadow-sm transition-colors hover:border-stone-300 hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="shrink-0 text-stone-400">review</span>
                  <span className="max-w-32 min-w-0 truncate font-medium">{reviewModelId ?? '跟随主循环'}</span>
                  <span className="shrink-0 text-stone-400">{effortLabels[activeReviewEffort]}</span>
                  <DownOutlined className={`shrink-0 text-[10px] text-stone-400 transition-transform ${reviewOpen ? 'rotate-180' : ''}`} />
                </button>
              </Popover>
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

/** 展开子页的顶栏：返回按钮 + 标题。 */
function PanelHeader({ label, onBack }: { label: string; onBack: () => void }) {
  return (
    <div className="flex items-center gap-2 border-b border-stone-100 px-2 py-2">
      <button
        type="button"
        onClick={onBack}
        aria-label="返回"
        className="flex h-6 w-6 items-center justify-center rounded-md text-stone-400 hover:bg-stone-100 hover:text-stone-600"
      >
        <RightOutlined className="rotate-180 text-xs" />
      </button>
      <span className="text-xs font-medium text-stone-500">{label}</span>
    </div>
  )
}

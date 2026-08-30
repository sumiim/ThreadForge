import { useEffect, useState } from 'react'
import { Button, Drawer, Form, Input, InputNumber, Modal, Select, Tag, message } from 'antd'
import { ApiOutlined, PlusOutlined } from '@ant-design/icons'
import type { Provider, ProviderProtocol } from '../../api/types'
import {
  activateProvider,
  ApiError,
  configureProvider,
  createProvider,
  deleteProvider,
  listProviders,
  testProvider,
  updateProvider,
} from '../../api/client'

const PROTOCOL_LABELS: Record<ProviderProtocol, string> = {
  openai_compatible: 'OpenAI 兼容',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  ollama: 'Ollama',
}

const REASONING_EFFORT_OPTIONS = [
  { value: 'none', label: 'None' },
  { value: 'minimal', label: 'Minimal' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra High' },
  { value: 'max', label: 'Max' },
]

/** 把 ApiError / worker 稳定错误码映射成可读原因，供提示展示。 */
function providerErrorCode(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.details?.reason as string | undefined) ?? err.details?.error ?? ''
    const code = err.code
    if (code) return [code, detail].filter(Boolean).join(' · ')
  }
  return (err as { message?: string })?.message ?? ''
}

interface FormValues {
  name: string
  protocol: ProviderProtocol
  base_url: string
  model?: string
  reasoning_efforts?: string[]
  timeout?: number
  concurrency?: number
  api_key?: string
}

export default function ProviderView({ deviceId }: { deviceId?: string }) {
  const [providers, setProviders] = useState<Provider[]>([])
  const [loading, setLoading] = useState(true)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Provider | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<FormValues>()

  const load = async () => {
    setLoading(true)
    try {
      const result = await listProviders()
      setProviders(deviceId ? result.providers.filter((item) => item.device_id === deviceId) : result.providers)
    } catch {
      message.error('加载供应商列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    listProviders()
      .then((result) => {
        if (!cancelled) setProviders(deviceId ? result.providers.filter((item) => item.device_id === deviceId) : result.providers)
      })
      .catch(() => {
        if (!cancelled) message.error('加载供应商列表失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [deviceId])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  const openEdit = (p: Provider) => {
    setEditing(p)
    form.setFieldsValue({
      name: p.name,
      protocol: p.protocol,
      base_url: p.base_url,
      model: p.model,
      reasoning_efforts: p.reasoning_efforts?.length ? p.reasoning_efforts : ['none'],
      timeout: p.timeout,
      concurrency: p.concurrency,
    })
    setModalOpen(true)
  }

  const submit = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      let providerId: string
      if (editing) {
        const updated = await updateProvider(editing.provider_id, values)
        providerId = updated.provider_id
        message.success('已保存')
      } else {
        // Provider 按设备绑定：创建时必须带当前 Worker 的 device_id，否则任务按
        // device_id 查默认 Provider 会匹配不到，静默回退到旧 .env。
        if (!deviceId) {
          message.error('请先选择本地设备/工作区再创建供应商')
          return
        }
        const created = await createProvider({ ...values, device_id: deviceId })
        providerId = created.provider_id
        message.success('已创建')
      }
      // 供应商配置推送到 Worker 本地（中央不落）。编辑时 api_key 留空表示保留 Worker 已保存的 Key。
      if (deviceId) {
        try {
          await configureProvider(providerId, {
            device_id: deviceId,
            base_url: values.base_url,
            api_key: values.api_key?.trim() ?? '',
            model: values.model ?? '',
            protocol: values.protocol,
            reasoning_efforts: values.reasoning_efforts ?? ['none'],
          })
        } catch (err) {
          const reason = providerErrorCode(err)
          message.warning(reason ? `供应商已保存，但 API Key 未能推送到 Worker：${reason}` : '供应商已保存，但 API Key 未能推送到 Worker，请稍后重试')
        }
      }
      setModalOpen(false)
      void load()
    } catch {
      message.error('保存失败')
    } finally {
      setSaving(false)
    }
  }

  const onActivate = async (p: Provider) => {
    try {
      await activateProvider(p.provider_id, deviceId)
      message.success(`已切换默认供应商为 ${p.name}`)
      void load()
    } catch {
      message.error('切换失败')
    }
  }

  const onDelete = (p: Provider) => {
    Modal.confirm({
      title: `删除供应商「${p.name}」？`,
      content: '删除后不可恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteProvider(p.provider_id)
          message.success('已删除')
          void load()
        } catch {
          message.error('删除失败')
        }
      },
    })
  }

  const onTest = async (p: Provider) => {
    if (!deviceId) {
      message.warning('请先选择本地设备/工作区')
      return
    }
    try {
      const result = await testProvider(p.provider_id, deviceId)
      message.success(`连接成功，发现 ${result.models.length} 个模型`)
      void load()
    } catch (err) {
      const reason = providerErrorCode(err)
      message.error(reason ? `连接测试失败：${reason}` : '连接测试失败，请检查 Base URL / API Key')
    }
  }

  return (
    <div className="h-full overflow-y-auto px-6 py-8 lg:px-10">
      <div className="mx-auto max-w-4xl">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
              <ApiOutlined />
            </span>
            <h1 className="text-lg font-semibold tracking-tight text-stone-900">供应商</h1>
          </div>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增供应商
          </Button>
        </div>
        <p className="mt-2 text-sm text-stone-500">
          集中管理模型供应商与 API 端点。密钥只存本地 Worker，中央不回显。
        </p>

        <div className="mt-6">
          {loading ? (
            <div className="py-12 text-center text-sm text-stone-400">加载中…</div>
          ) : providers.length === 0 ? (
            <div className="py-12 text-center text-sm text-stone-400">暂无供应商，点击「新增供应商」创建。</div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {providers.map((p) => (
                <div key={p.provider_id} className="rounded-xl border border-stone-200 bg-white p-4 shadow-sm">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-blue-50 text-xs font-medium text-blue-600">
                        { (PROTOCOL_LABELS[p.protocol] ?? 'P').slice(0, 1)}
                      </span>
                      <span className="truncate font-medium text-stone-800">{p.name}</span>
                      {p.is_default ? <Tag color="blue">默认</Tag> : null}
                    </div>
                    <Tag color={p.state === 'active' ? 'green' : 'default'}>{p.state}</Tag>
                  </div>
                  <div className="mt-3 space-y-1 text-xs text-stone-500">
                    <div className="truncate font-mono">{p.base_url}</div>
                    <div>
                      {PROTOCOL_LABELS[p.protocol] ?? p.protocol} · 模型 {p.model || '—'}
                      {p.models.length ? `（+${p.models.length}）` : ''}
                    </div>
                    <div>Reasoning efforts {(p.reasoning_efforts ?? []).join(' / ') || '—'}</div>
                    {p.last_error ? (
                      <div className="truncate text-red-500">{p.last_error}</div>
                    ) : p.last_test_at ? (
                      <div>上次测试 {p.last_test_at.slice(0, 16).replace('T', ' ')}</div>
                    ) : null}
                  </div>
                  <div className="mt-3 flex items-center gap-1 border-t border-stone-100 pt-2">
                    <Button type="link" size="small" onClick={() => onTest(p)}>测试连接</Button>
                    {!p.is_default ? (
                      <Button type="link" size="small" onClick={() => onActivate(p)}>设为默认</Button>
                    ) : null}
                    <Button type="link" size="small" onClick={() => openEdit(p)}>编辑</Button>
                    <Button type="link" size="small" danger onClick={() => onDelete(p)}>删除</Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <Drawer
        title={editing ? '编辑供应商' : '新增供应商'}
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        width={420}
        extra={
          <Button type="primary" loading={saving} onClick={submit}>
            {editing ? '保存' : '创建'}
          </Button>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如 DeepSeek" />
          </Form.Item>
          <Form.Item name="protocol" label="协议" rules={[{ required: true }]}>
            <Select
              options={(Object.keys(PROTOCOL_LABELS) as ProviderProtocol[]).map((value) => ({
                value,
                label: PROTOCOL_LABELS[value],
              }))}
            />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true, message: '请输入 Base URL' }]}>
            <Input placeholder="https://api.example.com/v1" />
          </Form.Item>
          <Form.Item name="model" label="默认模型">
            <Input placeholder="如 deepseek-chat" />
          </Form.Item>
          <Form.Item name="reasoning_efforts" label="Reasoning efforts" initialValue={['none']}>
            <Select
              mode="multiple"
              options={REASONING_EFFORT_OPTIONS}
              placeholder="Select the reasoning efforts this provider supports"
            />
          </Form.Item>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="timeout" label="超时（秒）">
              <InputNumber min={5} max={600} className="w-full" />
            </Form.Item>
            <Form.Item name="concurrency" label="并发">
              <InputNumber min={1} max={16} className="w-full" />
            </Form.Item>
          </div>
          <Form.Item name="api_key" label={editing ? 'API Key（留空保留 Worker 本地密钥）' : 'API Key'}>
            <Input.Password placeholder="只存本地 Worker，不回显" />
          </Form.Item>
        </Form>
      </Drawer>
    </div>
  )
}

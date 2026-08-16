import { useEffect, useState } from 'react'
import { Button, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, message } from 'antd'
import { ApiOutlined, PlusOutlined } from '@ant-design/icons'
import type { Provider, ProviderProtocol } from '../../api/types'
import {
  activateProvider,
  configureProvider,
  createProvider,
  deleteProvider,
  listProviders,
  updateProvider,
} from '../../api/client'

const PROTOCOL_LABELS: Record<ProviderProtocol, string> = {
  openai_compatible: 'OpenAI 兼容',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  ollama: 'Ollama',
}

interface FormValues {
  name: string
  protocol: ProviderProtocol
  base_url: string
  model?: string
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
      setProviders(result.providers)
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
        if (!cancelled) setProviders(result.providers)
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
  }, [])

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
        const created = await createProvider(values)
        providerId = created.provider_id
        message.success('已创建')
      }
      // api_key 只推送到 Worker 本地（中央不落）；device 未定或未填 key 时跳过。
      if (deviceId && values.api_key?.trim()) {
        try {
          await configureProvider(providerId, {
            device_id: deviceId,
            base_url: values.base_url,
            api_key: values.api_key,
            model: values.model ?? '',
            protocol: values.protocol,
          })
        } catch {
          message.warning('供应商已保存，但 API Key 未能推送到 Worker，请稍后重试')
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
      await activateProvider(p.provider_id)
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

  const columns = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (value: string, record: Provider) => (
        <Space size={6}>
          <span className="font-medium text-stone-800">{value}</span>
          {record.is_default ? <Tag color="blue">默认</Tag> : null}
        </Space>
      ),
    },
    {
      title: '协议',
      dataIndex: 'protocol',
      key: 'protocol',
      render: (value: ProviderProtocol) => PROTOCOL_LABELS[value] ?? value,
    },
    { title: 'Base URL', dataIndex: 'base_url', key: 'base_url', ellipsis: true },
    { title: '模型', dataIndex: 'model', key: 'model', render: (v: string) => v || '—' },
    {
      title: '状态',
      dataIndex: 'state',
      key: 'state',
      render: (value: string) =>
        value === 'active' ? <Tag color="green">active</Tag> : <Tag>{value}</Tag>,
    },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, record: Provider) => (
        <Space size={4}>
          {!record.is_default ? (
            <Button type="link" size="small" onClick={() => onActivate(record)}>
              设为默认
            </Button>
          ) : null}
          <Button type="link" size="small" onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Button type="link" size="small" danger onClick={() => onDelete(record)}>
            删除
          </Button>
        </Space>
      ),
    },
  ]

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
          <Table<Provider>
            rowKey="provider_id"
            columns={columns}
            dataSource={providers}
            loading={loading}
            pagination={false}
            size="middle"
          />
        </div>
      </div>

      <Modal
        title={editing ? '编辑供应商' : '新增供应商'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={submit}
        confirmLoading={saving}
        okText={editing ? '保存' : '创建'}
        destroyOnClose
      >
        <Form form={form} layout="vertical" className="mt-4">
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
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="timeout" label="超时（秒）">
              <InputNumber min={5} max={600} className="w-full" />
            </Form.Item>
            <Form.Item name="concurrency" label="并发">
              <InputNumber min={1} max={16} className="w-full" />
            </Form.Item>
          </div>
          {!editing ? (
            <Form.Item name="api_key" label="API Key">
              <Input.Password placeholder="只存本地 Worker，不回显" />
            </Form.Item>
          ) : null}
        </Form>
      </Modal>
    </div>
  )
}

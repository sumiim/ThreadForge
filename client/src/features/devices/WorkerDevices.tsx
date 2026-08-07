import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Form, Input, Modal, Popconfirm, Progress, Spin, Tag, Tooltip, Typography } from 'antd'
import {
  DeleteOutlined,
  FolderOpenOutlined,
  LaptopOutlined,
  LinkOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import {
  configureWorkerModel,
  createPairingCode,
  friendlyMessage,
  getLatestWorkerRelease,
  getWorkspaceSelection,
  listDevices,
  requestWorkspaceSelection,
  revokeDevice,
} from '../../api/client'
import type { Device, WorkerReleaseManifest } from '../../api/types'
import { workerIsReady } from './worker-version'

const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds))

const selectionErrors: Record<string, string> = {
  selection_busy: '上一次目录选择窗口仍在等待处理；请切回桌面完成操作，窗口超时后会自动关闭',
  selection_expired: '目录选择请求已过期',
  selection_failed: '本机目录选择失败，请重新下载安装最新 Worker',
  worker_disconnected: 'Worker 已断开连接',
  worker_reconnected: 'Worker 重连后请求已失效，请重新操作',
  worker_revoked: '设备已被撤销',
}

export default function WorkerDevices() {
  const [devices, setDevices] = useState<Device[]>([])
  const [release, setRelease] = useState<WorkerReleaseManifest | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pairing, setPairing] = useState<{ code: string; expires_in_seconds: number } | null>(null)
  const [selectionDeviceId, setSelectionDeviceId] = useState('')
  const [modelDevice, setModelDevice] = useState<Device | null>(null)
  const [modelSaving, setModelSaving] = useState(false)
  const [modelForm] = Form.useForm<{ base_url: string; api_key: string; model: string }>()
  const operationVersion = useRef(0)
  const workerServer = window.threadforge?.apiBaseUrl ?? window.location.origin

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [deviceResponse, manifest] = await Promise.all([listDevices(), getLatestWorkerRelease()])
      setDevices(deviceResponse.items)
      setRelease(manifest)
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const [response, manifest] = await Promise.all([listDevices(), getLatestWorkerRelease()])
        if (!active) return
        setDevices(response.items)
        setRelease(manifest)
        setError('')
      } catch (cause: unknown) {
        if (active) setError(friendlyMessage(cause))
      } finally {
        if (active) setLoading(false)
      }
    }
    void load()
    // The Worker can restart itself after an update. Keep the settings view
    // in sync without requiring the user to close and reopen the drawer.
    const refreshTimer = window.setInterval(() => {
      if (!active) return
      void load()
    }, 5_000)
    return () => {
      active = false
      window.clearInterval(refreshTimer)
      operationVersion.current += 1
    }
  }, [])

  const createCode = async () => {
    try {
      setError('')
      setPairing(await createPairingCode())
    } catch (cause) {
      setError(friendlyMessage(cause))
    }
  }

  const revoke = async (deviceId: string) => {
    try {
      await revokeDevice(deviceId)
      await refresh()
    } catch (cause) {
      setError(friendlyMessage(cause))
    }
  }

  const startLocalService = async () => {
    setNotice('已请求系统启动 ThreadForge Worker')
    window.location.href = 'threadforge://worker/start'
    await delay(1500)
    await refresh()
  }

  const uninstallLocalWorker = async () => {
    operationVersion.current += 1
    setError('')
    setNotice('已请求系统打开 ThreadForge Worker 卸载程序')
    window.location.href = 'threadforge://worker/uninstall'
    await delay(2_000)
    await refresh()
  }

  const selectWorkspace = async (deviceId: string) => {
    const version = operationVersion.current + 1
    operationVersion.current = version
    setSelectionDeviceId(deviceId)
    setError('')
    setNotice('请在 Worker 所在电脑完成目录选择')
    try {
      let request = await requestWorkspaceSelection(deviceId)
      const deadline = new Date(request.expires_at).getTime() + 2000
      while (request.status === 'pending' && Date.now() < deadline) {
        await delay(1000)
        if (operationVersion.current !== version) return
        request = await getWorkspaceSelection(deviceId, request.request_id)
      }
      if (operationVersion.current !== version) return
      if (request.status === 'completed') {
        setNotice('本地工作区已添加')
        await refresh()
      } else if (request.status === 'cancelled') {
        setNotice('已取消目录选择')
      } else {
        setNotice('')
        setError(selectionErrors[request.error ?? ''] ?? '目录选择请求未完成')
      }
    } catch (cause) {
      if (operationVersion.current === version) setError(friendlyMessage(cause))
    } finally {
      if (operationVersion.current === version) setSelectionDeviceId('')
    }
  }

  const openModelConfig = (device: Device) => {
    setModelDevice(device)
    modelForm.setFieldsValue({
      base_url: 'https://api.openai.com/v1',
      api_key: '',
      model: device.model || 'gpt-5.4',
    })
  }

  const saveModelConfig = async () => {
    if (!modelDevice) return
    try {
      const values = await modelForm.validateFields()
      setModelSaving(true)
      setError('')
      await configureWorkerModel(modelDevice.device_id, values)
      setNotice('模型配置已安全写入 Worker 本地')
      setModelDevice(null)
      modelForm.resetFields()
      await refresh()
    } catch (cause) {
      if (cause instanceof Error) setError(friendlyMessage(cause))
    } finally {
      setModelSaving(false)
    }
  }

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <div className="text-sm font-medium text-stone-800">本地 Worker</div>
        <Button type="text" size="small" icon={<ReloadOutlined />} onClick={() => void refresh()} />
      </div>
      <p className="mb-3 text-xs text-stone-500">
        Worker 在本机执行 Agent、文件、Git 与 Shell；会话正文和模型密钥仅保存在本机。
      </p>
      {error ? <Alert className="mb-3" type="error" showIcon message={error} /> : null}
      {notice ? <Alert className="mb-3" type="info" showIcon message={notice} /> : null}
      {loading ? (
        <Spin size="small" />
      ) : devices.length === 0 ? (
        <div className="rounded-xl border border-dashed border-stone-300 p-3 text-xs text-stone-500">
          尚未绑定设备。
        </div>
      ) : (
        <div className="space-y-2">
          {devices.map((device) => {
            const canSelectWorkspace =
              workerIsReady(device, release) && (device.capabilities ?? []).includes('workspace_selection')
            const updateStatus = device.update_status
            const updateProgress = updateStatus?.total_bytes
              ? Math.min(100, Math.round((updateStatus.downloaded_bytes / updateStatus.total_bytes) * 100))
              : null
            return (
              <div key={device.device_id} className="rounded-xl border border-stone-200 p-3">
                <div className="flex items-center gap-2">
                  <LaptopOutlined className="text-stone-500" />
                  <span className="min-w-0 flex-1 truncate text-sm font-medium text-stone-800">
                    {device.name}
                  </span>
                  <Tag color={device.online ? 'green' : 'default'}>{device.online ? '在线' : '离线'}</Tag>
                  <Popconfirm
                    title="撤销这台设备？"
                    description="设备将立即断开，正在执行的任务会失败。"
                    okText="撤销"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => void revoke(device.device_id)}
                  >
                    <Button type="text" danger size="small" icon={<DeleteOutlined />} />
                  </Popconfirm>
                </div>
                <div className="mt-2 text-[11px] text-stone-500">
                  {device.platform || 'unknown'} / {device.architecture || 'unknown'} · Worker{' '}
                  {device.version || '旧版'} · {device.workspaces.length} 个工作区 ·{' '}
                  {device.model_configured ? device.model : '模型未配置'}
                </div>
                {updateStatus?.status === 'downloading' && updateProgress !== null ? (
                  <div className="mt-2">
                    <div className="mb-1 text-[11px] text-stone-500">
                      正在自动更新至 {updateStatus.target_version}，支持断点续传
                    </div>
                    <Progress percent={updateProgress} size="small" />
                  </div>
                ) : null}
                {updateStatus?.status === 'installing' ? (
                  <Alert className="mt-2" type="info" showIcon message="正在安装更新，Worker 将自动重启" />
                ) : null}
                {updateStatus?.status === 'failed' ? (
                  <Alert className="mt-2" type="warning" showIcon message="自动更新失败，30 秒后自动续传重试" />
                ) : null}
                {device.workspaces.length > 0 ? (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {device.workspaces.map((workspace) => (
                      <Tag key={workspace.workspace_id}>{workspace.name}</Tag>
                    ))}
                  </div>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  {device.online && (device.capabilities ?? []).includes('model_configuration') ? (
                    <Tooltip title="配置模型接口">
                      <Button
                        size="small"
                        aria-label="配置模型接口"
                        icon={<SettingOutlined />}
                        onClick={() => openModelConfig(device)}
                      />
                    </Tooltip>
                  ) : null}
                  {device.online && canSelectWorkspace ? (
                    <Button
                      size="small"
                      icon={<FolderOpenOutlined />}
                      loading={selectionDeviceId === device.device_id}
                      disabled={Boolean(selectionDeviceId && selectionDeviceId !== device.device_id)}
                      onClick={() => void selectWorkspace(device.device_id)}
                    >
                      添加本地目录
                    </Button>
                  ) : null}
                  {device.online && (!canSelectWorkspace || !device.compatible) ? (
                    <Tag color="warning">需要更新 Worker</Tag>
                  ) : null}
                  {!device.online ? (
                    <Button size="small" icon={<PoweroffOutlined />} onClick={() => void startLocalService()}>
                      启动本机 Worker
                    </Button>
                  ) : null}
                </div>
              </div>
            )
          })}
        </div>
      )}
      <Button className="mt-3" block icon={<LinkOutlined />} onClick={() => void createCode()}>
        绑定新设备
      </Button>
      <Popconfirm
        title="卸载本机 Worker？"
        description="仅移除这台电脑上的 Worker 程序和自启动项；本地会话、工作区与模型配置会保留。"
        okText="打开卸载程序"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        onConfirm={() => void uninstallLocalWorker()}
      >
        <Button className="mt-2" danger block icon={<DeleteOutlined />}>
          卸载本机 Worker
        </Button>
      </Popconfirm>
      {pairing ? (
        <div className="mt-3 rounded-xl bg-stone-100 p-3">
          <div className="text-xs text-stone-500">
            一次性配对码（{Math.floor(pairing.expires_in_seconds / 60)} 分钟）
          </div>
          <Typography.Text copyable className="mt-1 block font-mono text-lg tracking-wider">
            {pairing.code}
          </Typography.Text>
          <div className="mt-2 text-[11px] text-stone-500">
            在本机执行：
            <code className="break-all">
              threadforge-worker pair --server {workerServer} --code {pairing.code}
            </code>
          </div>
        </div>
      ) : null}
      <Modal
        title="本地模型配置"
        open={Boolean(modelDevice)}
        okText="保存到本机"
        cancelText="取消"
        confirmLoading={modelSaving}
        onOk={() => void saveModelConfig()}
        onCancel={() => {
          setModelDevice(null)
          modelForm.resetFields()
        }}
      >
        <Form form={modelForm} layout="vertical" requiredMark={false}>
          <Form.Item
            label="API 地址"
            name="base_url"
            rules={[{ required: true, type: 'url', message: '请输入有效的 HTTP(S) 地址' }]}
          >
            <Input autoComplete="url" placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item label="模型" name="model" rules={[{ required: true, message: '请输入模型名称' }]}>
            <Input autoComplete="off" placeholder="gpt-5.4" />
          </Form.Item>
          <Form.Item label="API Key" name="api_key" rules={[{ required: true, message: '请输入 API Key' }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

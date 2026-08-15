import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Dropdown, Form, Input, Modal, Progress, Select, Spin, Tag, Tooltip, Typography } from 'antd'
import {
  DeleteOutlined,
  FolderOpenOutlined,
  LaptopOutlined,
  LinkOutlined,
  MoreOutlined,
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
  uninstallWorker,
  updateWorker,
} from '../../api/client'
import type { Device, WorkerReleaseManifest } from '../../api/types'
import { getWorkerDeviceActionState } from './worker-actions'
import { workerIsReady } from './worker-version'

const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds))

const formatBytes = (value: number) => `${(value / (1024 * 1024)).toFixed(1)} MiB`

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
  const [revokingDeviceId, setRevokingDeviceId] = useState('')
  const [uninstallingDeviceId, setUninstallingDeviceId] = useState('')
  const [updatingDeviceId, setUpdatingDeviceId] = useState('')
  const [statusClock, setStatusClock] = useState(0)
  const [modelForm] = Form.useForm<{ base_url: string; api_key: string; model: string; model_provider: string }>()
  const [modal, modalContextHolder] = Modal.useModal()
  const operationVersion = useRef(0)
  const workerServer = window.threadforge?.apiBaseUrl ?? window.location.origin

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [deviceResponse, manifest] = await Promise.all([listDevices(), getLatestWorkerRelease()])
      setDevices(deviceResponse.items)
      setRelease(manifest)
      setStatusClock(Date.now())
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
        setStatusClock(Date.now())
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

  const revoke = async (device: Device) => {
    try {
      setError('')
      setRevokingDeviceId(device.device_id)
      await revokeDevice(device.device_id)
      setNotice(`${device.name} 已解绑`)
      await refresh()
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setRevokingDeviceId('')
    }
  }

  const startLocalService = async () => {
    setNotice('已请求系统启动 ThreadForge Worker')
    window.location.assign('threadforge://worker/start')
    await delay(1500)
    await refresh()
  }

  const uninstall = async (device: Device) => {
    try {
      setError('')
      setUninstallingDeviceId(device.device_id)
      const remoteUninstall = device.online && (device.capabilities ?? []).includes('worker_uninstall')
      if (remoteUninstall) {
        await uninstallWorker(device.device_id)
        setNotice(`已请求 ${device.name} 启动卸载程序；设备稍后会离线，但仍会保留绑定记录和本地数据。`)
      } else {
        // An offline/old Worker cannot receive the authenticated command. The
        // protocol handler is local to the computer where this page is open,
        // so it remains available as a recovery path for a broken install.
        setNotice(
          `正在尝试在当前电脑启动 ${device.name} 的卸载程序；如果该设备不在当前电脑，请在目标电脑打开 ThreadForge 后再操作。`,
        )
        window.location.assign('threadforge://worker/uninstall')
      }
      await delay(2_000)
      await refresh()
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setUninstallingDeviceId('')
    }
  }

  const manuallyUpdate = async (device: Device) => {
    try {
      setError('')
      setUpdatingDeviceId(device.device_id)
      await updateWorker(device.device_id)
      setNotice(`已请求 ${device.name} 检查并更新到最新版本`)
      await delay(2_000)
      await refresh()
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setUpdatingDeviceId('')
    }
  }

  const confirmRevoke = (device: Device) => {
    modal.confirm({
      title: '解绑这台设备？',
      content: '设备将立即断开，正在执行的任务会失败。',
      okText: '解绑',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => revoke(device),
    })
  }

  const confirmUninstall = (device: Device, canRemoteUninstall: boolean) => {
    modal.confirm({
      title: `卸载 ${device.name} 上的 Worker？`,
      content: canRemoteUninstall
        ? '目标电脑会打开卸载程序并停止 Worker；本地会话、工作区授权、模型配置和设备绑定都会保留。'
        : '该 Worker 当前离线或版本过旧，将尝试在当前电脑打开本机卸载程序。远程设备请先在目标电脑启动 Worker。',
      okText: '卸载 Worker',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => uninstall(device),
    })
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
      model_provider: '',
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
      {modalContextHolder}
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
            const { canRemoteUninstall, pending: deviceActionPending, uninstallLabel } =
              getWorkerDeviceActionState(device, revokingDeviceId, uninstallingDeviceId)
            const updateStatus = device.update_status
            const updateProgress = updateStatus?.total_bytes
              ? Math.min(100, Math.round((updateStatus.downloaded_bytes / updateStatus.total_bytes) * 100))
              : null
            const updateSpeed = updateStatus?.bytes_per_second
              ? `${updateStatus.bytes_per_second >= 1024 * 1024
                  ? (updateStatus.bytes_per_second / (1024 * 1024)).toFixed(1) + ' MiB/s'
                  : Math.max(1, Math.round(updateStatus.bytes_per_second / 1024)) + ' KiB/s'}`
              : ''
            const updateProgressText = updateStatus?.total_bytes
              ? `${formatBytes(updateStatus.downloaded_bytes)} / ${formatBytes(updateStatus.total_bytes)}`
              : ''
            const updateStalled = Boolean(
              updateStatus &&
                ['downloading', 'retrying'].includes(updateStatus.status) &&
                updateStatus.updated_at &&
                statusClock - Date.parse(updateStatus.updated_at) > 45_000,
            )
            return (
              <div key={device.device_id} className="rounded-xl border border-stone-200 p-3">
                <div className="flex items-center gap-2">
                  <LaptopOutlined className="text-stone-500" />
                  <span className="min-w-0 flex-1 truncate text-sm font-medium text-stone-800">
                    {device.name}
                  </span>
                  <Tag color={device.online ? 'green' : 'default'}>{device.online ? '在线' : '离线'}</Tag>
                  <Dropdown
                    trigger={['click']}
                    placement="bottomRight"
                    disabled={deviceActionPending}
                    menu={{
                      items: [
                        {
                          key: 'revoke',
                          label: '解绑设备',
                          danger: true,
                          onClick: () => confirmRevoke(device),
                        },
                        {
                          key: 'uninstall',
                          icon: <DeleteOutlined />,
                          label: uninstallLabel,
                          danger: true,
                          onClick: () => confirmUninstall(device, canRemoteUninstall),
                        },
                      ],
                    }}
                  >
                    <Button
                      type="text"
                      size="small"
                      shape="circle"
                      aria-label={`${device.name} 的设备操作`}
                      icon={<MoreOutlined />}
                      loading={deviceActionPending}
                    />
                  </Dropdown>
                </div>
                <div className="mt-2 text-[11px] text-stone-500">
                  {device.platform || 'unknown'} / {device.architecture || 'unknown'} · Worker{' '}
                  {device.version || '旧版'} · {device.workspaces.length} 个工作区 ·{' '}
                  {device.model_configured ? device.model : '模型未配置'}
                </div>
                {updateStatus &&
                ['downloading', 'retrying'].includes(updateStatus.status) &&
                updateProgress !== null ? (
                  <div className="mt-2">
                    <div className="mb-1 text-[11px] text-stone-500">
                      {updateStatus.status === 'retrying' || updateStalled
                        ? `下载连接暂无进度，正在从 ${formatBytes(updateStatus.downloaded_bytes)} 继续${updateStatus.retry_count ? `（第 ${updateStatus.retry_count} 次重连）` : ''}`
                        : `正在自动更新至 ${updateStatus.target_version} · ${updateProgressText}${updateSpeed ? ` · ${updateSpeed}` : ''}`}
                    </div>
                    <Progress
                      percent={updateProgress}
                      size="small"
                      status={updateStatus.status === 'retrying' || updateStalled ? 'exception' : 'active'}
                    />
                  </div>
                ) : null}
                {updateStatus?.status === 'installing' ? (
                  <Alert className="mt-2" type="info" showIcon message="正在安装更新，Worker 将自动重启" />
                ) : null}
                {updateStatus?.status === 'auth_failed' ? (
                  <Alert
                    className="mt-2"
                    type="error"
                    showIcon
                    message="设备令牌失效，请重新配对"
                    description="该 Worker 的配对凭据已被服务器拒绝（401/403），自动更新已暂停。请解绑本设备后重新配对。"
                  />
                ) : null}
                {updateStatus?.status === 'failed' ? (
                  <Alert
                    className="mt-2"
                    type="warning"
                    showIcon
                    message="自动更新失败，30 秒后自动续传重试"
                    description={updateStatus.error || undefined}
                  />
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
                  {device.online && (device.capabilities ?? []).includes('auto_update') ? (
                    <Button
                      size="small"
                      icon={<ReloadOutlined />}
                      loading={updatingDeviceId === device.device_id}
                      disabled={['checking', 'downloading', 'retrying', 'installing'].includes(
                        device.update_status?.status ?? '',
                      )}
                      onClick={() => void manuallyUpdate(device)}
                    >
                      手动更新
                    </Button>
                  ) : null}
                  {device.online && (!canSelectWorkspace || !device.compatible) ? (
                    <Tag color="warning">需要更新 Worker</Tag>
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
      <Button
        className="mt-2"
        block
        icon={<PoweroffOutlined />}
        onClick={() => void startLocalService()}
      >
        启动这台电脑的 Worker
      </Button>
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
          <Form.Item label="接口协议" name="model_provider">
            <Select
              options={[
                { value: '', label: 'OpenAI Responses API（默认）' },
                { value: 'chat_completions', label: 'Chat Completions（SiliconFlow 等）' },
                { value: 'anthropic', label: 'Anthropic Messages API' },
                { value: 'openai', label: 'OpenAI（显式指定 Responses API）' },
              ]}
            />
          </Form.Item>
          <Form.Item label="API Key" name="api_key" rules={[{ required: true, message: '请输入 API Key' }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

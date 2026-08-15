import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Grid, Modal, Progress, Radio, Spin, Tag } from 'antd'
import {
  CheckCircleOutlined,
  DownloadOutlined,
  FolderOpenOutlined,
  LoadingOutlined,
  PoweroffOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  createPairingCode,
  downloadWorkerRelease,
  friendlyMessage,
  getLatestWorkerRelease,
  getWorkspaceSelection,
  listDevices,
  requestWorkspaceSelection,
  updateWorker,
} from '../../api/client'
import type { Device, Workspace, WorkspaceSelectionRequest, WorkerReleaseManifest } from '../../api/types'
import { workerIsReady, workerNeedsUpdate } from '../devices/worker-version'
import { workspaceKey } from './workspaceIdentity'

const delay = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds))

const selectionErrors: Record<string, string> = {
  native_directory_picker_failed: '本机无法打开目录选择窗口，请重新安装最新 Worker',
  native_directory_picker_unavailable: '本机缺少目录选择组件，请重新安装最新 Worker',
  selection_busy: '上一次目录选择窗口仍在等待处理；请切回桌面完成操作，窗口超时后会自动关闭',
  selection_expired: '目录选择请求已过期',
  selection_failed: '本机目录选择失败，请重新安装最新 Worker',
  workspace_path_unavailable: '所选目录不存在或当前用户无权访问，请重新选择',
  workspace_config_write_failed: 'Worker 暂时无法保存配置，请稍后重试；诊断日志保存在本机',
  workspace_registration_failed: '目录已选择，但 Worker 保存工作区失败',
  worker_disconnected: 'Worker 已断开连接',
  worker_reconnected: 'Worker 重连后请求已失效，请重新操作',
  worker_revoked: '设备已被撤销',
}

interface NewSessionModalProps {
  open: boolean
  intent?: 'session' | 'workspace' | 'host'
  workspaces: Workspace[]
  selected: Workspace | null
  preferredDeviceId?: string | null
  onSelect: (workspace: Workspace) => void
  onCreate: () => void
  onCancel: () => void
  onOpenSettings: () => void
  onWorkspacesChanged?: () => Promise<Workspace[]> | Workspace[]
}

// 新建会话同时负责：选择工作区、绑定多台 Worker、目录授权和首次安装。
export default function NewSessionModal({
  open,
  intent = 'session',
  workspaces,
  selected,
  preferredDeviceId = null,
  onSelect,
  onCreate,
  onCancel,
  onOpenSettings,
  onWorkspacesChanged,
}: NewSessionModalProps) {
  const selectable = workspaces.filter((w) => w.available)
  const value = selected && selectable.some((w) => workspaceKey(w) === workspaceKey(selected))
    ? workspaceKey(selected)
    : undefined
  const [devices, setDevices] = useState<Device[] | null>(null)
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null)
  const [release, setRelease] = useState<WorkerReleaseManifest | null>(null)
  const [releaseLoading, setReleaseLoading] = useState(true)
  const [refreshKey, setRefreshKey] = useState(0)
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null)
  const [downloaded, setDownloaded] = useState(false)
  const [error, setError] = useState('')
  const [deviceError, setDeviceError] = useState('')
  const [releaseError, setReleaseError] = useState('')
  const [downloadStatus, setDownloadStatus] = useState('')
  const [selecting, setSelecting] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [refreshingWorkspaces, setRefreshingWorkspaces] = useState(false)
  const [updatingDeviceId, setUpdatingDeviceId] = useState('')
  const screens = Grid.useBreakpoint()
  const isMobile = screens.md === false

  const manuallyUpdate = async (device: Device) => {
    try {
      setError('')
      setUpdatingDeviceId(device.device_id)
      await updateWorker(device.device_id)
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setUpdatingDeviceId('')
    }
  }

  const operationVersion = useRef(0)
  // An existing but offline Worker still has valid workspace choices. Keep
  // those entries visible so the user can see which device needs reconnecting;
  // only an empty catalog should enter the install/pairing flow.
  const noWorkspace = workspaces.length === 0
  const needsDeviceDiscovery = intent !== 'session' || noWorkspace
  const selectsNewWorkspace = intent === 'workspace' || (intent === 'session' && noWorkspace)
  const compatibleDevices = (devices ?? []).filter((device) => workerIsReady(device, release))
  const outdatedDevices = release
    ? (devices ?? []).filter((device) => workerNeedsUpdate(device, release))
    : []
  const activeUpdate = outdatedDevices.find((device) =>
    ['checking', 'downloading', 'installing'].includes(device.update_status?.status ?? ''),
  )
  const activeUpdateProgress = activeUpdate?.update_status?.total_bytes
    ? Math.min(
        100,
        Math.round(
          ((activeUpdate.update_status.downloaded_bytes ?? 0) /
            activeUpdate.update_status.total_bytes) *
            100,
        ),
      )
    : null
  const selectedDevice = compatibleDevices.find((device) => device.device_id === selectedDeviceId)
    ?? compatibleDevices.find((device) => device.device_id === preferredDeviceId)
    ?? compatibleDevices[0]
    ?? null
  const addWorkspaceTarget = selectable.find(
    (workspace) =>
      workspace.execution_environment === 'local_worker' &&
      workspace.device_id &&
      workspace.device_id === selected?.device_id,
  ) ?? selectable.find(
    (workspace) => workspace.execution_environment === 'local_worker' && workspace.device_id,
  ) ?? null

  useEffect(() => {
    if (!open || !needsDeviceDiscovery) return
    let active = true
    void listDevices()
      .then(({ items }) => {
        if (active) {
          setDevices(items)
          setDeviceError('')
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setDevices([])
          setDeviceError(friendlyMessage(cause))
        }
      })
    return () => {
      active = false
    }
  }, [needsDeviceDiscovery, open, refreshKey])

  useEffect(() => {
    if (!open || !needsDeviceDiscovery) return
    let active = true
    void getLatestWorkerRelease({ force: refreshKey > 0 })
      .then((manifest) => {
        if (active) {
          setRelease(manifest)
          setReleaseError('')
        }
      })
      .catch((cause: unknown) => {
        if (active) setReleaseError(friendlyMessage(cause))
      })
      .finally(() => {
        if (active) setReleaseLoading(false)
      })
    return () => {
      active = false
    }
  }, [needsDeviceDiscovery, open, refreshKey])

  useEffect(() => {
    if (!open || !needsDeviceDiscovery) return
    const timer = window.setInterval(() => {
      void listDevices()
        .then(({ items }) => {
          setDevices(items)
          setDeviceError('')
        })
        .catch((cause: unknown) => setDeviceError(friendlyMessage(cause)))
    }, 5_000)
    return () => window.clearInterval(timer)
  }, [needsDeviceDiscovery, open])

  const resetState = () => {
    operationVersion.current += 1
    setDevices(null)
    setSelectedDeviceId(null)
    setRelease(null)
    setReleaseLoading(true)
    setRefreshKey(0)
    setDownloadProgress(null)
    setDownloaded(false)
    setDeviceError('')
    setReleaseError('')
    setDownloadStatus('')
    setSelecting(false)
    setConnecting(false)
    setRefreshingWorkspaces(false)
    setError('')
  }

  const handleCancel = () => {
    resetState()
    onCancel()
  }

  const refreshDevices = () => {
    setDevices(null)
    setSelectedDeviceId(null)
    setRelease(null)
    setError('')
    setDeviceError('')
    setReleaseError('')
    setDownloadStatus('')
    setReleaseLoading(true)
    setRefreshKey((value) => value + 1)
  }

  const refreshWorkspaces = async () => {
    if (!onWorkspacesChanged) return
    setRefreshingWorkspaces(true)
    setError('')
    try {
      await onWorkspacesChanged()
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setRefreshingWorkspaces(false)
    }
  }

  const download = async () => {
    try {
      setError('')
      setDownloadStatus('正在连接公网入口…')
      setDownloadProgress(0)
      const result = await downloadWorkerRelease('windows-x86_64', (received, total) => {
        if (received === 0 && total === 0) setDownloadStatus('下载连接中断，正在自动重试…')
        else if (received > 0) setDownloadStatus('正在下载 Worker 安装程序…')
        setDownloadProgress(total > 0 ? Math.min(99, Math.round((received / total) * 100)) : 0)
      })
      const url = URL.createObjectURL(result.blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = result.filename
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000)
      setDownloadProgress(100)
      setDownloadStatus('安装程序已下载，请运行它完成安装')
      setDownloaded(true)
    } catch (cause) {
      setDownloadProgress(null)
      setDownloadStatus('')
      setError(friendlyMessage(cause))
    }
  }

  const connectWorker = async () => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    try {
      setConnecting(true)
      setError('')
      const hasExistingDevice = intent !== 'host' && (devices ?? []).length > 0
      if (hasExistingDevice) {
        // An older paired Worker should keep its identity and workspaces. Ask
        // the installed service to restart after the installer replaces it;
        // do not create a second device or force a new pairing.
        window.location.href = 'threadforge://worker/start'
      } else {
        const pairing = await createPairingCode()
        const server = window.threadforge?.apiBaseUrl ?? window.location.origin
        const query = new URLSearchParams({ server, code: pairing.code })
        window.location.href = `threadforge://worker/pair?${query.toString()}`
      }
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await delay(1_000)
        if (operationVersion.current !== operation) return
        const items = (await listDevices()).items
        const device = items.find((item) => workerIsReady(item, release))
        if (device) {
          setDevices(items)
          setDeviceError('')
          setSelectedDeviceId(device.device_id)
          await onWorkspacesChanged?.()
          return
        }
      }
      setError('暂未检测到本机 Worker。请确认安装程序已运行完成，再点击“安装完成，连接本机”。')
    } catch (cause) {
      if (operationVersion.current === operation) setError(friendlyMessage(cause))
    } finally {
      if (operationVersion.current === operation) setConnecting(false)
    }
  }

  const selectWorkspace = async (deviceId = selectedDevice?.device_id) => {
    if (!deviceId) return
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    setSelecting(true)
    setError('')
    try {
      let request: WorkspaceSelectionRequest = await requestWorkspaceSelection(deviceId)
      let attempts = 0
      while (request.status === 'pending' && attempts < 122) {
        attempts += 1
        await delay(1_000)
        if (operationVersion.current !== operation) return
        request = await getWorkspaceSelection(deviceId, request.request_id)
      }
      if (operationVersion.current !== operation) return
      if (request.status === 'completed') {
        const updatedWorkspaces = await onWorkspacesChanged?.()
        const addedWorkspace = updatedWorkspaces?.find(
          (workspace) =>
            workspace.workspace_id === request.workspace_id && workspace.device_id === deviceId,
        )
        if (addedWorkspace) onSelect(addedWorkspace)
        if (addedWorkspace && intent === 'workspace') handleCancel()
        return
      }
      if (request.status === 'cancelled') {
        setError('已取消目录选择')
      } else {
        setError(selectionErrors[request.error ?? ''] ?? '目录选择请求未完成')
      }
    } catch (cause) {
      if (operationVersion.current === operation) setError(friendlyMessage(cause))
    } finally {
      if (operationVersion.current === operation) setSelecting(false)
    }
  }

  const renderDownloadPanel = () => (
    <div className="space-y-3 rounded-lg border border-stone-200 px-3 py-4">
      <Alert
        type="info"
        showIcon
        message={outdatedDevices.length > 0 ? '检测到需要更新的 Worker' : '没有可用的兼容 Worker'}
        description="请安装或更新本机 Worker。安装程序自带运行环境，不需要单独安装 Python。"
      />
      {error ? <Alert type="error" showIcon message={error} /> : null}
      {releaseError ? (
        <Alert
          type="error"
          showIcon
          message="无法读取 Worker 发布清单"
          description={releaseError}
          action={<Button size="small" onClick={refreshDevices}>重试</Button>}
        />
      ) : null}
      {deviceError ? (
        <Alert
          type="warning"
          showIcon
          message="暂时无法刷新本机 Worker 状态"
          description={`${deviceError}。安装程序下载不受影响，完成安装后可点击“刷新 Worker 状态”重试。`}
        />
      ) : null}
      {outdatedDevices.length > 0 && release ? (
        <Alert
          type="warning"
          showIcon
          message={`检测到旧版 Worker ${outdatedDevices.map((device) => device.version || '版本未知').join('、')}，当前稳定版为 ${release.version}`}
          description="旧版 Worker 不会调用目录选择器，请下载并运行最新安装程序；更新后会保留现有配对和工作区。"
        />
      ) : null}
      {outdatedDevices
        .filter((device) => device.online && (device.capabilities ?? []).includes('auto_update'))
        .map((device) => (
          <Button
            key={device.device_id}
            size="small"
            icon={<ReloadOutlined />}
            loading={updatingDeviceId === device.device_id}
            disabled={activeUpdate !== undefined}
            onClick={() => void manuallyUpdate(device)}
          >
            手动更新 {device.name}
          </Button>
        ))}
      {activeUpdate ? (
        <Alert
          type="info"
          showIcon
          message={`Worker 正在自动更新至 ${activeUpdate.update_status?.target_version || release?.version || '最新版本'}`}
          description={
            activeUpdate.update_status?.status === 'installing'
              ? '安装程序正在替换旧版本，Worker 会自动重启并重新连接。'
              : '下载支持断点续传；网络中断后会自动继续，不需要重复手动下载。'
          }
        />
      ) : null}
      {activeUpdateProgress !== null ? (
        <Progress percent={activeUpdateProgress} status="active" />
      ) : null}
      {outdatedDevices.some((device) => device.update_status?.status === 'failed') ? (
        <Alert
          type="warning"
          showIcon
          message="Worker 自动更新暂时失败，后台将在 30 秒后续传重试"
          description="你也可以使用下面的手动下载安装作为备用方式。"
        />
      ) : null}
      <div className="flex items-center justify-between gap-3 border-b border-stone-200 pb-3">
        <div>
          <div className="text-sm font-medium text-stone-800">Windows Worker</div>
          <div className="mt-1 text-xs text-stone-500">
            {release ? `稳定版 ${release.version}` : error ? '暂时无法读取版本' : '正在读取最新版本'}
          </div>
        </div>
        <Tag color={release ? 'green' : 'default'}>
          {release ? '清单签名已验证' : releaseLoading ? '正在读取清单' : '等待清单'}
        </Tag>
      </div>
      {downloadProgress !== null ? (
        <Progress percent={downloadProgress} status={downloadProgress === 100 ? 'success' : 'active'} />
      ) : null}
      {downloadStatus ? <div className="text-xs text-stone-500">{downloadStatus}</div> : null}
      <Button
        type="primary"
        block
        icon={downloaded ? <CheckCircleOutlined /> : <DownloadOutlined />}
        disabled={!release || releaseLoading || downloaded || connecting}
        loading={Boolean(downloadProgress !== null && downloadProgress < 100)}
        onClick={() => void download()}
      >
        {downloaded ? '安装程序已下载，请运行' : '下载安装程序'}
      </Button>
      {downloaded ? (
        <>
          <Alert
            type="success"
            showIcon
            message="请先运行刚下载的安装程序"
            description={
              (devices ?? []).length > 0
                ? '安装完成后点击下方按钮，网页会等待原有 Worker 重新上线，不会重复创建设备。'
                : '安装完成后点击下方按钮，网页会生成一次性配对码并连接本机 Worker。'
            }
          />
          <Button type="primary" block icon={<PoweroffOutlined />} loading={connecting} onClick={() => void connectWorker()}>
            安装完成，连接本机
          </Button>
        </>
      ) : null}
      <Button block icon={<ReloadOutlined />} onClick={refreshDevices}>
        刷新 Worker 状态
      </Button>
      <Button
        block
        onClick={() => {
          handleCancel()
          onOpenSettings()
        }}
      >
        打开 Worker 设置
      </Button>
    </div>
  )

  return (
    <Modal
      title={intent === 'host' ? '添加主机' : intent === 'workspace' ? '添加工作区' : '新建会话'}
      open={open}
      onOk={intent === 'session' ? onCreate : handleCancel}
      onCancel={handleCancel}
      okText={intent === 'session' ? '创建' : '关闭'}
      cancelText="取消"
      okButtonProps={intent === 'session' ? { disabled: !value } : undefined}
      width={isMobile ? '100%' : 560}
      styles={{ body: isMobile ? { padding: '16px 14px' } : undefined }}
    >
      {intent === 'host' ? renderDownloadPanel() : selectsNewWorkspace ? (
        devices === null ? (
          <div className="flex min-h-40 flex-col items-center justify-center gap-3 text-center text-xs text-stone-500">
            <Spin indicator={<LoadingOutlined spin />} />
            正在检测本账号绑定的 Worker
          </div>
        ) : selectedDevice ? (
          <div className="space-y-3 rounded-lg border border-stone-200 px-3 py-4">
            <Alert
              type="info"
              showIcon
              message="选择要使用的 Worker，并授权一个本地目录"
              description="每台 Worker 可以绑定多个工作区；工作区只保存目录授权，不会移动或修改真实项目文件。"
            />
            {error ? <Alert type="error" showIcon message={error} /> : null}
            {compatibleDevices.length > 1 ? (
              <Radio.Group
                value={selectedDevice.device_id}
                onChange={(event) => setSelectedDeviceId(event.target.value as string)}
                className="flex w-full flex-col gap-2"
              >
                {compatibleDevices.map((device) => (
                  <Radio key={device.device_id} value={device.device_id} className="w-full rounded-lg border border-stone-200 px-3 py-2">
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate text-sm font-medium text-stone-800">{device.name}</span>
                      <span className="text-[11px] text-stone-500">
                        {device.platform || 'unknown'} / {device.architecture || 'unknown'} · Worker{' '}
                        {device.version || '版本未知'} · {device.workspaces.length} 个工作区
                      </span>
                    </span>
                  </Radio>
                ))}
              </Radio.Group>
            ) : (
              <div className="rounded-lg border border-stone-200 px-3 py-3 text-xs text-stone-600">
                <div className="font-medium text-stone-800">{selectedDevice.name}</div>
                <div className="mt-1">
                  {selectedDevice.platform || 'unknown'} / {selectedDevice.architecture || 'unknown'} · Worker{' '}
                  {selectedDevice.version || '版本未知'} · 尚未授权工作区
                </div>
              </div>
            )}
            <Button type="primary" block icon={<FolderOpenOutlined />} loading={selecting} onClick={() => void selectWorkspace()}>
              {selecting ? '等待本机选择目录' : '选择本地目录'}
            </Button>
            <Button block icon={<ReloadOutlined />} onClick={refreshDevices}>
              刷新 Worker 状态
            </Button>
          </div>
        ) : (
          renderDownloadPanel()
        )
      ) : (
        <>
          <div className="mb-2 text-sm font-medium text-stone-800">工作区</div>
          {selectable.length === 0 ? (
            <Alert
              className="mb-3"
              type="warning"
              showIcon
              message="当前没有在线 Worker"
              description="请启动对应设备上的 Worker 后刷新页面；离线工作区仍会保留在列表中。"
              action={
                onWorkspacesChanged ? (
                  <Button size="small" loading={refreshingWorkspaces} onClick={() => void refreshWorkspaces()}>
                    刷新工作区
                  </Button>
                ) : undefined
              }
            />
          ) : null}
          <Radio.Group
            value={value}
            onChange={(event) => {
              const workspace = workspaces.find((item) => workspaceKey(item) === event.target.value)
              if (workspace) onSelect(workspace)
            }}
            className="flex w-full flex-col gap-2"
          >
            {workspaces.map((workspace) => (
              <Radio
                key={workspaceKey(workspace)}
                value={workspaceKey(workspace)}
                disabled={!workspace.available}
                className="w-full rounded-lg border border-stone-200 px-3 py-2"
              >
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-mono text-xs text-stone-700">{workspace.display_path}</span>
                  <span className="text-[11px] text-stone-400">
                    {workspace.execution_environment === 'local_worker'
                      ? `${workspace.device_name ?? '本地 Worker'}${workspace.device_platform ? ` · ${workspace.device_platform}` : ''}${workspace.available ? '' : ' · Worker 离线'}${workspace.model_configured ? '' : ' · 模型未配置'}`
                      : '服务器工作区'}
                  </span>
                </span>
              </Radio>
            ))}
          </Radio.Group>
          {addWorkspaceTarget ? (
            <Button
              className="mt-3"
              block
              icon={<FolderOpenOutlined />}
              loading={selecting}
              onClick={() => void selectWorkspace(addWorkspaceTarget.device_id)}
            >
              {selecting
                ? '等待本机选择目录'
                : `向 ${addWorkspaceTarget.device_name ?? '当前 Worker'} 添加工作区`}
            </Button>
          ) : null}
          {error ? <Alert className="mt-3" type="error" showIcon message={error} /> : null}
        </>
      )}
      <p className="mt-3 text-xs text-stone-500">
        本地 Worker 工作区的真实路径只保存在对应设备；Agent 工具调用仅限所选目录。
      </p>
    </Modal>
  )
}

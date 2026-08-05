import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal, Progress, Radio, Spin, Tag } from 'antd'
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
} from '../../api/client'
import type { Device, Workspace, WorkspaceSelectionRequest, WorkerReleaseManifest } from '../../api/types'
import { workerIsReady, workerNeedsUpdate } from '../devices/worker-version'

const delay = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds))

const selectionErrors: Record<string, string> = {
  native_directory_picker_failed: '本机无法打开目录选择窗口，请重新安装最新 Worker',
  native_directory_picker_unavailable: '本机缺少目录选择组件，请重新安装最新 Worker',
  selection_busy: '本机已有目录选择窗口等待处理',
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
  workspaces: Workspace[]
  selected: string | null
  onSelect: (workspaceId: string) => void
  onCreate: () => void
  onCancel: () => void
  onOpenSettings: () => void
  onWorkspacesChanged?: () => Promise<unknown> | unknown
}

// 新建会话同时负责：选择工作区、绑定多台 Worker、目录授权和首次安装。
export default function NewSessionModal({
  open,
  workspaces,
  selected,
  onSelect,
  onCreate,
  onCancel,
  onOpenSettings,
  onWorkspacesChanged,
}: NewSessionModalProps) {
  const selectable = workspaces.filter((w) => w.available)
  const value = selected && selectable.some((w) => w.workspace_id === selected) ? selected : undefined
  const [devices, setDevices] = useState<Device[] | null>(null)
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null)
  const [release, setRelease] = useState<WorkerReleaseManifest | null>(null)
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null)
  const [downloaded, setDownloaded] = useState(false)
  const [error, setError] = useState('')
  const [selecting, setSelecting] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const operationVersion = useRef(0)
  const noWorkspace = selectable.length === 0
  const releaseLoading = devices !== null && release === null && !error
  const compatibleDevices = (devices ?? []).filter((device) => workerIsReady(device, release))
  const outdatedDevices = release
    ? (devices ?? []).filter((device) => workerNeedsUpdate(device, release))
    : []
  const selectedDevice =
    compatibleDevices.find((device) => device.device_id === selectedDeviceId) ?? compatibleDevices[0] ?? null

  useEffect(() => {
    if (!open || !noWorkspace || devices !== null) return
    let active = true
    void listDevices()
      .then(({ items }) => {
        if (active) setDevices(items)
      })
      .catch((cause: unknown) => {
        if (active) {
          setDevices([])
          setError(friendlyMessage(cause))
        }
      })
    void getLatestWorkerRelease()
      .then((manifest) => {
        if (active) setRelease(manifest)
      })
      .catch((cause: unknown) => {
        if (active) setError(friendlyMessage(cause))
      })
    return () => {
      active = false
    }
  }, [devices, noWorkspace, open])

  const resetState = () => {
    operationVersion.current += 1
    setDevices(null)
    setSelectedDeviceId(null)
    setRelease(null)
    setDownloadProgress(null)
    setDownloaded(false)
    setSelecting(false)
    setConnecting(false)
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
  }

  const download = async () => {
    try {
      setError('')
      setDownloadProgress(0)
      const result = await downloadWorkerRelease('windows-x86_64', (received, total) => {
        setDownloadProgress(total > 0 ? Math.min(99, Math.round((received / total) * 100)) : 0)
      })
      const url = URL.createObjectURL(result.blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = result.filename
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000)
      setDownloadProgress(100)
      setDownloaded(true)
    } catch (cause) {
      setDownloadProgress(null)
      setError(friendlyMessage(cause))
    }
  }

  const connectWorker = async () => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    try {
      setConnecting(true)
      setError('')
      const hasExistingDevice = (devices ?? []).length > 0
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

  const selectWorkspace = async () => {
    if (!selectedDevice) return
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    setSelecting(true)
    setError('')
    try {
      let request: WorkspaceSelectionRequest = await requestWorkspaceSelection(selectedDevice.device_id)
      let attempts = 0
      while (request.status === 'pending' && attempts < 122) {
        attempts += 1
        await delay(1_000)
        if (operationVersion.current !== operation) return
        request = await getWorkspaceSelection(selectedDevice.device_id, request.request_id)
      }
      if (operationVersion.current !== operation) return
      if (request.status === 'completed') {
        await onWorkspacesChanged?.()
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
      {outdatedDevices.length > 0 && release ? (
        <Alert
          type="warning"
          showIcon
          message={`检测到旧版 Worker ${outdatedDevices.map((device) => device.version || '版本未知').join('、')}，当前稳定版为 ${release.version}`}
          description="旧版 Worker 不会调用目录选择器，请下载并运行最新安装程序；更新后会保留现有配对和工作区。"
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
      title="新建会话"
      open={open}
      onOk={onCreate}
      onCancel={handleCancel}
      okText="创建"
      cancelText="取消"
      okButtonProps={{ disabled: !value }}
      width={560}
    >
      <div className="mb-2 text-sm font-medium text-stone-800">工作区</div>
      {selectable.length === 0 ? (
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
              message="请选择要使用的 Worker，并授权一个本地目录"
              description="每台 Worker 可以绑定多个工作区；后续可以在这里切换不同设备。"
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
        <Radio.Group
          value={value}
          onChange={(event) => onSelect(event.target.value as string)}
          className="flex w-full flex-col gap-2"
        >
          {selectable.map((workspace) => (
            <Radio
              key={workspace.workspace_id}
              value={workspace.workspace_id}
              className="w-full rounded-lg border border-stone-200 px-3 py-2"
            >
              <span className="flex min-w-0 flex-col">
                <span className="truncate font-mono text-xs text-stone-700">{workspace.display_path}</span>
                <span className="text-[11px] text-stone-400">
                  {workspace.execution_environment === 'local_worker'
                    ? `${workspace.device_name ?? '本地 Worker'}${workspace.device_platform ? ` · ${workspace.device_platform}` : ''}${workspace.model_configured ? '' : ' · 模型未配置'}`
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

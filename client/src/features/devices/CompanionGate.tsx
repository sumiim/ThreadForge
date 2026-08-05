import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal, Progress, Spin, Tag } from 'antd'
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
import type { Device, WorkspaceSelectionRequest, WorkerReleaseManifest } from '../../api/types'

const WAKE_TIMEOUT_MS = 8_000
const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds))

type GateState = 'checking' | 'waking' | 'workspace' | 'connected' | 'download'

interface CompanionGateProps {
  onWorkspacesChanged?: () => Promise<unknown> | unknown
}

interface ProbeResult {
  items: Device[]
  device: Device | null
}

const selectionErrors: Record<string, string> = {
  native_directory_picker_failed: '本机无法打开目录选择窗口，请重启 Worker 后重试',
  native_directory_picker_unavailable: '本机缺少目录选择组件，请重新安装最新 Worker',
  selection_busy: '本机已有目录选择窗口等待处理',
  selection_expired: '目录选择请求已过期',
  selection_failed: '本机目录选择失败',
  workspace_registration_failed: '目录已选择，但 Worker 保存工作区失败',
  worker_disconnected: 'Worker 已断开连接',
  worker_reconnected: 'Worker 重连后请求已失效，请重新操作',
  worker_revoked: '设备已被撤销',
}

export default function CompanionGate({ onWorkspacesChanged }: CompanionGateProps) {
  const [state, setState] = useState<GateState>('checking')
  const [devices, setDevices] = useState<Device[]>([])
  const [workspaceDevice, setWorkspaceDevice] = useState<Device | null>(null)
  const [selecting, setSelecting] = useState(false)
  const [release, setRelease] = useState<WorkerReleaseManifest | null>(null)
  const [error, setError] = useState('')
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null)
  const [downloaded, setDownloaded] = useState(false)
  const operationVersion = useRef(0)
  const workerServer = window.threadforge?.apiBaseUrl ?? window.location.origin

  const needsUpdate = devices.some((device) => device.online && !device.compatible)

  const loadRelease = useCallback(async () => {
    try {
      setRelease(await getLatestWorkerRelease())
    } catch (cause) {
      setError(friendlyMessage(cause))
    }
  }, [])

  const probe = useCallback(async (expectedVersion = operationVersion.current): Promise<ProbeResult> => {
    const items = (await listDevices()).items
    const device = items.find((item) => item.online && item.compatible) ?? null
    if (operationVersion.current !== expectedVersion) return { items, device: null }

    setDevices(items)
    setWorkspaceDevice(device)
    if (device) {
      setState(device.workspaces.length > 0 ? 'connected' : 'workspace')
      setError('')
    }
    return { items, device }
  }, [])

  const waitForConnection = useCallback(
    async (operation: number): Promise<Device | null> => {
      const deadline = Date.now() + WAKE_TIMEOUT_MS
      while (operationVersion.current === operation && Date.now() < deadline) {
        await delay(1_000)
        try {
          const result = await probe(operation)
          if (result.device) return result.device
        } catch {
          // Keep polling through a transient central API error.
        }
      }
      return null
    },
    [probe],
  )

  const runWorkspaceSelection = useCallback(
    async (deviceId: string, operation: number): Promise<boolean> => {
      if (operationVersion.current !== operation) return false
      setSelecting(true)
      setError('')
      try {
        let request: WorkspaceSelectionRequest = await requestWorkspaceSelection(deviceId)
        const deadline = new Date(request.expires_at).getTime() + 2_000
        while (request.status === 'pending' && Date.now() < deadline) {
          await delay(1_000)
          if (operationVersion.current !== operation) return false
          request = await getWorkspaceSelection(deviceId, request.request_id)
        }
        if (operationVersion.current !== operation) return false
        if (request.status === 'completed') {
          await onWorkspacesChanged?.()
          await probe(operation)
          return true
        }
        if (request.status === 'cancelled') {
          setError('已取消目录选择')
        } else {
          setError(selectionErrors[request.error ?? ''] ?? '目录选择请求未完成')
        }
        setState('workspace')
        return false
      } catch (cause) {
        if (operationVersion.current === operation) {
          setError(friendlyMessage(cause))
          setState('workspace')
        }
        return false
      } finally {
        if (operationVersion.current === operation) setSelecting(false)
      }
    },
    [onWorkspacesChanged, probe],
  )

  const selectWorkspace = async (deviceId: string) => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    await runWorkspaceSelection(deviceId, operation)
  }

  const wakeAndWait = useCallback(async () => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    setState('waking')
    window.location.href = 'threadforge://worker/start'
    if (!(await waitForConnection(operation)) && operationVersion.current === operation) {
      setState('download')
      void loadRelease()
    }
  }, [loadRelease, waitForConnection])

  useEffect(() => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    void probe(operation)
      .then(async ({ items, device }) => {
        if (operationVersion.current !== operation || device) return
        if (items.length === 0 || items.some((item) => item.online && !item.compatible)) {
          setState('download')
          await loadRelease()
        } else {
          await wakeAndWait()
        }
      })
      .catch((cause: unknown) => {
        if (operationVersion.current !== operation) return
        setError(friendlyMessage(cause))
        setState('download')
      })
    return () => {
      operationVersion.current += 1
    }
  }, [loadRelease, probe, wakeAndWait])

  useEffect(() => {
    if (state !== 'download' && state !== 'workspace') return
    const timer = window.setInterval(() => {
      if (selecting) return
      void probe()
        .then(({ items, device }) => {
          if (device || state !== 'workspace' || operationVersion.current === 0) return
          if (items.length === 0 || items.some((item) => item.online && !item.compatible)) {
            setState('download')
            void loadRelease()
          } else {
            void wakeAndWait()
          }
        })
        .catch(() => undefined)
    }, 3_000)
    return () => window.clearInterval(timer)
  }, [loadRelease, probe, selecting, state, wakeAndWait])

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

  const pairAndWait = async () => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    try {
      setError('')
      const pairing = await createPairingCode()
      setState('waking')
      const query = new URLSearchParams({ server: workerServer, code: pairing.code })
      window.location.href = `threadforge://worker/pair?${query.toString()}`
      const device = await waitForConnection(operation)
      if (!device && operationVersion.current === operation) {
        setError('未能连接 Companion。请确认安装程序已运行完成，再重新连接。')
        setState('download')
        return
      }
      if (device && device.workspaces.length === 0) {
        await runWorkspaceSelection(device.device_id, operation)
      }
    } catch (cause) {
      if (operationVersion.current === operation) {
        setError(friendlyMessage(cause))
        setState('download')
      }
    }
  }

  if (state === 'connected') return null

  return (
    <Modal
      title="连接本地 Worker Companion"
      open
      closable={false}
      maskClosable={false}
      footer={null}
      width={520}
    >
      <div className="space-y-4">
        {state === 'checking' || state === 'waking' ? (
          <div className="flex min-h-32 flex-col items-center justify-center gap-3 text-center">
            <Spin indicator={<LoadingOutlined spin />} size="large" />
            <div>
              <div className="text-sm font-medium text-stone-800">
                {state === 'checking' ? '正在检查本地 Companion' : '正在唤醒本地 Companion'}
              </div>
              <div className="mt-1 text-xs text-stone-500">连接成功后会自动进入工作台</div>
            </div>
          </div>
        ) : state === 'workspace' ? (
          <>
            <Alert
              type="info"
              showIcon
              message="Worker 已连接，请授权一个本地工作区"
              description="目录选择窗口会在 Worker 所在的电脑上打开，网页不会读取或上传真实路径。"
            />
            {error ? <Alert type="error" showIcon message={error} /> : null}
            {workspaceDevice ? (
              <div className="rounded-lg border border-stone-200 px-3 py-3 text-xs text-stone-600">
                <div className="font-medium text-stone-800">{workspaceDevice.name}</div>
                <div className="mt-1">
                  {workspaceDevice.platform || 'unknown'} / {workspaceDevice.architecture || 'unknown'} · 尚未授权工作区
                </div>
              </div>
            ) : null}
            <Button
              type="primary"
              block
              icon={<FolderOpenOutlined />}
              loading={selecting}
              disabled={!workspaceDevice}
              onClick={() => {
                if (workspaceDevice) void selectWorkspace(workspaceDevice.device_id)
              }}
            >
              {selecting ? '等待本机选择目录' : '选择本地目录'}
            </Button>
            <Button block icon={<ReloadOutlined />} onClick={() => void probe()}>
              刷新连接状态
            </Button>
          </>
        ) : (
          <>
            <Alert
              type={needsUpdate ? 'warning' : 'info'}
              showIcon
              message={needsUpdate ? '本机 Worker 与当前服务不兼容，需要更新' : '尚未检测到可用的本地 Companion'}
            />
            {error ? <Alert type="error" showIcon message={error} /> : null}
            <div className="flex items-center justify-between gap-3 border-b border-stone-200 pb-3">
              <div>
                <div className="text-sm font-medium text-stone-800">Windows Worker</div>
                <div className="mt-1 text-xs text-stone-500">
                  {release ? `稳定版 ${release.version}` : '正在读取最新版本'}
                </div>
              </div>
              <Tag color={release ? 'green' : 'default'}>
                {release ? '清单签名已验证' : '等待清单'}
              </Tag>
            </div>
            <div className="text-xs leading-5 text-stone-600">
              安装程序已包含 Worker 和完整运行环境，不需要安装 Python 或其他依赖。下载后只需运行一次。
            </div>
            {downloadProgress !== null ? (
              <Progress percent={downloadProgress} status={downloadProgress === 100 ? 'success' : 'active'} />
            ) : null}
            <Button
              type="primary"
              block
              icon={downloaded ? <CheckCircleOutlined /> : <DownloadOutlined />}
              disabled={!release || downloaded}
              loading={downloadProgress !== null && downloadProgress < 100}
              onClick={() => void download()}
            >
              {downloaded ? '安装程序已下载，请运行' : '下载安装程序'}
            </Button>
            {downloaded ? (
              <Alert
                type="success"
                showIcon
                message="安装完成后返回此页面，点击下方按钮即可自动绑定当前账号。"
              />
            ) : null}
            {downloaded ? (
              <Button type="primary" block icon={<PoweroffOutlined />} onClick={() => void pairAndWait()}>
                安装完成，连接本机
              </Button>
            ) : null}
            <Button block icon={<PoweroffOutlined />} onClick={() => void wakeAndWait()}>
              已安装，重新唤醒
            </Button>
          </>
        )}
      </div>
    </Modal>
  )
}

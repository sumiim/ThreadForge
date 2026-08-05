import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal, Progress, Spin, Tag, Typography } from 'antd'
import {
  CheckCircleOutlined,
  DownloadOutlined,
  LinkOutlined,
  LoadingOutlined,
  PoweroffOutlined,
} from '@ant-design/icons'
import {
  createPairingCode,
  downloadWorkerRelease,
  friendlyMessage,
  getLatestWorkerRelease,
  listDevices,
} from '../../api/client'
import type { Device, WorkerReleaseManifest } from '../../api/types'

const WAKE_TIMEOUT_MS = 8_000
const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds))
type GateState = 'checking' | 'waking' | 'connected' | 'download'

export default function CompanionGate() {
  const [state, setState] = useState<GateState>('checking')
  const [devices, setDevices] = useState<Device[]>([])
  const [release, setRelease] = useState<WorkerReleaseManifest | null>(null)
  const [error, setError] = useState('')
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null)
  const [downloaded, setDownloaded] = useState(false)
  const [pairing, setPairing] = useState<{ code: string; expires_in_seconds: number } | null>(null)
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

  const probe = useCallback(async (expectedVersion = operationVersion.current) => {
    const result = (await listDevices()).items
    if (operationVersion.current !== expectedVersion) return false
    setDevices(result)
    const connected = result.some((device) => device.online && device.compatible)
    if (connected) {
      setState('connected')
      setError('')
    }
    return connected
  }, [])

  const wakeAndWait = useCallback(async () => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    setState('waking')
    window.location.href = 'threadforge://worker/start'
    const deadline = Date.now() + WAKE_TIMEOUT_MS
    while (operationVersion.current === operation && Date.now() < deadline) {
      await delay(1_000)
      try {
        if (await probe(operation)) return
      } catch {
        // Keep polling through a transient central API error.
      }
    }
    if (operationVersion.current === operation) {
      setState('download')
      void loadRelease()
    }
  }, [loadRelease, probe])

  useEffect(() => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    void listDevices()
      .then(async ({ items }) => {
        if (operationVersion.current !== operation) return
        setDevices(items)
        if (items.some((device) => device.online && device.compatible)) {
          setState('connected')
        } else if (items.length === 0 || items.some((device) => device.online && !device.compatible)) {
          setState('download')
          await loadRelease()
        } else {
          await wakeAndWait()
        }
      })
      .catch((cause: unknown) => {
        setError(friendlyMessage(cause))
        setState('download')
      })
    return () => {
      operationVersion.current += 1
    }
  }, [loadRelease, wakeAndWait])

  useEffect(() => {
    if (state !== 'download') return
    const timer = window.setInterval(() => void probe().catch(() => undefined), 3_000)
    return () => window.clearInterval(timer)
  }, [probe, state])

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
      setPairing(await createPairingCode())
    } catch (cause) {
      setDownloadProgress(null)
      setError(friendlyMessage(cause))
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
              <Tag color={release ? 'green' : 'default'}>{release ? '签名版本' : '等待清单'}</Tag>
            </div>
            <div className="text-xs leading-5 text-stone-600">
              安装包包含 Worker 与运行依赖 wheels，需要电脑已安装 Python 3.12。解压后运行
              <Typography.Text code>install-worker.ps1</Typography.Text>。
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
              {downloaded ? '安装包已下载' : '下载并校验 Worker 安装包'}
            </Button>
            {pairing ? (
              <div className="rounded-lg bg-stone-100 p-3 text-xs text-stone-600">
                <div className="mb-1 flex items-center gap-1.5 font-medium text-stone-800">
                  <LinkOutlined /> 安装后绑定此账号
                </div>
                <Typography.Text copyable className="block font-mono text-base">
                  {pairing.code}
                </Typography.Text>
                <code className="mt-2 block break-all">
                  threadforge-worker pair --server {workerServer} --code {pairing.code}
                </code>
              </div>
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

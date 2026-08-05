import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal, Progress, Radio, Tag } from 'antd'
import {
  CheckCircleOutlined,
  DownloadOutlined,
  PoweroffOutlined,
} from '@ant-design/icons'
import {
  createPairingCode,
  downloadWorkerRelease,
  friendlyMessage,
  getLatestWorkerRelease,
  listDevices,
} from '../../api/client'
import type { Workspace, WorkerReleaseManifest } from '../../api/types'

const delay = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds))

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

// 新建会话：选择工作区（GET /api/v1/workspaces 返回的可用工作区）
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
  const [release, setRelease] = useState<WorkerReleaseManifest | null>(null)
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null)
  const [downloaded, setDownloaded] = useState(false)
  const [error, setError] = useState('')
  const [connecting, setConnecting] = useState(false)
  const operationVersion = useRef(0)
  const noWorkspace = selectable.length === 0

  useEffect(() => {
    if (!open || !noWorkspace || release) return
    let active = true
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
  }, [open, noWorkspace, release])

  const resetDownloadState = () => {
    operationVersion.current += 1
    setConnecting(false)
    setDownloadProgress(null)
    setDownloaded(false)
    setError('')
  }

  const handleCancel = () => {
    resetDownloadState()
    onCancel()
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
      const pairing = await createPairingCode()
      const server = window.threadforge?.apiBaseUrl ?? window.location.origin
      const query = new URLSearchParams({ server, code: pairing.code })
      window.location.href = `threadforge://worker/pair?${query.toString()}`
      const deadline = Date.now() + 10_000
      while (Date.now() < deadline) {
        await delay(1_000)
        if (operationVersion.current !== operation) return
        const devices = (await listDevices()).items
        if (devices.some((device) => device.online && device.compatible)) {
          await onWorkspacesChanged?.()
          handleCancel()
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

  return (
    <Modal
      title="新建会话"
      open={open}
      onOk={onCreate}
      onCancel={handleCancel}
      okText="创建"
      cancelText="取消"
      okButtonProps={{ disabled: !value }}
    >
      <div className="mb-2 text-sm font-medium text-stone-800">工作区</div>
      {selectable.length === 0 ? (
        <div className="space-y-3 rounded-lg border border-stone-200 px-3 py-4">
          <Alert
            type="info"
            showIcon
            message="暂无可用工作区"
            description="需要在本机安装并连接 Worker，然后在 Worker 所在电脑选择一个目录。"
          />
          {error ? <Alert type="error" showIcon message={error} /> : null}
          <div className="flex items-center justify-between gap-3 border-b border-stone-200 pb-3">
            <div>
              <div className="text-sm font-medium text-stone-800">Windows Worker</div>
              <div className="mt-1 text-xs text-stone-500">
                {release ? `稳定版 ${release.version}` : error ? '暂时无法读取版本' : '正在读取最新版本'}
              </div>
            </div>
            <Tag color={release ? 'green' : 'default'}>
              {release ? '清单签名已验证' : '等待清单'}
            </Tag>
          </div>
          <div className="text-xs leading-5 text-stone-600">
            安装程序已包含 Worker 和完整运行环境，不需要安装 Python 或其他依赖。下载后运行一次即可。
          </div>
          {downloadProgress !== null ? (
            <Progress percent={downloadProgress} status={downloadProgress === 100 ? 'success' : 'active'} />
          ) : null}
          <Button
            type="primary"
            block
            icon={downloaded ? <CheckCircleOutlined /> : <DownloadOutlined />}
            disabled={!release || downloaded || connecting}
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
                description="安装完成后点击下方按钮，网页会生成一次性配对码并连接本机 Worker。"
              />
              <Button
                type="primary"
                block
                icon={<PoweroffOutlined />}
                loading={connecting}
                onClick={() => void connectWorker()}
              >
                安装完成，连接本机
              </Button>
            </>
          ) : null}
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
      ) : (
        <Radio.Group
          value={value}
          onChange={(e) => onSelect(e.target.value as string)}
          className="flex w-full flex-col gap-2"
        >
          {selectable.map((w) => (
            <Radio key={w.workspace_id} value={w.workspace_id} className="w-full rounded-lg border border-stone-200 px-3 py-2">
              <span className="flex min-w-0 flex-col">
                <span className="truncate font-mono text-xs text-stone-700">{w.display_path}</span>
                <span className="text-[11px] text-stone-400">
                  {w.execution_environment === 'local_worker'
                    ? `${w.device_name ?? '本地 Worker'}${w.device_platform ? ` · ${w.device_platform}` : ''}${w.model_configured ? '' : ' · 模型未配置'}`
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

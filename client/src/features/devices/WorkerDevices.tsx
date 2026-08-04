import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Input, Popconfirm, Spin, Tag, Typography } from 'antd'
import {
  DeleteOutlined,
  FolderOpenOutlined,
  LaptopOutlined,
  LinkOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { createPairingCode, friendlyMessage, listDevices, revokeDevice } from '../../api/client'
import type { Device } from '../../api/types'

export default function WorkerDevices() {
  const desktop = window.threadforge?.desktop
  const [devices, setDevices] = useState<Device[]>([])
  const [workerStatus, setWorkerStatus] = useState<Awaited<ReturnType<NonNullable<typeof desktop>['workerStatus']>> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pairing, setPairing] = useState<{ code: string; expires_in_seconds: number } | null>(null)
  const [workerName, setWorkerName] = useState('我的电脑')
  const workerServer = window.threadforge?.apiBaseUrl ?? window.location.origin

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [deviceResponse, nextWorkerStatus] = await Promise.all([
        listDevices(),
        desktop ? desktop.workerStatus() : Promise.resolve(null),
      ])
      setDevices(deviceResponse.items)
      setWorkerStatus(nextWorkerStatus)
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setLoading(false)
    }
  }, [desktop])

  useEffect(() => {
    let active = true
    void Promise.all([
      listDevices(),
      desktop ? desktop.workerStatus() : Promise.resolve(null),
    ])
      .then(([response, nextWorkerStatus]) => {
        if (active) {
          setDevices(response.items)
          setWorkerStatus(nextWorkerStatus)
        }
      })
      .catch((cause: unknown) => {
        if (active) setError(friendlyMessage(cause))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [desktop])

  const createCode = async () => {
    try {
      setPairing(await createPairingCode())
    } catch (cause) {
      setError(friendlyMessage(cause))
    }
  }

  const pairDesktopWorker = async () => {
    if (!desktop || !pairing) return
    try {
      await desktop.pairWorker({ server: workerServer, code: pairing.code, name: workerName })
      setPairing(null)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '桌面 Worker 配对失败')
    }
  }

  const chooseWorkspace = async () => {
    if (!desktop) return
    try {
      const selectedPath = await desktop.selectDirectory()
      if (!selectedPath) return
      await desktop.addWorkspace({ path: selectedPath })
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '工作区添加失败')
    }
  }

  const startDesktopWorker = async () => {
    if (!desktop) return
    try {
      await desktop.startWorker()
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Worker 启动失败')
    }
  }

  const stopDesktopWorker = async () => {
    if (!desktop) return
    try {
      await desktop.stopWorker()
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Worker 停止失败')
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

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <div className="text-sm font-medium text-stone-800">本地 Worker</div>
        <Button type="text" size="small" icon={<ReloadOutlined />} onClick={() => void refresh()} />
      </div>
      <p className="mb-3 text-xs text-stone-500">
        Worker 在本机执行 Agent、文件、Git 与 Shell；服务器只同步会话和事件。
      </p>
      {error ? <Alert className="mb-3" type="error" showIcon message={error} /> : null}
      {desktop ? (
        <div className="mb-3 rounded-xl border border-stone-200 bg-stone-50 p-3">
          <div className="flex items-center gap-2 text-xs text-stone-600">
            <Tag color={workerStatus?.running ? 'green' : 'default'}>
              {workerStatus?.running ? '运行中' : '未运行'}
            </Tag>
            {workerStatus?.installed
              ? `桌面端自动管理已启用 · ${workerStatus.workspaceCount} 个本地目录`
              : '尚未安装 Worker'}
          </div>
          {workerStatus?.error ? (
            <Alert className="mt-2" type="warning" showIcon message={workerStatus.error} />
          ) : null}
          {!workerStatus?.installed ? (
            <p className="mt-2 text-[11px] text-stone-500">
              请先在仓库执行 scripts/install-worker.ps1，重新打开桌面端后即可自动启动。
            </p>
          ) : null}
        </div>
      ) : null}
      {loading ? (
        <Spin size="small" />
      ) : devices.length === 0 ? (
        <div className="rounded-xl border border-dashed border-stone-300 p-3 text-xs text-stone-500">
          尚未绑定设备。
        </div>
      ) : (
        <div className="space-y-2">
          {devices.map((device) => (
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
                {device.workspaces.length} 个工作区 · {device.model_configured ? device.model : '模型未配置'}
              </div>
            </div>
          ))}
        </div>
      )}
      <Button className="mt-3" block icon={<LinkOutlined />} onClick={() => void createCode()}>
        绑定新设备
      </Button>
      {pairing ? (
        <div className="mt-3 rounded-xl bg-stone-100 p-3">
          <div className="text-xs text-stone-500">一次性配对码（{Math.floor(pairing.expires_in_seconds / 60)} 分钟）</div>
          <Typography.Text copyable className="mt-1 block font-mono text-lg tracking-wider">
            {pairing.code}
          </Typography.Text>
          <div className="mt-2 text-[11px] text-stone-500">
            {desktop ? '桌面端可以直接完成配对；也可以复制配对码到命令行。' : '在本机执行：'}
            {!desktop ? (
              <code className="break-all">
                threadforge-worker pair --server {workerServer} --code {pairing.code}
              </code>
            ) : null}
          </div>
          {desktop ? (
            <>
              <Input
                className="mt-3"
                value={workerName}
                onChange={(event) => setWorkerName(event.target.value)}
                placeholder="设备名称"
                maxLength={128}
              />
              <Button className="mt-2" type="primary" block onClick={() => void pairDesktopWorker()}>
                在桌面端配对并启动
              </Button>
            </>
          ) : null}
        </div>
      ) : null}
      {desktop && workerStatus?.paired ? (
        <div className="mt-3 flex gap-2">
          <Button icon={<FolderOpenOutlined />} onClick={() => void chooseWorkspace()}>
            选择本地目录
          </Button>
          {workerStatus.running ? (
            <Button danger icon={<StopOutlined />} onClick={() => void stopDesktopWorker()}>
              停止 Worker
            </Button>
          ) : (
            <Button icon={<PlayCircleOutlined />} onClick={() => void startDesktopWorker()}>
              启动 Worker
            </Button>
          )}
        </div>
      ) : null}
    </div>
  )
}

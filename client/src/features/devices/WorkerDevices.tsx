import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Popconfirm, Spin, Tag, Typography } from 'antd'
import { DeleteOutlined, LaptopOutlined, LinkOutlined, ReloadOutlined } from '@ant-design/icons'
import { createPairingCode, friendlyMessage, listDevices, revokeDevice } from '../../api/client'
import type { Device } from '../../api/types'

export default function WorkerDevices() {
  const [devices, setDevices] = useState<Device[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pairing, setPairing] = useState<{ code: string; expires_in_seconds: number } | null>(null)
  const workerServer = window.threadforge?.apiBaseUrl ?? window.location.origin

  const refresh = useCallback(async () => {
    setError('')
    try {
      setDevices((await listDevices()).items)
    } catch (cause) {
      setError(friendlyMessage(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    void listDevices()
      .then((response) => {
        if (active) setDevices(response.items)
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
  }, [])

  const createCode = async () => {
    try {
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
            在本机执行：
            <code className="break-all">
              threadforge-worker pair --server {workerServer} --code {pairing.code}
            </code>
          </div>
        </div>
      ) : null}
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal } from 'antd'
import { FolderOpenOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  friendlyMessage,
  getWorkspaceSelection,
  listDevices,
  requestWorkspaceSelection,
} from '../../api/client'
import type { Device, WorkspaceSelectionRequest } from '../../api/types'

const WAKE_TIMEOUT_MS = 8_000
const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds))

type GateState = 'checking' | 'waking' | 'workspace' | 'connected' | 'idle'

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
  const [workspaceDevice, setWorkspaceDevice] = useState<Device | null>(null)
  const [selecting, setSelecting] = useState(false)
  const [error, setError] = useState('')
  const operationVersion = useRef(0)

  const probe = useCallback(async (expectedVersion = operationVersion.current): Promise<ProbeResult> => {
    const items = (await listDevices()).items
    const device = items.find((item) => item.online && item.compatible) ?? null
    if (operationVersion.current !== expectedVersion) return { items, device: null }

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
      setState('idle')
    }
  }, [waitForConnection])

  useEffect(() => {
    const operation = operationVersion.current + 1
    operationVersion.current = operation
    void probe(operation)
      .then(async ({ items, device }) => {
        if (operationVersion.current !== operation || device) return
        if (items.length === 0 || items.some((item) => item.online && !item.compatible)) {
          setState('idle')
        } else {
          await wakeAndWait()
        }
      })
      .catch((cause: unknown) => {
        if (operationVersion.current !== operation) return
        setError(friendlyMessage(cause))
        setState('idle')
      })
    return () => {
      operationVersion.current += 1
    }
  }, [probe, wakeAndWait])

  useEffect(() => {
    if (state !== 'idle' && state !== 'workspace') return
    const timer = window.setInterval(() => {
      if (selecting) return
      void probe()
        .then(({ items, device }) => {
          if (device || operationVersion.current === 0) return
          if (state === 'workspace' && items.length > 0 && !items.some((item) => item.online && !item.compatible)) {
            void wakeAndWait()
          }
        })
        .catch(() => undefined)
    }, 3_000)
    return () => window.clearInterval(timer)
  }, [probe, selecting, state, wakeAndWait])

  if (state !== 'workspace') return null

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
      </div>
    </Modal>
  )
}

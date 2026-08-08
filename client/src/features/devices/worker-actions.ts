import type { Device } from '../../api/types'

interface WorkerDeviceActionState {
  canRemoteUninstall: boolean
  pending: boolean
  uninstallLabel: string
}

export function getWorkerDeviceActionState(
  device: Pick<Device, 'device_id' | 'online' | 'capabilities'>,
  revokingDeviceId: string,
  uninstallingDeviceId: string,
): WorkerDeviceActionState {
  const canRemoteUninstall =
    device.online && (device.capabilities ?? []).includes('worker_uninstall')
  return {
    canRemoteUninstall,
    pending:
      revokingDeviceId === device.device_id || uninstallingDeviceId === device.device_id,
    uninstallLabel: canRemoteUninstall ? '卸载 Worker' : '本机卸载 Worker',
  }
}

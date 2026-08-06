import type { Session, Workspace } from '../../api/types'

type WorkspaceIdentity = {
  workspace_id: string
  device_id?: string
  execution_environment?: string
}

export function workspaceKey(workspace: WorkspaceIdentity): string {
  return [
    workspace.execution_environment || 'backend_process',
    workspace.device_id || 'backend',
    workspace.workspace_id,
  ].join(':')
}

export function sessionWorkspaceKey(session: Pick<Session, 'workspaceId' | 'deviceId' | 'executionEnvironment'>): string {
  return workspaceKey({
    workspace_id: session.workspaceId,
    device_id: session.deviceId,
    execution_environment: session.executionEnvironment,
  })
}

export function workspaceDeviceKey(workspace: Pick<Workspace, 'device_id' | 'execution_environment'>): string {
  return workspace.execution_environment === 'local_worker'
    ? `device:${workspace.device_id || 'unknown'}`
    : 'device:backend'
}

export function workspaceDeviceLabel(workspace: Pick<Workspace, 'device_name' | 'execution_environment'>): string {
  return workspace.execution_environment === 'local_worker'
    ? workspace.device_name || '本地 Worker'
    : '后端工作区'
}

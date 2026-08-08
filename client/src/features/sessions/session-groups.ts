import type { Session, Workspace } from '../../api/types'
import {
  sessionWorkspaceKey,
  workspaceDeviceKey,
  workspaceDeviceLabel,
  workspaceKey,
} from './workspaceIdentity.ts'

export interface WorkspaceGroup {
  key: string
  label: string
  workspaceId: string
  deviceId?: string
  sessions: Session[]
}

export interface DeviceGroup {
  key: string
  label: string
  deviceId?: string
  workspaces: WorkspaceGroup[]
}

export function buildDeviceGroups(
  workspaces: Workspace[],
  sessions: Session[],
  includeEmpty: boolean,
): DeviceGroup[] {
  const devices = new Map<string, DeviceGroup>()
  const workspaceGroups = new Map<string, WorkspaceGroup>()

  const addWorkspace = (workspace: Workspace) => {
    const deviceKey = workspaceDeviceKey(workspace)
    let device = devices.get(deviceKey)
    if (!device) {
      device = {
        key: deviceKey,
        label: workspaceDeviceLabel(workspace),
        deviceId: workspace.device_id,
        workspaces: [],
      }
      devices.set(deviceKey, device)
    }
    const key = workspaceKey(workspace)
    let group = workspaceGroups.get(key)
    if (!group) {
      group = {
        key,
        label: workspace.display_name || workspace.name || workspace.display_path || workspace.workspace_id,
        workspaceId: workspace.workspace_id,
        deviceId: workspace.device_id,
        sessions: [],
      }
      workspaceGroups.set(key, group)
      device.workspaces.push(group)
    }
    return group
  }

  if (includeEmpty) {
    workspaces.forEach(addWorkspace)
  }

  sessions.forEach((session) => {
    const known = workspaces.find(
      (workspace) => workspaceKey(workspace) === sessionWorkspaceKey(session),
    )
    // An unbound device no longer belongs in the active navigation tree.
    // Keep its session data intact so re-pairing can restore it later.
    if (!known) return
    addWorkspace(known).sessions.push(session)
  })

  return Array.from(devices.values())
    .map((device) => ({
      ...device,
      workspaces: device.workspaces.filter((workspace) => includeEmpty || workspace.sessions.length > 0),
    }))
    .filter((device) => device.workspaces.length > 0)
}

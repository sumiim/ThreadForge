import assert from 'node:assert/strict'
import test from 'node:test'
import type { Session, Workspace } from '../../api/types.ts'
import { buildDeviceGroups } from './session-groups.ts'

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    workspace_id: 'ws_current',
    name: 'ThreadForge',
    display_path: 'Worker / ThreadForge',
    available: true,
    is_git: true,
    execution_environment: 'local_worker',
    container_sandbox_enabled: false,
    device_id: 'dev_current',
    device_name: 'Worker',
    ...overrides,
  }
}

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: 'session_current',
    title: '当前会话',
    createdAt: '2026-08-09T00:00:00Z',
    workspaceId: 'ws_current',
    executionEnvironment: 'local_worker',
    deviceId: 'dev_current',
    model: 'gpt-5.4',
    modelOptions: [],
    messages: [],
    ...overrides,
  }
}

test('hides retained sessions after their device or workspace is unbound', () => {
  const groups = buildDeviceGroups(
    [workspace()],
    [
      session(),
      session({
        id: 'session_unbound',
        title: '保留的历史会话',
        workspaceId: 'ws_unbound',
        deviceId: 'dev_unbound',
      }),
    ],
    true,
  )

  assert.equal(groups.length, 1)
  assert.equal(groups[0].label, 'Worker')
  assert.equal(groups[0].workspaces.length, 1)
  assert.equal(groups[0].workspaces[0].label, 'ThreadForge')
  assert.deepEqual(groups[0].workspaces[0].sessions.map((item) => item.id), ['session_current'])
})

test('does not synthesize a raw workspace id when every retained session is unbound', () => {
  const groups = buildDeviceGroups(
    [],
    [session({ workspaceId: 'ws_bfabc036031643eab159a50a4714ffff' })],
    true,
  )

  assert.deepEqual(groups, [])
})

test('keeps an empty current workspace visible outside search mode', () => {
  assert.equal(buildDeviceGroups([workspace()], [], true).length, 1)
  assert.equal(buildDeviceGroups([workspace()], [], false).length, 0)
})

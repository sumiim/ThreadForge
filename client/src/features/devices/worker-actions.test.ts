import assert from 'node:assert/strict'
import test from 'node:test'
import { getWorkerDeviceActionState } from './worker-actions.ts'

test('uses authenticated remote uninstall for an online capable Worker', () => {
  assert.deepEqual(
    getWorkerDeviceActionState(
      { device_id: 'online', online: true, capabilities: ['worker_uninstall'] },
      '',
      '',
    ),
    {
      canRemoteUninstall: true,
      pending: false,
      uninstallLabel: '卸载 Worker',
    },
  )
})

test('keeps local uninstall available when the Worker is offline or too old', () => {
  assert.deepEqual(
    getWorkerDeviceActionState(
      { device_id: 'offline', online: false, capabilities: [] },
      '',
      'offline',
    ),
    {
      canRemoteUninstall: false,
      pending: true,
      uninstallLabel: '本机卸载 Worker',
    },
  )
})

test('marks a device busy while unbinding to prevent duplicate actions', () => {
  const state = getWorkerDeviceActionState(
    { device_id: 'current', online: true, capabilities: ['worker_uninstall'] },
    'current',
    '',
  )
  assert.equal(state.pending, true)
})

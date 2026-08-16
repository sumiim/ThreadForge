import assert from 'node:assert/strict'
import test from 'node:test'
import { getWorkerDeviceActionState, workerPairingUri } from './worker-actions.ts'

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

test('builds a protocol pairing link from the short-lived pairing code', () => {
  assert.equal(
    workerPairingUri('https://threadforge.example', 'ABCD-1234-EF56-7890'),
    'threadforge://worker/pair?server=https%3A%2F%2Fthreadforge.example&code=ABCD-1234-EF56-7890',
  )
})

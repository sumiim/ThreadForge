import assert from 'node:assert/strict'
import test from 'node:test'
import { compareWorkerVersions, workerIsReady, workerNeedsUpdate, workerVersionAtLeast } from './worker-version.ts'

const release = { version: '0.2.5' }

test('compares Worker versions numerically instead of lexicographically', () => {
  assert.equal(compareWorkerVersions('0.2.2', '0.2.5'), -1)
  assert.equal(compareWorkerVersions('0.2.5', '0.2.5'), 0)
  assert.equal(compareWorkerVersions('0.2.10', '0.2.5'), 1)
  assert.equal(compareWorkerVersions('bad', '0.2.5'), null)
})
test('requires an update when the installed Worker is missing or below the stable version', () => {
  assert.equal(workerVersionAtLeast('', release.version), false)
  assert.equal(
    workerNeedsUpdate({ online: true, compatible: true, version: '0.2.2' }, release),
    true,
  )
  assert.equal(
    workerNeedsUpdate({ online: true, compatible: true, version: '' }, release),
    true,
  )
})

test('allows a current protocol-compatible Worker and rejects an incompatible one', () => {
  assert.equal(
    workerIsReady({ online: true, compatible: true, version: '0.2.5' }, release),
    true,
  )
  assert.equal(
    workerIsReady({ online: true, compatible: true, version: '0.2.10' }, release),
    true,
  )
  assert.equal(
    workerIsReady({ online: true, compatible: false, version: '0.2.5' }, release),
    false,
  )
  assert.equal(
    workerNeedsUpdate({ online: true, compatible: false, version: '0.1.0' }, release),
    true,
  )
})

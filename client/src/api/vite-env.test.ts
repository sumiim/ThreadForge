import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { DEFAULT_API_PROXY_TARGET, resolveApiProxyTarget } from '../../vite-env.ts'

test('uses the local API when no Vite environment override exists', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'threadforge-vite-env-'))
  try {
    assert.equal(resolveApiProxyTarget('development', directory), DEFAULT_API_PROXY_TARGET)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

test('loads the API proxy target from .env.local', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'threadforge-vite-env-'))
  try {
    await writeFile(
      path.join(directory, '.env.local'),
      'VITE_API_PROXY_TARGET=http://127.0.0.1:18000\n',
      'utf8',
    )
    assert.equal(resolveApiProxyTarget('development', directory), 'http://127.0.0.1:18000')
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

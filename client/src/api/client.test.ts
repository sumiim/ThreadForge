import assert from 'node:assert/strict'
import { afterEach, describe, it } from 'node:test'
import {
  ApiError,
  configureWorkerModel,
  createPairingCode,
  downloadWorkerRelease,
  getLatestWorkerRelease,
  getAuthStatus,
  getRuntimeConfig,
  listMcpServers,
  listOnlineWorkers,
  listSkills,
  logout,
  revokeDevice,
} from './client.ts'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

describe('client metadata API', () => {
  it('requests config, skills, and MCP metadata from the backend', async () => {
    const responses = [
      response({ model: 'gpt-test' }),
      response({ items: [] }),
      response({ items: [] }),
    ]
    const urls: string[] = []
    globalThis.fetch = async (input) => {
      urls.push(String(input))
      const next = responses.shift()
      if (!next) throw new Error('unexpected request')
      return next
    }

    await getRuntimeConfig()
    await listSkills()
    await listMcpServers()

    assert.deepEqual(urls, [
      '/api/v1/config',
      '/api/v1/skills',
      '/api/v1/mcp/servers',
    ])
  })

  it('preserves the backend error code and details', async () => {
    globalThis.fetch = async () =>
      response(
        { error: { code: 'not_ready', message: 'not ready', details: { reason: 'startup' } } },
        503,
      )

    await assert.rejects(getRuntimeConfig(), (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.code, 'not_ready')
      assert.equal(error.status, 503)
      assert.deepEqual(error.details, { reason: 'startup' })
      return true
    })
  })
})

describe('client authentication API', () => {
  it('includes credentials and CSRF protection on writes', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = []
    globalThis.fetch = async (input, init) => {
      calls.push({ url: String(input), init })
      return response({ authenticated: true, status: 'signed_out' })
    }

    await getAuthStatus()
    await logout()
    await createPairingCode()
    await revokeDevice('dev_a/b')
    await configureWorkerModel('dev_model', {
      base_url: 'https://provider.example/v1',
      api_key: 'secret',
      model: 'model-a',
    })

    assert.equal(calls[0].url, '/api/v1/auth/status')
    assert.equal(calls[0].init?.credentials, 'include')
    assert.equal(calls[1].url, '/api/v1/auth/logout')
    assert.equal(calls[1].init?.credentials, 'include')
    assert.equal(new Headers(calls[1].init?.headers).get('X-ThreadForge-CSRF'), '1')
    assert.equal(calls[2].url, '/api/v1/devices/pairing-codes')
    assert.equal(calls[2].init?.method, 'POST')
    assert.equal(new Headers(calls[2].init?.headers).get('X-ThreadForge-CSRF'), '1')
    assert.equal(calls[3].url, '/api/v1/devices/dev_a%2Fb')
    assert.equal(calls[3].init?.method, 'DELETE')
    assert.equal(new Headers(calls[3].init?.headers).get('X-ThreadForge-CSRF'), '1')
    assert.equal(calls[4].url, '/api/v1/devices/dev_model/model-config')
    assert.equal(calls[4].init?.method, 'PUT')
    assert.deepEqual(JSON.parse(String(calls[4].init?.body)), {
      base_url: 'https://provider.example/v1',
      api_key: 'secret',
      model: 'model-a',
    })
  })

  it('loads the online Worker pool for future multi-Worker routing', async () => {
    globalThis.fetch = async (input) => {
      assert.equal(String(input), '/api/v1/workers/online?capability=workspace_selection')
      return response({
        items: [
          {
            worker_id: 'dev_a',
            device_id: 'dev_a',
            name: 'Laptop',
            online: true,
            version: '0.2.5',
            protocol_version: 1,
            platform: 'windows',
            architecture: 'x86_64',
            compatible: true,
            capabilities: ['workspace_selection'],
            workspaces: [],
          },
        ],
        routing: { mode: 'single', multi_worker: 'reserved' },
      })
    }

    const result = await listOnlineWorkers('workspace_selection')
    assert.equal(result.routing.multi_worker, 'reserved')
    assert.equal(result.items[0]?.worker_id, 'dev_a')
  })

  it('downloads the same-origin Worker bundle with progress', async () => {
    const progress: Array<[number, number]> = []
    globalThis.fetch = async (input, init) => {
      assert.equal(String(input), '/api/v1/worker/releases/download/windows-x86_64')
      assert.equal(init?.credentials, 'include')
      return new Response(new Uint8Array([1, 2, 3, 4]), {
        headers: {
          'Content-Length': '4',
          'Content-Disposition': 'attachment; filename="worker.exe"',
        },
      })
    }

    const result = await downloadWorkerRelease('windows-x86_64', (received, total) => {
      progress.push([received, total])
    })

    assert.equal(result.filename, 'worker.exe')
    assert.equal(result.blob.size, 4)
    assert.deepEqual(progress.at(-1), [4, 4])
  })

  it('requests signed Worker release metadata', async () => {
    globalThis.fetch = async (input) => {
      assert.equal(String(input), '/api/v1/worker/releases/latest')
      return response({ version: '0.2.0' })
    }
    assert.equal((await getLatestWorkerRelease()).version, '0.2.0')
  })
})

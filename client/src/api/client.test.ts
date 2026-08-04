import assert from 'node:assert/strict'
import { afterEach, describe, it } from 'node:test'
import {
  ApiError,
  getAuthStatus,
  getRuntimeConfig,
  listMcpServers,
  listSkills,
  logout,
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

    assert.equal(calls[0].url, '/api/v1/auth/status')
    assert.equal(calls[0].init?.credentials, 'include')
    assert.equal(calls[1].url, '/api/v1/auth/logout')
    assert.equal(calls[1].init?.credentials, 'include')
    assert.equal(new Headers(calls[1].init?.headers).get('X-ThreadForge-CSRF'), '1')
  })
})

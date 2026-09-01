import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { providerModelIds } from './model-options.ts'
import { inputTimelineEvent } from './traceModel.ts'

describe('provider model options', () => {
  it('uses the configured model when discovery returns an empty catalog', () => {
    assert.deepEqual(providerModelIds({ models: [], model: 'gpt-5.6-sol' }), ['gpt-5.6-sol'])
  })

  it('prefers discovered models over the configured default', () => {
    assert.deepEqual(providerModelIds({ models: ['a', 'b'], model: 'fallback' }), ['a', 'b'])
  })

  it('returns no models when neither source is configured', () => {
    assert.deepEqual(providerModelIds({ models: [], model: '  ' }), [])
    assert.deepEqual(providerModelIds(undefined), [])
  })

  it('projects the user request as an audit timeline event', () => {
    const event = inputTimelineEvent({ id: 'm-user-1', content: '你好', createdAt: '2026-09-01T10:00:00Z' })
    assert.deepEqual(event, {
      event_id: 'input:m-user-1',
      type: 'user.input',
      timestamp: '2026-09-01T10:00:00Z',
      label: '用户输入',
      status: 'completed',
      text: '你好',
      message_id: 'm-user-1',
      source: 'input',
    })
  })
})

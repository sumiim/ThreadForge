import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { providerModelIds } from './model-options.ts'

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
})

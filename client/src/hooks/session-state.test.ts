import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { SessionTask } from '../api/types'
import { getFinalAnswer, getLatestTask } from './session-state.ts'

function task(taskId: string): SessionTask {
  return {
    task_id: taskId,
    run_id: `run-${taskId}`,
    status: 'completed',
    input: 'input',
    final_answer: 'answer',
    stop_reason: null,
    created_at: '2026-08-04T00:00:00Z',
    updated_at: '2026-08-04T00:00:00Z',
  }
}

describe('session task recovery', () => {
  it('uses the first task because the API returns newest first', () => {
    assert.equal(getLatestTask([task('newest'), task('oldest')])?.task_id, 'newest')
    assert.equal(getLatestTask([]), undefined)
  })

  it('recovers a non-empty final answer from a terminal snapshot', () => {
    assert.equal(getFinalAnswer({ status: 'completed', final_answer: 'done' }), 'done')
    assert.equal(getFinalAnswer({ status: 'cancelled', final_answer: '' }), null)
    assert.equal(getFinalAnswer({ status: 'failed' }), null)
  })
})

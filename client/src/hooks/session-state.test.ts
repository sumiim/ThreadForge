import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { SessionTask } from '../api/types'
import {
  getFinalAnswer,
  getLatestTask,
  historyAllowsSending,
  isInternalReviewDiagnostic,
  reconcileToolCalls,
  resolveHistoryStatus,
} from './session-state.ts'

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
  it('keeps unloaded history out of the empty state and send path', () => {
    const loaded = new Set(['loaded-session'])
    const failed = new Set(['failed-session'])

    assert.equal(resolveHistoryStatus('pending-session', loaded, failed), 'loading')
    assert.equal(resolveHistoryStatus('failed-session', loaded, failed), 'error')
    assert.equal(resolveHistoryStatus('loaded-session', loaded, failed), 'loaded')
    assert.equal(historyAllowsSending(false, 'loading'), false)
    assert.equal(historyAllowsSending(false, 'error'), false)
    assert.equal(historyAllowsSending(false, 'loaded'), true)
    assert.equal(historyAllowsSending(true, 'loading'), true)
  })

  it('uses the first task because the API returns newest first', () => {
    assert.equal(getLatestTask([task('newest'), task('oldest')])?.task_id, 'newest')
    assert.equal(getLatestTask([]), undefined)
  })

  it('recovers a non-empty final answer from a terminal snapshot', () => {
    assert.equal(getFinalAnswer({ status: 'completed', final_answer: 'done' }), 'done')
    assert.equal(getFinalAnswer({ status: 'cancelled', final_answer: '' }), null)
    assert.equal(getFinalAnswer({ status: 'failed' }), null)
    assert.equal(getFinalAnswer({ status: 'failed', final_answer: 'status: needs_fix' }), null)
    assert.equal(getFinalAnswer({ status: 'completed', final_answer: 'status: needs_fix\nretry' }), null)
  })

  it('recognizes leaked review diagnostics without hiding normal answers', () => {
    assert.equal(isInternalReviewDiagnostic('status: needs_fix candidate issue'), true)
    assert.equal(isInternalReviewDiagnostic('{"status":"pass","text":"ok"}'), true)
    assert.equal(isInternalReviewDiagnostic('The status: needs_fix value is internal.'), false)
    assert.equal(isInternalReviewDiagnostic('done'), false)
  })

  it('reconciles a missed tool completion when the task completed normally', () => {
    const tools = reconcileToolCalls(
      [{ id: 'call-1', toolName: 'read_file', status: 'running' }],
      'completed',
    )

    assert.equal(tools?.[0].status, 'completed')
    assert.equal(tools?.[0].result, '工具已执行完成')
  })

  it('keeps unfinished tools as errors when the task failed', () => {
    const tools = reconcileToolCalls(
      [{ id: 'call-1', toolName: 'read_file', status: 'running' }],
      'failed',
    )

    assert.equal(tools?.[0].status, 'error')
    assert.equal(tools?.[0].result, '任务已停止，工具未完成')
  })
})

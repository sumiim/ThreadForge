import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { SessionTask } from '../api/types'
import {
  applyToolEvent,
  getFinalAnswer,
  getLatestTask,
  historyAllowsSending,
  isInternalReviewDiagnostic,
  reconcileToolCalls,
  resolveHistoryStatus,
  terminalFailureMessage,
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

  it('marks unfinished tools as cancelled when the user stops the task', () => {
    const tools = reconcileToolCalls(
      [{ id: 'call-1', toolName: 'read_file', status: 'running' }],
      'cancelled',
    )

    assert.equal(tools?.[0].status, 'rejected')
    assert.equal(tools?.[0].result, '任务已取消，工具未完成')
  })

  it('records a fast tool completion even when the requested card is not committed yet', () => {
    const tools = applyToolEvent(undefined, {
      id: 'call-fast',
      toolName: 'list_files',
      status: 'completed',
      result: '[F] README.md',
    })

    assert.deepEqual(tools, [{
      id: 'call-fast',
      toolName: 'list_files',
      status: 'completed',
      args: undefined,
      result: '[F] README.md',
    }])
  })

  it('preserves tool arguments while later events update its status', () => {
    const requested = applyToolEvent(undefined, {
      id: 'call-1',
      toolName: 'search',
      status: 'running',
      args: { path: '.', pattern: 'client|api-server' },
    })
    const completed = applyToolEvent(requested, {
      id: 'call-1',
      toolName: 'search',
      status: 'completed',
      result: 'README.md:1:client',
    })

    assert.deepEqual(completed?.[0].args, { path: '.', pattern: 'client|api-server' })
    assert.equal(completed?.[0].status, 'completed')
  })

  it('uses the actual stop reason instead of labeling every blocked run as a completion gate', () => {
    assert.equal(
      terminalFailureMessage({ status: 'blocked', stop_reason: 'retry_limit_reached' }),
      '模型输出未通过执行协议校验，达到重试上限后停止。',
    )
    assert.equal(
      terminalFailureMessage({ status: 'blocked', stop_reason: 'budget_exhausted' }),
      '本次运行已达到时间、步骤或令牌预算，请缩小任务范围后重试。',
    )
    assert.equal(
      terminalFailureMessage({ status: 'blocked', stop_reason: 'completion_gate_failed' }),
      '运行结果未满足计划中的全部完成条件，请根据当前进度重试。',
    )
    assert.equal(
      terminalFailureMessage({ status: 'blocked', stop_reason: 'convergence_guard_triggered' }),
      '模型未能通过审查或持续产生有效进展，本次运行已停止空转。',
    )
  })
})

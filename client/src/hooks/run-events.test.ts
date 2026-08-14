import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import type { RunEventEnvelope, RunIndexItem } from '../api/types'
import {
  isTerminalEventType,
  mergeEvents,
  mergeRunIndex,
  terminalStatus,
  terminalStatusOf,
} from './run-events.ts'

function envelope(sequence: number, type: string, eventId = `evt_${sequence}`): RunEventEnvelope {
  return {
    event_id: eventId,
    sequence,
    type,
    task_id: 'task_1',
    run_id: 'run_1',
    timestamp: `2026-08-14T00:00:${String(sequence).padStart(2, '0')}Z`,
    data: {},
  }
}

function indexItem(eventId: string, type: string, timestamp: string): RunIndexItem {
  return { event_id: eventId, type, timestamp, label: type }
}

describe('unified event merge', () => {
  it('deduplicates the same event_id on SSE replay', () => {
    const merged = mergeEvents([envelope(1, 'task.started')], [envelope(1, 'task.started')])
    assert.equal(merged.length, 1)
  })

  it('sorts out-of-order events by sequence', () => {
    const merged = mergeEvents(
      [envelope(1, 'task.started'), envelope(5, 'task.completed')],
      [envelope(3, 'tool.started')],
    )
    assert.deepEqual(merged.map((event) => event.sequence), [1, 3, 5])
  })

  it('does not reset state when incoming is empty', () => {
    const existing = [envelope(1, 'task.started')]
    assert.equal(mergeEvents(existing, []), existing)
    assert.equal(mergeRunIndex([indexItem('a', 'tool.started', 't1')], []).length, 1)
  })

  it('merges snapshot run_index idempotently by event_id', () => {
    const merged = mergeRunIndex(
      [indexItem('a', 'tool.started', '2026-08-14T00:00:01Z')],
      [indexItem('a', 'tool.started', '2026-08-14T00:00:01Z'), indexItem('b', 'task.completed', '2026-08-14T00:00:03Z')],
    )
    assert.deepEqual(merged.map((item) => item.event_id), ['a', 'b'])
  })

  it('does not misreport an incomplete run as success', () => {
    assert.equal(terminalStatus([envelope(1, 'task.started'), envelope(2, 'tool.started')]), null)
    assert.equal(terminalStatus([envelope(1, 'task.started'), envelope(2, 'task.completed')]), 'completed')
    assert.equal(terminalStatus([envelope(1, 'task.failed')]), 'failed')
  })

  it('recognizes terminal event types and their status', () => {
    assert.equal(isTerminalEventType('task.completed'), true)
    assert.equal(isTerminalEventType('tool.completed'), false)
    assert.equal(terminalStatusOf('task.cancelled'), 'cancelled')
    assert.equal(terminalStatusOf('task.started'), null)
  })
})

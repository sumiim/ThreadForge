import type { SessionTask } from '../api/types'

// api-server returns session tasks newest first (mtime DESC).
export function getLatestTask(tasks: SessionTask[]): SessionTask | undefined {
  return tasks[0]
}

export function getFinalAnswer(data: Record<string, unknown>): string | null {
  return typeof data.final_answer === 'string' && data.final_answer.length > 0
    ? data.final_answer
    : null
}

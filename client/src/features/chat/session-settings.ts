import type { PermissionMode, ReasoningEffort } from '../../api/types'

/** §会话级配置持久化（2026-09-03）：每个 session 记住主循环 provider/model/推理档、
 * review provider/model/推理档 和 审批模式，刷新/切换会话后恢复。
 *
 * localStorage 按 sessionId 记一份 JSON 映射 { [sessionId]: SessionSettings }。
 * （进阶可升级为 server session 存储，跨设备/浏览器一致。）
 */

export interface SessionSettings {
  modelId?: string
  providerId?: string
  reasoningEffort?: ReasoningEffort
  reviewProviderId?: string | null
  reviewModelId?: string | null
  reviewReasoningEffort?: ReasoningEffort
  permissionMode?: PermissionMode
}

const KEY = 'threadforge.sessionSettings'

function readMap(): Record<string, SessionSettings> {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, unknown>
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, SessionSettings>) : {}
  } catch {
    return {}
  }
}

function writeMap(map: Record<string, SessionSettings>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(map))
  } catch {
    // 忽略（隐私模式 / 失败）
  }
}

export function loadSessionSettings(sessionId?: string): SessionSettings {
  if (!sessionId) return {}
  return readMap()[sessionId] ?? {}
}

export function saveSessionSettings(sessionId: string | undefined, settings: Partial<SessionSettings>): void {
  if (!sessionId) return
  const map = readMap()
  map[sessionId] = { ...(map[sessionId] ?? {}), ...settings }
  writeMap(map)
}

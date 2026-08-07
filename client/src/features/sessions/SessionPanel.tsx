import { useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Collapse, Input, Spin } from 'antd'
import {
  ApiOutlined,
  MoonOutlined,
  PlusOutlined,
  RightOutlined,
  SearchOutlined,
  SettingOutlined,
  SunOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import Logo from '../../components/Logo'
import type { Session, Workspace } from '../../api/types'
import type { ThemeMode } from '../../hooks/useTheme'
import NewSessionModal from './NewSessionModal'
import { sessionWorkspaceKey, workspaceDeviceKey, workspaceDeviceLabel, workspaceKey } from './workspaceIdentity'

export type PanelView = 'chat' | 'skills' | 'mcp'

interface SessionPanelProps {
  sessions: Session[]
  activeId: string | null
  activeView: PanelView
  workspaces: Workspace[]
  loading: boolean
  onSelect: (id: string) => void
  onCreate: (workspaceId: string, deviceId?: string) => void
  onNavigate: (view: PanelView) => void
  onOpenSettings: () => void
  onWorkspacesChanged?: () => Promise<Workspace[]> | Workspace[]
  onRenameDevice: (deviceId: string, displayName: string) => Promise<void>
  onRenameWorkspace: (deviceId: string, workspaceId: string, displayName: string) => Promise<void>
  onRenameSession: (sessionId: string, displayName: string) => Promise<void>
  themeMode: ThemeMode
  onToggleTheme: () => void
}

// 侧边栏：设备/Worker -> 工作区 -> 会话，并保留搜索和扩展导航。
export default function SessionPanel({
  sessions,
  activeId,
  activeView,
  workspaces,
  loading,
  onSelect,
  onCreate,
  onNavigate,
  onOpenSettings,
  onWorkspacesChanged,
  onRenameDevice,
  onRenameWorkspace,
  onRenameSession,
  themeMode,
  onToggleTheme,
}: SessionPanelProps) {
  const [query, setQuery] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const available = workspaces.find((w) => w.available)
  const [selectedWorkspaceKey, setSelectedWorkspaceKey] = useState<string | null>(
    () => (available ? workspaceKey(available) : null),
  )
  const effectiveWorkspace = workspaces.find(
    (workspace) => workspace.available && workspaceKey(workspace) === selectedWorkspaceKey,
  ) ?? available ?? null

  const visibleSessions = sessions.filter((session) => !session.draft)
  const filtered = query.trim()
    ? visibleSessions.filter((s) => s.title.toLowerCase().includes(query.trim().toLowerCase()))
    : visibleSessions
  const deviceGroups = buildDeviceGroups(workspaces, filtered, !query.trim())

  const handleCreate = () => {
    if (effectiveWorkspace) onCreate(effectiveWorkspace.workspace_id, effectiveWorkspace.device_id)
    setModalOpen(false)
  }

  return (
    <div className="flex h-full flex-col">
      {/* 品牌区 */}
      <div className="flex items-center gap-2.5 px-5 pb-4 pt-5">
        <Logo size={22} />
        <span className="text-[15px] font-semibold tracking-tight text-stone-900">ThreadForge</span>
      </div>

      {/* 搜索会话 */}
      <div className="px-4 pb-2.5">
        <Input
          variant="filled"
          prefix={<SearchOutlined className="text-stone-400" />}
          placeholder="搜索会话"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {/* 新建会话（与导航/会话项统一 40px 高） */}
      <div className="px-4 pb-2.5">
        <Button
          block
          icon={<PlusOutlined />}
          onClick={() => setModalOpen(true)}
          style={{ height: 40 }}
          className="border-stone-300 text-stone-600 hover:!border-blue-600 hover:!text-blue-700"
        >
          新建会话
        </Button>
      </div>

      {/* Skills / MCP 导航：点击切换主区域子页面 */}
      <div className="px-4 pb-3">
        <div className="space-y-0.5">
          <NavItem
            icon={<ThunderboltOutlined />}
            label="Skills"
            active={activeView === 'skills'}
            onClick={() => onNavigate('skills')}
          />
          <NavItem
            icon={<ApiOutlined />}
            label="MCP"
            active={activeView === 'mcp'}
            onClick={() => onNavigate('mcp')}
          />
        </div>
      </div>

      {/* 会话列表：标题分隔线 + 与导航项一致的 40px 行高与选中样式 */}
      <div className="flex items-center gap-2 px-3 pb-1 pt-1">
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-stone-400">
          会话
        </span>
        <span className="h-px min-w-0 flex-1 bg-stone-200" aria-hidden />
      </div>
      {/* 会话树：设备/Worker -> 工作区 -> 会话 */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2">
        {loading ? (
          <div className="flex items-center justify-center gap-2 px-3 py-5 text-xs text-stone-500" role="status">
            <Spin size="small" />
            <span>正在读取历史记录...</span>
          </div>
        ) : deviceGroups.length === 0 ? (
          <div className="px-3 py-4 text-center text-xs text-stone-400">
            {visibleSessions.length === 0 ? '暂无会话，点击上方「新建会话」开始' : '未找到匹配的会话'}
          </div>
        ) : (
          <Collapse
            ghost
            className="[&_.ant-collapse-header]:!px-2 [&_.ant-collapse-content-box]:!px-2 [&_.ant-collapse-content-box]:!py-1"
            defaultActiveKey={deviceGroups.map((group) => group.key)}
            items={deviceGroups.map((device) => ({
              key: device.key,
              label: (
                <InlineName
                  value={device.label}
                  disabled={!device.deviceId}
                  className="text-xs font-semibold text-stone-700"
                  onSave={(value) => onRenameDevice(device.deviceId!, value)}
                />
              ),
              children: (
                <Collapse
                  ghost
                  className="[&_.ant-collapse-header]:!px-2 [&_.ant-collapse-content-box]:!px-0 [&_.ant-collapse-content-box]:!py-0"
                  defaultActiveKey={device.workspaces.map((group) => group.key)}
                  items={device.workspaces.map((workspace) => ({
                    key: workspace.key,
                    label: (
                      <span className="flex min-w-0 items-center gap-1 text-xs text-stone-600">
                        <InlineName
                          value={workspace.label}
                          disabled={!workspace.deviceId}
                          className="min-w-0 flex-1 truncate"
                          onSave={(value) => onRenameWorkspace(workspace.deviceId!, workspace.workspaceId, value)}
                        />
                        <span className="font-mono text-[10px] text-stone-400">{workspace.sessions.length}</span>
                        <button
                          type="button"
                          title="在此工作区新建会话"
                          aria-label={`在 ${workspace.label} 新建会话`}
                          onClick={(event) => {
                            event.preventDefault()
                            event.stopPropagation()
                            onCreate(workspace.workspaceId, workspace.deviceId)
                            onNavigate('chat')
                          }}
                          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-stone-400 hover:bg-white hover:text-blue-700"
                        >
                          <PlusOutlined className="text-[10px]" />
                        </button>
                      </span>
                    ),
                    children: workspace.sessions.length > 0 ? (
                      <div className="space-y-0.5">
                        {workspace.sessions.map((session) => {
                          const active = session.id === activeId
                          return (
                            <div
                              key={session.id}
                              role="button"
                              tabIndex={0}
                              onClick={() => {
                                onSelect(session.id)
                                onNavigate('chat')
                              }}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                  onSelect(session.id)
                                  onNavigate('chat')
                                }
                              }}
                              className={`relative flex h-10 w-full items-center rounded-xl px-3 text-left transition-all ${
                                active ? 'bg-white shadow-[0_1px_3px_rgba(28,25,23,0.1)]' : 'hover:bg-white/70'
                              }`}
                            >
                              <span className="min-w-0 flex-1">
                                <span
                                  className={`block truncate text-sm ${
                                    active ? 'font-semibold text-stone-900' : 'font-medium text-stone-700'
                                  }`}
                                >
                                  <InlineName
                                    value={session.title}
                                    className="block truncate"
                                    onSave={(value) => onRenameSession(session.id, value)}
                                  />
                                </span>
                                <span className="mt-px block font-mono text-[11px] text-stone-500">
                                  {formatTime(session.createdAt)}
                                </span>
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    ) : (
                      <div className="px-3 pb-2 text-[11px] text-stone-400">暂无会话</div>
                    ),
                  }))}
                />
              ),
            }))}
          />
        )}
      </div>

      {/* 底部：设置入口 + 主题切换 */}
      <div className="border-t border-stone-200 p-2">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onOpenSettings}
            className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-xl px-3 text-sm text-stone-600 transition-all hover:bg-white hover:text-stone-900"
          >
            <SettingOutlined className="text-stone-500" />
            设置
          </button>
          <button
            type="button"
            onClick={onToggleTheme}
            title={themeMode === 'dark' ? '切换到白天模式' : '切换到黑夜模式'}
            aria-label={themeMode === 'dark' ? '切换到白天模式' : '切换到黑夜模式'}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-stone-500 transition-all hover:bg-white hover:text-stone-900"
          >
            {themeMode === 'dark' ? <SunOutlined /> : <MoonOutlined />}
          </button>
        </div>
      </div>

      {/* 新建会话：选择工作区（GET /api/v1/workspaces 返回的可用工作区） */}
      <NewSessionModal
        open={modalOpen}
        workspaces={workspaces}
        selected={effectiveWorkspace}
        onSelect={(workspace) => setSelectedWorkspaceKey(workspaceKey(workspace))}
        onCreate={handleCreate}
        onCancel={() => setModalOpen(false)}
        onOpenSettings={onOpenSettings}
        onWorkspacesChanged={onWorkspacesChanged}
      />
    </div>
  )
}

interface WorkspaceGroup {
  key: string
  label: string
  workspaceId: string
  deviceId?: string
  sessions: Session[]
}

interface DeviceGroup {
  key: string
  label: string
  deviceId?: string
  workspaces: WorkspaceGroup[]
}

function buildDeviceGroups(workspaces: Workspace[], sessions: Session[], includeEmpty: boolean): DeviceGroup[] {
  const devices = new Map<string, DeviceGroup>()
  const workspaceGroups = new Map<string, WorkspaceGroup>()

  const addWorkspace = (workspace: Workspace) => {
    const deviceKey = workspaceDeviceKey(workspace)
    let device = devices.get(deviceKey)
    if (!device) {
      device = {
        key: deviceKey,
        label: workspaceDeviceLabel(workspace),
        deviceId: workspace.device_id,
        workspaces: [],
      }
      devices.set(deviceKey, device)
    }
    const key = workspaceKey(workspace)
    let group = workspaceGroups.get(key)
    if (!group) {
      group = {
        key,
        label: workspace.display_name || workspace.name || workspace.display_path || workspace.workspace_id,
        workspaceId: workspace.workspace_id,
        deviceId: workspace.device_id,
        sessions: [],
      }
      workspaceGroups.set(key, group)
      device.workspaces.push(group)
    }
    return group
  }

  if (includeEmpty) {
    workspaces.forEach(addWorkspace)
  }

  sessions.forEach((session) => {
    const known = workspaces.find(
      (workspace) => workspaceKey(workspace) === sessionWorkspaceKey(session),
    )
    const group = addWorkspace(
      known ?? {
        workspace_id: session.workspaceId,
        name: session.workspaceId,
        display_path: session.workspaceId,
        available: false,
        is_git: false,
        execution_environment: session.executionEnvironment || 'backend_process',
        container_sandbox_enabled: false,
        device_id: session.deviceId,
        device_name: session.deviceId ? 'Worker' : undefined,
      },
    )
    group.sessions.push(session)
  })

  return Array.from(devices.values())
    .map((device) => ({
      ...device,
      workspaces: device.workspaces.filter((workspace) => includeEmpty || workspace.sessions.length > 0),
    }))
    .filter((device) => device.workspaces.length > 0)
}

function InlineName({
  value,
  onSave,
  disabled = false,
  className = '',
}: {
  value: string
  onSave: (value: string) => Promise<void>
  disabled?: boolean
  className?: string
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const [saving, setSaving] = useState(false)

  if (!editing) {
    return (
      <span
        className={className}
        onDoubleClick={(event) => {
          if (disabled) return
          event.preventDefault()
          event.stopPropagation()
          setDraft(value)
          setEditing(true)
        }}
      >
        {value}
      </span>
    )
  }

  const save = async () => {
    const normalized = draft.trim()
    if (!normalized || normalized === value) {
      setEditing(false)
      return
    }
    setSaving(true)
    try {
      await onSave(normalized)
      setEditing(false)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Input
      size="small"
      autoFocus
      value={draft}
      disabled={saving}
      maxLength={200}
      onClick={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onChange={(event) => setDraft(event.target.value)}
      onPressEnter={(event) => {
        event.stopPropagation()
        void save()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          setEditing(false)
        }
      }}
      onBlur={() => {
        if (!saving) setEditing(false)
      }}
      className="h-7 min-w-0"
      aria-label="重命名"
    />
  )
}

function NavItem({
  icon,
  label,
  active,
  onClick,
}: {
  icon: ReactNode
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex h-10 w-full items-center gap-2 rounded-xl px-3 text-left text-sm transition-all ${
        active
          ? 'bg-white font-semibold text-stone-900 shadow-[0_1px_3px_rgba(28,25,23,0.1)]'
          : 'text-stone-600 hover:bg-white/70 hover:text-stone-900'
      }`}
    >
      <span className={active ? 'text-stone-700' : 'text-stone-400'}>{icon}</span>
      <span className="flex-1">{label}</span>
      <RightOutlined className={`text-xs ${active ? 'text-stone-400' : 'text-stone-300'}`} />
    </button>
  )
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

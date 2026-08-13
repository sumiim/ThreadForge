import { useState } from 'react'
import type { ReactNode } from 'react'
import { Collapse, Dropdown, Input, Popconfirm, Spin, Tooltip } from 'antd'
import {
  ApiOutlined,
  DeleteOutlined,
  DesktopOutlined,
  FolderAddOutlined,
  FormOutlined,
  MoreOutlined,
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
import { buildDeviceGroups } from './session-groups'
import type { WorkspaceGroup } from './session-groups'
import { sessionWorkspaceKey, workspaceKey } from './workspaceIdentity'

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
  onDeleteWorkspace: (deviceId: string, workspaceId: string) => Promise<void>
  onDeleteSession: (sessionId: string) => Promise<void>
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
  onDeleteWorkspace,
  onDeleteSession,
  themeMode,
  onToggleTheme,
}: SessionPanelProps) {
  const [query, setQuery] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [modalIntent, setModalIntent] = useState<'session' | 'workspace' | 'host'>('session')
  const [preferredDeviceId, setPreferredDeviceId] = useState<string | null>(null)
  const available = workspaces.find((w) => w.available)
  const activeSession = sessions.find((session) => session.id === activeId)
  const [selectedWorkspaceKey, setSelectedWorkspaceKey] = useState<string | null>(
    () => (activeSession ? sessionWorkspaceKey(activeSession) : available ? workspaceKey(available) : null),
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
    onNavigate('chat')
  }

  const openModal = (intent: 'session' | 'workspace' | 'host', deviceId?: string) => {
    setModalIntent(intent)
    setPreferredDeviceId(deviceId ?? effectiveWorkspace?.device_id ?? null)
    setModalOpen(true)
  }

  const createFocusedSession = () => {
    if (effectiveWorkspace) {
      onCreate(effectiveWorkspace.workspace_id, effectiveWorkspace.device_id)
      onNavigate('chat')
      return
    }
    openModal('session')
  }

  const focusWorkspace = (workspace: WorkspaceGroup) => {
    setSelectedWorkspaceKey(workspace.key)
    setPreferredDeviceId(workspace.deviceId ?? null)
  }

  const selectSession = (session: Session) => {
    setSelectedWorkspaceKey(sessionWorkspaceKey(session))
    onSelect(session.id)
    onNavigate('chat')
  }

  return (
    <div className="flex h-full flex-col">
      {/* 品牌区 */}
      <div className="flex items-center gap-2.5 px-5 pb-4 pt-5">
        <Logo size={22} />
        <span className="flex-1 text-[15px] font-semibold text-stone-900">ThreadForge</span>
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { key: 'host', icon: <DesktopOutlined />, label: '添加主机' },
              { key: 'workspace', icon: <FolderAddOutlined />, label: '添加工作区' },
              { key: 'session', icon: <FormOutlined />, label: '添加会话' },
            ],
            onClick: ({ key }) => {
              if (key === 'host') openModal('host')
              else if (key === 'workspace') openModal('workspace')
              else createFocusedSession()
            },
          }}
        >
          <Tooltip title="添加">
            <button
              type="button"
              aria-label="添加"
              className="flex h-8 w-8 items-center justify-center rounded-md text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-900"
            >
              <PlusOutlined />
            </button>
          </Tooltip>
        </Dropdown>
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
      <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-2">
        {loading ? (
          <div className="flex items-center justify-center gap-2 px-3 py-5 text-xs text-stone-500" role="status">
            <Spin size="small" />
            <span>正在读取历史记录...</span>
          </div>
        ) : deviceGroups.length === 0 ? (
          <div className="px-3 py-4 text-center text-xs text-stone-400">
            {visibleSessions.length === 0 ? '暂无会话，点击右上角「添加」开始' : '未找到匹配的会话'}
          </div>
        ) : (
          <Collapse
            ghost
            className="min-w-0 [&_.ant-collapse-content-box]:!px-2 [&_.ant-collapse-content-box]:!py-1 [&_.ant-collapse-header-text]:!min-w-0 [&_.ant-collapse-header]:!min-w-0 [&_.ant-collapse-header]:!px-2"
            defaultActiveKey={deviceGroups.map((group) => group.key)}
            items={deviceGroups.map((device) => ({
              key: device.key,
              label: (
                <div className="flex min-w-0 items-center gap-1.5 pr-1">
                  <DesktopOutlined className="shrink-0 text-stone-400" />
                  <InlineName
                    value={device.label}
                    disabled={!device.deviceId}
                    className="min-w-0 flex-1 truncate text-xs font-semibold text-stone-700"
                    onSave={(value) => onRenameDevice(device.deviceId!, value)}
                  />
                  <Tooltip title={device.deviceId ? '向此设备添加工作区' : '服务器工作区由部署配置管理'}>
                    <button
                      type="button"
                      aria-label={`向 ${device.label} 添加工作区`}
                      disabled={!device.deviceId}
                      onClick={(event) => {
                        event.preventDefault()
                        event.stopPropagation()
                        if (device.deviceId) openModal('workspace', device.deviceId)
                      }}
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-800 disabled:cursor-not-allowed disabled:opacity-35"
                    >
                      <PlusOutlined className="text-[10px]" />
                    </button>
                  </Tooltip>
                  <Dropdown
                    trigger={['click']}
                    menu={{
                      items: [{ key: 'manage', label: '管理此设备' }],
                      onClick: () => onOpenSettings(),
                    }}
                  >
                    <button
                      type="button"
                      aria-label={`管理设备 ${device.label}`}
                      onClick={(event) => {
                        event.preventDefault()
                        event.stopPropagation()
                      }}
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-800"
                    >
                      <MoreOutlined />
                    </button>
                  </Dropdown>
                </div>
              ),
              children: (
                <Collapse
                  ghost
                  className="min-w-0 [&_.ant-collapse-content-box]:!px-0 [&_.ant-collapse-content-box]:!py-0 [&_.ant-collapse-header-text]:!min-w-0 [&_.ant-collapse-header]:!min-w-0 [&_.ant-collapse-header]:!px-2"
                  defaultActiveKey={device.workspaces.map((group) => group.key)}
                  items={device.workspaces.map((workspace) => ({
                    key: workspace.key,
                    label: (
                      <span
                        className="flex min-w-0 items-center gap-1 text-xs text-stone-600"
                        onClick={() => focusWorkspace(workspace)}
                      >
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
                            focusWorkspace(workspace)
                            onCreate(workspace.workspaceId, workspace.deviceId)
                            onNavigate('chat')
                          }}
                          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-stone-400 hover:bg-white hover:text-blue-700"
                        >
                          <PlusOutlined className="text-[10px]" />
                        </button>
                        {workspace.deviceId ? (
                          <Popconfirm
                            title={`删除工作区“${workspace.label}”？`}
                            description={`将永久删除该工作区的 ${workspace.sessions.length} 个 ThreadForge 会话和运行记录，但不会删除真实目录或项目文件。`}
                            okText="删除工作区"
                            okButtonProps={{ danger: true }}
                            cancelText="取消"
                            onConfirm={() => onDeleteWorkspace(workspace.deviceId!, workspace.workspaceId)}
                          >
                            <Tooltip title="删除工作区">
                              <button
                                type="button"
                                aria-label={`删除工作区 ${workspace.label}`}
                                onClick={(event) => {
                                  event.preventDefault()
                                  event.stopPropagation()
                                }}
                                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-stone-400 hover:bg-red-50 hover:text-red-600"
                              >
                                <DeleteOutlined className="text-[10px]" />
                              </button>
                            </Tooltip>
                          </Popconfirm>
                        ) : null}
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
                                selectSession(session)
                              }}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                  selectSession(session)
                                }
                              }}
                              className={`group relative flex h-10 w-full items-center rounded-xl px-3 text-left transition-all ${
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
                              <Popconfirm
                                title={`永久删除会话“${session.title}”？`}
                                description="本地会话正文和对应运行记录都会删除，且无法恢复。"
                                okText="永久删除"
                                okButtonProps={{ danger: true }}
                                cancelText="取消"
                                onConfirm={() => onDeleteSession(session.id)}
                              >
                                <Tooltip title="删除会话">
                                  <button
                                    type="button"
                                    aria-label={`删除会话 ${session.title}`}
                                    onClick={(event) => {
                                      event.preventDefault()
                                      event.stopPropagation()
                                    }}
                                    className="ml-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-stone-400 opacity-0 transition-opacity hover:bg-red-50 hover:text-red-600 focus:opacity-100 group-hover:opacity-100"
                                  >
                                    <DeleteOutlined className="text-[11px]" />
                                  </button>
                                </Tooltip>
                              </Popconfirm>
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
        intent={modalIntent}
        workspaces={workspaces}
        selected={effectiveWorkspace}
        preferredDeviceId={preferredDeviceId}
        onSelect={(workspace) => setSelectedWorkspaceKey(workspaceKey(workspace))}
        onCreate={handleCreate}
        onCancel={() => {
          setModalOpen(false)
          setPreferredDeviceId(null)
        }}
        onOpenSettings={onOpenSettings}
        onWorkspacesChanged={onWorkspacesChanged}
      />
    </div>
  )
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
    } catch {
      // 保存失败（乐观锁冲突/网络错误）：恢复原名称并退出编辑；
      // 错误原因已由 renameDevice/renameWorkspace/renameSession 层 notify.error 统一提示。
      setDraft(value)
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

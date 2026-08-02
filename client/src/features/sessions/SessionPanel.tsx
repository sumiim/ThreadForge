import { useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Input } from 'antd'
import {
  ApiOutlined,
  PlusOutlined,
  RightOutlined,
  SearchOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import Logo from '../../components/Logo'
import { WORKSPACE_OPTIONS } from '../../api/mock'
import type { Session } from '../../api/types'
import NewSessionModal from './NewSessionModal'

export type PanelView = 'chat' | 'skills' | 'mcp'

interface SessionPanelProps {
  sessions: Session[]
  activeId: string
  activeView: PanelView
  onSelect: (id: string) => void
  onCreate: (workspace: string) => void
  onNavigate: (view: PanelView) => void
  onOpenSettings: () => void
}

// 侧边栏：搜索 + 新建会话 + Skills/MCP 导航（点击切换主区域子页面）
export default function SessionPanel({
  sessions,
  activeId,
  activeView,
  onSelect,
  onCreate,
  onNavigate,
  onOpenSettings,
}: SessionPanelProps) {
  const [query, setQuery] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [selectedWorkspace, setSelectedWorkspace] = useState<string>(
    () => sessions[0]?.workspace ?? WORKSPACE_OPTIONS[0],
  )

  const filtered = query.trim()
    ? sessions.filter((s) => s.title.toLowerCase().includes(query.trim().toLowerCase()))
    : sessions

  const handleCreate = () => {
    onCreate(selectedWorkspace)
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
      <div className="min-h-0 flex-1 overflow-y-auto px-4">
        {filtered.length === 0 ? (
          <div className="px-3 py-4 text-center text-xs text-stone-400">未找到匹配的会话</div>
        ) : (
          filtered.map((session) => {
            const active = session.id === activeId
            return (
              <button
                key={session.id}
                type="button"
                onClick={() => {
                  onSelect(session.id)
                  onNavigate('chat')
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
                    {session.title}
                  </span>
                  <span className="mt-px block font-mono text-[11px] text-stone-500">
                    {formatTime(session.createdAt)}
                  </span>
                </span>
              </button>
            )
          })
        )}
      </div>

      {/* 底部：设置入口（页面左下角） */}
      <div className="border-t border-stone-200 p-2">
        <button
          type="button"
          onClick={onOpenSettings}
          className="flex h-10 w-full items-center gap-2 rounded-xl px-3 text-sm text-stone-600 transition-all hover:bg-white hover:text-stone-900"
        >
          <SettingOutlined className="text-stone-500" />
          设置
        </button>
      </div>

      {/* 新建会话：选择工作区（对应 GET /api/v1/workspaces + 路径边界） */}
      <NewSessionModal
        open={modalOpen}
        selected={selectedWorkspace}
        onSelect={setSelectedWorkspace}
        onCreate={handleCreate}
        onCancel={() => setModalOpen(false)}
      />
    </div>
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

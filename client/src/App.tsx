import { useEffect, useState } from 'react'
import { Avatar, Button, ConfigProvider, Drawer, Dropdown, Input, Layout, Spin, Tag } from 'antd'
import {
  AuditOutlined,
  FolderOpenOutlined,
  LogoutOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import type { AuthStatus } from './api/types'
import { useSessions } from './hooks/useSessions'
import { useTheme } from './hooks/useTheme'
import { themeConfig, darkThemeConfig } from './styles/theme'
import SessionPanel, { type PanelView } from './features/sessions/SessionPanel'
import ChatView from './features/chat/ChatView'
import RunTracePanel from './features/chat/RunTracePanel'
import SkillsView from './features/skills/SkillsView'
import McpView from './features/mcp/McpView'
import WorkerDevices from './features/devices/WorkerDevices'
import { getLatestWorkerRelease } from './api/client'
import { workspaceKey } from './features/sessions/workspaceIdentity'

const { Header, Sider, Content } = Layout

// 对话工作台：左列表 + 右对话
// 右上角：dev 标签 + 工作区路径；左下角（侧边栏底部）：设置入口（模型切换）+ 主题切换
interface AppProps {
  auth: AuthStatus
  onLogout: () => void
  signingOut: boolean
}

export default function App({ auth, onLogout, signingOut }: AppProps) {
  const {
    sessions,
    activeId,
    active,
    workspaces,
    runtimeConfig,
    skills,
    mcpServers,
    loading,
    historyStatus,
    running,
    stopping,
    agentProgress,
    refreshWorkspaces,
    retryHistory,
    select,
    createSession,
    sendMessage,
    approveTool,
    rejectTool,
    stopRun,
    selectRun,
    renameDevice,
    renameWorkspace,
    renameSession,
    deleteSession,
    deleteWorkspace,
  } = useSessions()
  const { mode, toggle: toggleTheme } = useTheme()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [view, setView] = useState<PanelView>('chat')
  const hasRun = active?.lastRunId != null
  const pageTitle = view === 'chat' ? (active?.title ?? 'ThreadForge') : view === 'skills' ? 'Skills' : 'MCP'
  const activePath = active
    ? (workspaces.find((w) => workspaceKey(w) === workspaceKey({
        workspace_id: active.workspaceId,
        device_id: active.deviceId,
        execution_environment: active.executionEnvironment,
      }))?.display_path ?? active.workspaceId)
    : ''
  const activeWorkspace = active
    ? workspaces.find((workspace) => workspaceKey(workspace) === workspaceKey({
        workspace_id: active.workspaceId,
        device_id: active.deviceId,
        execution_environment: active.executionEnvironment,
      }))
    : undefined
  const executionLabel = activeWorkspace?.execution_environment === 'local_worker'
    ? `${activeWorkspace.device_name ?? '本地设备'} · 本地 Worker`
    : runtimeConfig?.container_sandbox_enabled
      ? '独立容器沙盒'
      : '后端进程 · 无独立容器沙盒'
  const modelScope = activeWorkspace?.execution_environment === 'local_worker'
    ? `${activeWorkspace.device_name ?? '本地设备'} · Worker 默认`
    : activeWorkspace
      ? 'API Server 默认'
      : '未选择会话'

  useEffect(() => {
    void getLatestWorkerRelease().catch(() => undefined)
  }, [])

  return (
    // 主题 ConfigProvider：antd 组件随亮/暗切换；Tailwind 侧由 .dark 类变量色板接管
    <ConfigProvider theme={mode === 'dark' ? darkThemeConfig : themeConfig}>
      <Layout className="h-screen">
        <Sider
          width={280}
          theme={mode === 'dark' ? 'dark' : 'light'}
          className="border-r border-stone-200"
        >
          <SessionPanel
            sessions={sessions}
            activeId={activeId}
            activeView={view}
            workspaces={workspaces}
            loading={loading}
            onSelect={select}
            onCreate={createSession}
            onNavigate={setView}
            onOpenSettings={() => setSettingsOpen(true)}
            onWorkspacesChanged={refreshWorkspaces}
            onRenameDevice={renameDevice}
            onRenameWorkspace={renameWorkspace}
            onRenameSession={renameSession}
            onDeleteWorkspace={deleteWorkspace}
            onDeleteSession={deleteSession}
            themeMode={mode}
            onToggleTheme={toggleTheme}
          />
        </Sider>

        <Layout>
          <Header className="flex items-center justify-between">
            <div className="truncate text-sm font-medium text-stone-800">{pageTitle}</div>
            <div className="flex min-w-0 shrink-0 items-center gap-2 font-mono text-[11px]">
              <Tag icon={<SafetyCertificateOutlined />} color={runtimeConfig?.container_sandbox_enabled ? 'green' : 'warning'}>
                {executionLabel}
              </Tag>
              <Button
                type="text"
                size="small"
                icon={<AuditOutlined />}
                disabled={!hasRun}
                onClick={() => setTraceOpen(true)}
                className="font-mono text-[11px] text-stone-500"
              >
                运行审计
              </Button>
              <div className="flex min-w-0 items-center gap-1.5" title={activePath}>
                <FolderOpenOutlined className="shrink-0 text-stone-400" />
                <span className="max-w-[280px] truncate text-stone-500">{activePath}</span>
              </div>
              {auth.user ? (
                <Dropdown
                  trigger={['click']}
                  menu={{
                    items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
                    onClick: ({ key }) => {
                      if (key === 'logout') onLogout()
                    },
                  }}
                >
                  <Button type="text" size="small" loading={signingOut}>
                    <Avatar size={20} src={auth.user.avatar_url || undefined}>
                      {auth.user.login.slice(0, 1).toUpperCase()}
                    </Avatar>
                    <span className="max-w-28 truncate">@{auth.user.login}</span>
                  </Button>
                </Dropdown>
              ) : null}
            </div>
          </Header>
          <Content className="min-h-0">
            {traceOpen ? (
              <RunTracePanel
                open
                session={active}
                activeRunId={active?.activeRunId}
                provider={activeWorkspace?.model_capabilities?.provider}
                onClose={() => setTraceOpen(false)}
              />
            ) : view === 'chat' ? (
              active ? (
                <ChatView
                  session={active}
                  historyStatus={historyStatus}
                  running={running}
                  agentProgress={agentProgress}
                  onSend={sendMessage}
                  onRetryHistory={retryHistory}
                  stopping={stopping}
                  onStop={stopRun}
                  onSelectRun={selectRun}
                  onApprove={approveTool}
                  onReject={rejectTool}
                />
              ) : loading ? (
                <div className="flex h-full flex-col items-center justify-center gap-3 text-sm text-stone-500" role="status">
                  <Spin size="large" />
                  <span>正在读取历史记录...</span>
                </div>
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-stone-500">
                  还没有会话，点击左侧「新建会话」开始
                </div>
              )
            ) : view === 'skills' ? (
              <SkillsView items={skills} />
            ) : (
              <McpView items={mcpServers} />
            )}
          </Content>
        </Layout>

        <Drawer title="设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} size={320}>
          <div className="space-y-5">
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <div className="text-sm font-medium text-stone-800">当前会话模型</div>
                <Tag className="!mr-0">{modelScope}</Tag>
              </div>
              <Input value={activeWorkspace?.model ?? runtimeConfig?.model ?? '正在读取后端配置'} readOnly />
              <p className="mt-1.5 text-xs text-stone-500">
                {activeWorkspace?.execution_environment === 'local_worker'
                  ? (activeWorkspace.model_configured
                      ? '该配置只作用于这台 Worker 上的工作区，不影响其他设备。'
                      : '当前 Worker 尚未配置模型密钥。')
                  : (runtimeConfig?.model_configured
                      ? '该配置作用于 API Server 直接执行的工作区。'
                      : '后端尚未配置模型密钥，无法启动任务。')}
              </p>
            </div>
            <div>
              <div className="mb-1.5 text-sm font-medium text-stone-800">执行边界</div>
              <p className="text-xs text-stone-500">{executionLabel}。工具会直接作用于已授权工作区。</p>
            </div>
            <WorkerDevices />
          </div>
        </Drawer>

      </Layout>
    </ConfigProvider>
  )
}

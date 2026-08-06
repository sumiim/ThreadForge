import { useEffect, useState } from 'react'
import { Avatar, Button, ConfigProvider, Drawer, Dropdown, Input, Layout, Modal, Tag } from 'antd'
import {
  FileTextOutlined,
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
import RunArtifactsDrawer from './features/chat/RunArtifactsDrawer'
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
    running,
    agentProgress,
    refreshWorkspaces,
    select,
    createSession,
    sendMessage,
    approveTool,
    rejectTool,
    stopRun,
  } = useSessions()
  const { mode, toggle: toggleTheme } = useTheme()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false)
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
            onSelect={select}
            onCreate={createSession}
            onNavigate={setView}
            onOpenSettings={() => setSettingsOpen(true)}
            onWorkspacesChanged={refreshWorkspaces}
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
                icon={<FileTextOutlined />}
                disabled={!hasRun}
                onClick={() => setArtifactsOpen(true)}
                className="font-mono text-[11px] text-stone-500"
              >
                运行结果
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
            {view === 'chat' ? (
              active ? (
                <ChatView
                  session={active}
                  running={running}
                  agentProgress={agentProgress}
                  onSend={sendMessage}
                  onStop={() => setStopConfirmOpen(true)}
                  onApprove={approveTool}
                  onReject={rejectTool}
                />
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
              <div className="mb-1.5 text-sm font-medium text-stone-800">模型</div>
              <Input value={runtimeConfig?.model ?? '正在读取后端配置'} readOnly />
              <p className="mt-1.5 text-xs text-stone-500">
                {runtimeConfig?.model_configured ? '模型已由后端配置。' : '后端尚未配置模型密钥，无法启动任务。'}
              </p>
            </div>
            <div>
              <div className="mb-1.5 text-sm font-medium text-stone-800">执行边界</div>
              <p className="text-xs text-stone-500">{executionLabel}。工具会直接作用于已授权工作区。</p>
            </div>
            <WorkerDevices />
          </div>
        </Drawer>

        {/* 运行结果：task_state / trace / report（GET /api/v1/runs/{run_id}/artifacts） */}
        <RunArtifactsDrawer
          open={artifactsOpen}
          session={active}
          onClose={() => setArtifactsOpen(false)}
        />

        <Modal
          title="停止当前任务？"
          open={stopConfirmOpen && running}
          okText="停止任务"
          okButtonProps={{ danger: true }}
          cancelText="继续运行"
          onOk={() => {
            setStopConfirmOpen(false)
            stopRun()
          }}
          onCancel={() => setStopConfirmOpen(false)}
          afterClose={() => setStopConfirmOpen(false)}
        >
          当前模型请求或工具执行将被取消，未完成的结果不会保留为最终回答。
        </Modal>
      </Layout>
    </ConfigProvider>
  )
}

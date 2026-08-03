import { useState } from 'react'
import { Button, ConfigProvider, Drawer, Layout, Select } from 'antd'
import { FileTextOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { MODEL_OPTIONS } from './api/constants'
import { useSessions } from './hooks/useSessions'
import { useTheme } from './hooks/useTheme'
import { themeConfig, darkThemeConfig } from './styles/theme'
import SessionPanel, { type PanelView } from './features/sessions/SessionPanel'
import ChatView from './features/chat/ChatView'
import RunArtifactsDrawer from './features/chat/RunArtifactsDrawer'
import SkillsView from './features/skills/SkillsView'
import McpView from './features/mcp/McpView'

const { Header, Sider, Content } = Layout

// 对话工作台：左列表 + 右对话
// 右上角：dev 标签 + 工作区路径；左下角（侧边栏底部）：设置入口（模型切换）+ 主题切换
export default function App() {
  const {
    sessions,
    activeId,
    active,
    workspaces,
    running,
    select,
    createSession,
    sendMessage,
    approveTool,
    rejectTool,
    stopRun,
    updateModel,
  } = useSessions()
  const { mode, toggle: toggleTheme } = useTheme()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const [view, setView] = useState<PanelView>('chat')
  const hasRun = active?.lastRunId != null
  const pageTitle = view === 'chat' ? (active?.title ?? 'ThreadForge') : view === 'skills' ? 'Skills' : 'MCP'
  const activePath = active
    ? (workspaces.find((w) => w.workspace_id === active.workspaceId)?.display_path ?? active.workspaceId)
    : ''

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
            themeMode={mode}
            onToggleTheme={toggleTheme}
          />
        </Sider>

        <Layout>
          <Header className="flex items-center justify-between">
            <div className="truncate text-sm font-medium text-stone-800">{pageTitle}</div>
            <div className="flex min-w-0 shrink-0 items-center gap-2 font-mono text-[11px]">
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
            </div>
          </Header>
          <Content className="min-h-0">
            {view === 'chat' ? (
              active ? (
                <ChatView
                  session={active}
                  running={running}
                  onSend={sendMessage}
                  onStop={stopRun}
                  onApprove={approveTool}
                  onReject={rejectTool}
                />
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-stone-500">
                  还没有会话，点击左侧「新建会话」开始
                </div>
              )
            ) : view === 'skills' ? (
              <SkillsView />
            ) : (
              <McpView />
            )}
          </Content>
        </Layout>

        <Drawer title="设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} width={320}>
          <div className="space-y-5">
            <div>
              <div className="mb-1.5 text-sm font-medium text-stone-800">模型</div>
              <Select
                value={active?.model ?? MODEL_OPTIONS[0]}
                onChange={(value) => updateModel(value)}
                options={MODEL_OPTIONS.map((m) => ({ value: m, label: m }))}
                style={{ width: '100%' }}
              />
              <p className="mt-1.5 text-xs text-stone-500">模型由后端统一配置，此处仅为本地展示。</p>
            </div>
            <p className="text-xs text-stone-500">更多设置项随 V1 迭代补充。</p>
          </div>
        </Drawer>

        {/* 运行结果：task_state / trace / report（GET /api/v1/runs/{run_id}/artifacts） */}
        <RunArtifactsDrawer
          open={artifactsOpen}
          session={active}
          onClose={() => setArtifactsOpen(false)}
        />
      </Layout>
    </ConfigProvider>
  )
}

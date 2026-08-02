import { useState } from 'react'
import { Button, Drawer, Layout, Select } from 'antd'
import { FileTextOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { MODEL_OPTIONS } from './api/mock'
import { useSessions } from './hooks/useSessions'
import SessionPanel, { type PanelView } from './features/sessions/SessionPanel'
import ChatView from './features/chat/ChatView'
import RunArtifactsDrawer from './features/chat/RunArtifactsDrawer'
import SkillsView from './features/skills/SkillsView'
import McpView from './features/mcp/McpView'

const { Header, Sider, Content } = Layout

// 对话工作台：左列表 + 右对话
// 右上角：dev 标签 + 工作区路径；左下角（侧边栏底部）：设置入口（模型切换）
export default function App() {
  const {
    sessions,
    activeId,
    active,
    running,
    select,
    createSession,
    sendMessage,
    approveTool,
    rejectTool,
    stopRun,
    updateModel,
  } = useSessions()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const [view, setView] = useState<PanelView>('chat')
  const hasRun = active.messages.some((m) => (m.toolCalls?.length ?? 0) > 0)
  const pageTitle = view === 'chat' ? active.title : view === 'skills' ? 'Skills' : 'MCP'

  return (
    <Layout className="h-screen">
      <Sider width={280} theme="light" className="border-r border-stone-200">
        <SessionPanel
          sessions={sessions}
          activeId={activeId}
          activeView={view}
          onSelect={select}
          onCreate={createSession}
          onNavigate={setView}
          onOpenSettings={() => setSettingsOpen(true)}
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
            <div className="flex min-w-0 items-center gap-1.5" title={active.workspace}>
              <FolderOpenOutlined className="shrink-0 text-stone-400" />
              <span className="max-w-[280px] truncate text-stone-500">{active.workspace}</span>
            </div>
          </div>
        </Header>
        <Content className="min-h-0">
          {view === 'chat' ? (
            <ChatView
              session={active}
              running={running}
              onSend={sendMessage}
              onStop={stopRun}
              onApprove={approveTool}
              onReject={rejectTool}
            />
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
              value={active.model}
              onChange={(value) => updateModel(value)}
              options={MODEL_OPTIONS.map((m) => ({ value: m, label: m }))}
              style={{ width: '100%' }}
            />
          </div>
          <p className="text-xs text-stone-500">更多设置项随 V1 迭代补充。</p>
        </div>
      </Drawer>

      {/* 运行结果：task_state / trace / report */}
      <RunArtifactsDrawer
        open={artifactsOpen}
        session={active}
        onClose={() => setArtifactsOpen(false)}
      />
    </Layout>
  )
}

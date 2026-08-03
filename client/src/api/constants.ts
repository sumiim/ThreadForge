// 静态展示数据（无对应后端接口）
// Skills / MCP 为 V1 展示占位；模型由后端统一配置（PICO_OPENAI_MODEL），此列表仅作展示

export const MODEL_OPTIONS = ['claude-sonnet-5', 'claude-opus-5', 'deepseek-v4-flash']

// 侧边栏 Skills 栏（V1 仅展示，执行能力后续版本接入）
export const SKILL_OPTIONS = [
  { id: 'code-review', name: 'code-review', desc: '代码评审' },
  { id: 'security-review', name: 'security-review', desc: '安全审查' },
  { id: 'simplify', name: 'simplify', desc: '代码简化' },
] as const

// 侧边栏 MCP 栏（V1 仅展示连接状态，实际接入规划在后续版本）
export const MCP_OPTIONS = [
  { id: 'filesystem', name: 'filesystem', desc: '本地文件系统访问', status: 'connected' },
  { id: 'github', name: 'github', desc: 'GitHub 仓库与 Issue 操作', status: 'connected' },
  { id: 'fetch', name: 'fetch', desc: 'HTTP 抓取与网页内容读取', status: 'disconnected' },
] as const

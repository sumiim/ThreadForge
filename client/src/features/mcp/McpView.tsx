import { ApiOutlined } from '@ant-design/icons'
import { Empty } from 'antd'
import type { McpServerMetadata } from '../../api/types'

// MCP 子页面：服务器列表（V1 展示连接状态，实际接入规划在后续版本）
export default function McpView({ items }: { items: McpServerMetadata[] }) {
  return (
    <div className="h-full overflow-y-auto px-6 py-8 lg:px-10">
      <div className="mx-auto max-w-4xl">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
            <ApiOutlined />
          </span>
          <h1 className="text-lg font-semibold tracking-tight text-stone-900">MCP</h1>
        </div>
        <p className="mt-2 text-sm text-stone-500">后端声明的模型上下文协议服务器。</p>

        {items.length === 0 ? (
          <Empty className="mt-16" description="后端未返回 MCP 服务器" />
        ) : (
          <div className="mt-6 space-y-2.5">
          {items.map((mcp) => {
            const connected = mcp.connected
            return (
              <div
                key={mcp.id}
                className="flex items-center gap-3 rounded-2xl border border-stone-100 bg-stone-50 px-4 py-3.5"
              >
                <span className="text-stone-400">
                  <ApiOutlined />
                </span>
                <div className="min-w-0">
                  <div className="font-mono text-sm font-medium text-stone-800">{mcp.name}</div>
                  <div className="mt-0.5 truncate text-xs text-stone-500">{mcp.description}</div>
                </div>
                <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs text-stone-500">
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-green-500' : 'bg-stone-300'}`}
                    aria-hidden
                  />
                  {connected ? '已连接' : '未连接'}
                </span>
              </div>
            )
          })}
          </div>
        )}
      </div>
    </div>
  )
}

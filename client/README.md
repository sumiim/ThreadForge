# Client — ThreadForge Web Console

ThreadForge 的 React Web 工作台。Agent 协作约定见 [AGENTS.md](AGENTS.md)。

## V1 范围

- Session 列表与新建会话。
- Agent 任务输入和最终回答展示。
- SSE 任务状态与工具事件。
- 危险工具的批准和拒绝。
- Agent 停止按钮。
- Workspace、模型和开发模式提示。
- 后端运行边界、Skills 计划状态和 MCP 未连接状态。

## 技术栈

- pnpm
- Vite
- React 19
- TypeScript
- Ant Design v6
- TailwindCSS v4
- Electron 43

## 本地运行

```powershell
pnpm install
pnpm dev
```

Vite 默认把 `/api` 代理到 `http://127.0.0.1:8000`。需要覆盖时设置
`VITE_API_PROXY_TARGET`。

## Electron 直连

打包后的页面从 `file://` 加载，不经过 Vite 代理。启动桌面端前通过
`THREADFORGE_API_BASE_URL` 指定 loopback API 地址；后端同时显式启用桌面 Origin：

```powershell
$env:THREADFORGE_API_BASE_URL = "http://127.0.0.1:18000"
$env:THREADFORGE_DESKTOP_ORIGIN_ENABLED = "true"
```

后端的桌面 Origin 开关默认关闭。它只应在运行受信任的打包客户端时开启。

## 校验

```powershell
pnpm typecheck
pnpm lint
pnpm test
pnpm build
```

V1 不包含完整 Plan 树、Agent 树、可执行 Skills 或真实 MCP 连接；相关页面读取后端的只读兼容元数据，并明确显示为计划中或未连接。

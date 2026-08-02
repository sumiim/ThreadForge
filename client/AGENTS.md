# Client — ThreadForge Web Console

ThreadForge 的 React Web 工作台：面向本地代码仓库的 Web Coding Agent 界面，展示 Agent 对话、任务状态、工具事件，并提供工具审批与停止控制。对应后端为 `api-server`（FastAPI）。

## V1 范围

- Session 列表与新建会话。
- Agent 任务输入和最终回答展示。
- SSE 任务状态与工具事件流。
- 危险工具的逐次批准和拒绝。
- Agent 停止按钮。
- Workspace、模型和开发模式提示。
- 单用户、本机访问、单个活动任务。

V1 不包含完整 Plan 树、Agent 树、Skills 管理或桌面端打包。

## 技术栈（已确定，勿随意更换）

- pnpm —— 唯一包管理器，不混用 npm / yarn。
- Vite —— 开发与构建。
- React 19 + TypeScript（strict）。**TypeScript 固定 5.9.x**：7.x（Go 原生版）已发布，但 typescript-eslint 尚未支持，勿升。
- Ant Design v6 + `@ant-design/icons` v6 —— 业务组件（v5 的 React 19 compat patch 不需要）。
- TailwindCSS v4 —— 布局与工具类。

## antd 与 Tailwind 分工

- **antd 负责业务组件与交互**：Table、Form、Modal、消息提示、审批控件。
- **Tailwind 负责布局、间距、排版等工具类**。
- 主题定制通过 antd `ConfigProvider` 的 token 完成；**不要**用 CSS 覆盖 antd 内部 DOM 类名（v6 语义化 DOM 后内部结构可变，v7 会移除旧 API）。
- Tailwind v4 使用 `@import "tailwindcss"` 引入，与 antd 的 CSS-in-JS 共存，避免样式优先级冲突。

## 常用命令

```bash
pnpm install     # 安装依赖
pnpm dev         # 启动开发服务器
pnpm build       # 生产构建
pnpm typecheck   # tsc --noEmit
pnpm lint        # eslint
```

## 目录结构约定

```text
src/
├── api/          # REST / SSE 客户端与数据契约类型；组件不得直接裸 fetch
├── features/     # 按业务域组织：sessions、tasks、approvals 等
├── components/   # 跨业务域通用组件
├── hooks/        # 通用 hooks（含 SSE 连接管理等）
├── pages/        # 路由页面
└── styles/       # 全局样式与主题
```

## 数据与联调约定

- 所有请求封装在 `src/api/`，统一错误处理与 antd `message` / `notification` 提示。
- SSE 事件流（任务状态、工具事件）在 `src/api/` 统一封装，处理断线重连与组件卸载清理。
- 开发环境通过 Vite `server.proxy` 转发到 api-server；**SSE 端点需在代理配置中关闭缓冲**，否则事件流不实时。
- 后端地址不写死：以 api-server 实际启动端口为准，通过环境变量配置。
- 审批、停止等关键操作，交互前需用户确认。

## 代码规范

- TypeScript strict；除对接外部 JSON 的边界外，禁止 `any`。
- 函数组件 + hooks，不使用 class 组件。
- 命名：组件文件 `PascalCase.tsx`、组件名 `PascalCase`；非组件文件 kebab-case。
- 新增依赖需说明用途，优先使用栈内既有能力。

## 当前状态

脚手架已初始化：Vite 8 + React 19.2 + antd 6.5 + Tailwind 4.3，`src/` 骨架（api / components / features / hooks / pages / styles）与 Vite 代理已就位，typecheck / lint / build 通过。当前仅有占位 App，尚未实现 V1 页面。下一步：实现 Session 列表与新建会话、任务输入与 SSE 事件流。

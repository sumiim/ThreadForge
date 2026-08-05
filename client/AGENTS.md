# Client — ThreadForge Web Console

ThreadForge 的 React Web 工作台：面向本地代码仓库的 Web Coding Agent 界面，展示 Agent 对话、任务状态、工具事件，并提供工具审批与停止控制。对应后端为 `api-server`（FastAPI）。

## V1 范围

- Session 列表与新建会话。
- Agent 任务输入和最终回答展示。
- SSE 任务状态与工具事件流。
- 危险工具的逐次批准和拒绝。
- Agent 停止按钮。
- Workspace、模型和开发模式提示。
- 默认单用户；可选 GitHub OAuth 白名单多用户；本机访问、全局单个活动任务。
- Windows 桌面壳(Electron):开发与打包链路可用,窗口/外链/桥接骨架就绪。

V1 不包含完整 Plan 树、Agent 树、Skills 管理。

## 技术栈（已确定，勿随意更换）

- pnpm —— 唯一包管理器，不混用 npm / yarn。
- Vite —— 开发与构建。
- React 19 + TypeScript（strict）。**TypeScript 固定 5.9.x**：7.x（Go 原生版）已发布，但 typescript-eslint 尚未支持，勿升。
- Ant Design v6 + `@ant-design/icons` v6 —— 业务组件（v5 的 React 19 compat patch 不需要）。
- TailwindCSS v4 —— 布局与工具类。
- Electron 43 + `vite-plugin-electron`(v1)+ electron-builder —— Windows 桌面壳。**未用 electron-vite**:其对 Vite 8 的兼容滞后(peerDeps 锁 ^5~^7),本项目 Vite 8 固定。

## antd 与 Tailwind 分工

- **antd 负责业务组件与交互**：Table、Form、Modal、消息提示、审批控件。
- **Tailwind 负责布局、间距、排版等工具类**。
- 主题定制通过 antd `ConfigProvider` 的 token 完成；**不要**用 CSS 覆盖 antd 内部 DOM 类名（v6 语义化 DOM 后内部结构可变，v7 会移除旧 API）。
- Tailwind v4 使用 `@import "tailwindcss"` 引入，与 antd 的 CSS-in-JS 共存，避免样式优先级冲突。
- **形状系统（全页统一，新增组件遵循）**：控件 10px（antd token，按钮/输入/Select）> 卡片/列表项 12px（`rounded-xl`）> 容器 16px（`rounded-2xl`，Composer/气泡/页面卡片）> 微件全圆（状态点/色条/徽标）。antd 组件圆角走 token，勿单独写死。

## 常用命令

```bash
pnpm install        # 安装依赖
pnpm dev            # 启动开发服务器(纯 web,不弹桌面窗口)
pnpm dev:electron   # 桌面开发模式:dev server + Electron 窗口(依赖 postinstall 已下载 electron 二进制)
pnpm build          # 生产构建(纯 web)
pnpm build:electron # 桌面打包:构建 + electron-builder 出 NSIS 安装包到仓库根 release-desktop/
pnpm typecheck      # tsc --noEmit
pnpm lint           # eslint
pnpm test           # Node 24 原生 TypeScript 单元测试
```

桌面构建/运行说明:

- 桌面壳通过 `ELECTRON=1` 环境变量开启(`cross-env` 注入),`vite.config.ts` 中条件启用 `vite-plugin-electron` 并切相对 `base`(打包后 file:// 加载)。
- 打包依赖 `pnpm-workspace.yaml` 的 `allowBuilds`(pnpm 11 已不读 package.json 的 `pnpm` 字段),缺配置时 electron 二进制不会下载,报错后先 `pnpm rebuild electron`。
- 网络受限时:electron 二进制用 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`,builder 工具用 `ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/`。

## 目录结构约定

```text
src/
├── api/          # REST / SSE 客户端与数据契约类型；组件不得直接裸 fetch
├── features/     # 按业务域组织：sessions、tasks、approvals 等
├── components/   # 跨业务域通用组件
├── hooks/        # 通用 hooks（含 SSE 连接管理等）
├── pages/        # 路由页面
└── styles/       # 全局样式与主题
electron/         # 桌面壳:main.ts 主进程、preload.ts 桥接(preload 产物在 dist-electron/)
```

Electron 桥接(`electron/preload.ts`)经 `contextBridge` 暴露 `window.threadforge`;纯 web 环境不存在,**渲染层使用前必须判空**。

## 数据与联调约定

- 所有请求封装在 `src/api/`，统一错误处理与 antd `message` / `notification` 提示。
- SSE 事件流（任务状态、工具事件）在 `src/api/` 统一封装，处理断线重连与组件卸载清理。
- 开发环境通过 Vite `server.proxy` 转发到 api-server；**SSE 端点需在代理配置中关闭缓冲**，否则事件流不实时。
- 后端地址不写死：Web 开发代理使用 `VITE_API_PROXY_TARGET`；Web 生产构建可使用 `VITE_API_BASE_URL`。桌面生产模式可通过 `THREADFORGE_WEB_URL` 加载与 Web 相同的 HTTPS 站点（GitHub OAuth 多用户推荐），或从 `file://` 加载内置页面并由 preload 读取 `THREADFORGE_API_BASE_URL`。远端只接受 HTTPS，HTTP 仅允许 loopback；只有 `file://` 模式才需要后端显式启用 `THREADFORGE_DESKTOP_ORIGIN_ENABLED=true` 允许 `Origin: null`。
- 审批、停止等关键操作，交互前需用户确认。

## 代码规范

- TypeScript strict；除对接外部 JSON 的边界外，禁止 `any`。
- 函数组件 + hooks，不使用 class 组件。
- 命名：组件文件 `PascalCase.tsx`、组件名 `PascalCase`；非组件文件 kebab-case。
- 新增依赖需说明用途，优先使用栈内既有能力。

## 当前状态

V1 页面骨架已实现（白色调对话工作台）：

- Session 侧边栏（列表 / 新建 / workspace / model 提示）与对话区（消息流、工具调用卡片、危险操作审批、停止按钮、空态欢迎页）。
- 亮/暗主题切换（侧边栏底部设置右侧）：`useTheme`（localStorage `threadforge-theme` 持久化，缺省跟随系统）。antd 走 `darkThemeConfig`（darkAlgorithm），Tailwind 色板经 `@theme inline` 映射为 `--tf-*` CSS 变量、`.dark` 下覆盖 —— **组件新增颜色必须从现有类名选**，自定义色值需同步进变量色板。
- `useSessions` 已接入 api-server 的 Session、Task、Approval、Artifact REST 接口和 SSE 事件流。
- 模型、执行边界、Skills 计划状态和 MCP 未连接状态均从后端只读接口获取；界面不得把占位能力显示为已启用。
- GitHub OAuth 启用时，工作台在认证状态确认后挂载；写请求统一携带 CSRF 标记，Cookie 由浏览器管理，不在前端存储令牌。

桌面壳（Electron）已就绪：`pnpm dev:electron` 开发、`pnpm build:electron` 出 Windows 安装包；`pnpm dev` / `pnpm build` 保持纯 web 不变。窗口图标尚未定制（暂用 Electron 默认）。

本地 Worker V1 已接入：Web/Electron 统一经中央 API 管理设备、Session、Task、审批和事件；Worker 通过出站 WebSocket 在用户电脑执行 Pico、模型、文件、Git 与 Shell。前端会检测 Companion、尝试唤醒，并在首次使用、唤醒失败或协议不兼容时提供带进度的签名发布包下载；Worker 支持登录自启动和后台自动更新。后续版本再接入真实 Skills/MCP 执行能力。

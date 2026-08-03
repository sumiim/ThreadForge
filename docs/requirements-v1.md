# ThreadForge V1 需求基线

## 目标

V1 交付一个可在本机使用的单用户 Web Coding Agent，使用户能够选择允许的本地项目、创建会话、提交任务、观察执行事件、审批危险工具、请求停止并查看运行结果。

V1 优先复用已经通过测试的 Pico Legacy Runtime，不在第一版同时完成全部现代化改造。

## 必须实现

- React + Vite + TypeScript Web Console（client/）。
- Ant Design 控件与 TailwindCSS 布局。
- FastAPI REST API。
- SSE 任务生命周期和工具事件。
- Right Code GPT 配置与调用。
- 本地 Workspace 选择和路径边界校验。
- Session 创建、列表、详情和恢复。
- Task 创建、状态查询和最终回答。
- `write_file`、`patch_file`、`run_shell` 的逐次审批。
- 运行中停止请求；停止后不得开始新的模型或工具步骤。
- `task_state.json`、`trace.jsonl` 和 `report.json` 查询。
- 本地开发启动方式和 Linux Docker Compose。

## V1 约束

- 仅支持单用户和 loopback 访问，不提供登录或公网部署。
- Web 默认复用 `native` AgentLoop 和现有工具合同。
- 全局最多一个活动 root Task。
- 每个 Run 创建独立 Runtime 实例。
- V1 不伪造 token streaming；非流式模型只发送 `message.completed`。
- V1 继续使用 JSON SessionStore 与 RunStore，不引入数据库。
- 危险工具使用 `per_call_only`，不提供整任务自动允许。
- 当前工具在后端运行环境执行，UI 必须显示尚无独立容器沙盒。
- 对已经发出的同步模型请求不承诺立即中断；返回或超时后不得继续执行工具。

## V1 不包含

- Electron/Tauri。
- 登录、多用户、PostgreSQL 或 Redis。
- 原生 provider tool calling、四工具重构和 MCP。
- LangGraph Web 默认化、独立 Planning、多 Worker 和 Agent Swarm。
- 独立 Docker Sandbox 与网络白名单。
- 可执行 Skills API、Skills Registry 和 Skills 自进化；允许只读兼容接口明确返回 `planned/available=false`，但不得把占位能力显示为已启用。
- Git URL 克隆、ZIP 上传或运行中追加消息。

## 核心接口

```text
GET  /health/live
GET  /health/ready
GET  /api/v1/workspaces
POST /api/v1/sessions
GET  /api/v1/sessions
GET  /api/v1/sessions/{session_id}
POST /api/v1/tasks
GET  /api/v1/tasks/{task_id}
GET  /api/v1/tasks/{task_id}/events
POST /api/v1/tasks/{task_id}/cancel
POST /api/v1/tasks/{task_id}/approvals/{approval_id}
GET  /api/v1/runs/{run_id}/artifacts
```

## 验收闭环

1. 用户选择允许的 Workspace 并创建 Session。
2. 用户提交任务，后端创建 Task/Run 并调用模型。
3. Web 通过 SSE 展示任务、模型和工具状态。
4. 只读任务能够展示最终回答。
5. 危险工具会暂停并等待单次审批。
6. 批准后执行，拒绝后工具不得执行。
7. 停止请求能够阻止后续 Agent 步骤并终止活动 Shell 进程。
8. 页面刷新后能够恢复 Session、Task 最终状态和 artifacts。

## 后续候选

- V1.1：真实 token streaming、会话搜索和统一 diff。
- V1.2：SQLite 状态、可靠事件重放、Task 重试和 Run 恢复。
- V2：原生 Tool Calling、标准 Tool Registry、LangGraph Web 和 Planning。
- V3：Container Sandbox、多 Worker 和 Agent Swarm。
- V4：Skills Registry 和受控 Skills 自进化。

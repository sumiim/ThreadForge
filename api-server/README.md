# API Server

ThreadForge 的 FastAPI 服务边界，负责把 Web 请求适配到 Agent Runtime。

## V1 范围

- Workspace、Session、Task 和 Run 查询。
- REST 创建任务、审批和停止。
- SSE 生命周期、模型完成和工具事件。
- 单用户、本机访问和单个活动任务。
- 复用 Pico Legacy Runtime 的 SessionStore、RunStore 与 EventSink。

FastAPI 路由不实现 Prompt、Memory 或 Agent 决策逻辑。该模块尚未实现。

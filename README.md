# ThreadForge

ThreadForge 是一个面向本地代码仓库的 Web Coding Agent 工作台。项目目标是在保留轻量 Agent Runtime 的基础上，逐步提供 Web 会话与任务管理、实时事件流、工具审批、执行审计、容器级沙盒、多 Agent 编排和可演进 Skills。

当前仓库已完成 V1 原生 Runtime 的 FastAPI 封装和 React 工作台接入。Sandbox、可执行 Skills 和 MCP 仍属于后续版本，当前界面只展示后端明确标记为未启用的兼容元数据。

## 仓库结构

```text
ThreadForge
├── client/               React/Vite Web 工作台与 Electron 桌面壳
├── api-server/           FastAPI、REST、SSE、审批与运行恢复
├── agent-orchestrator/   LangGraph 编排、意图路由与多角色协作
├── sandbox-workers/      Docker 工具执行隔离（后续版本）
├── skills-registry/      Skills 注册、版本与演进（后续版本）
└── pico-legacy-runtime/  Pico 原生 Runtime、CLI、Memory 与评测
```

## 整体功能

ThreadForge 计划覆盖以下能力：

- 面向本地代码仓库的 Agent 对话与任务执行。
- 会话、记忆、Checkpoint 和运行恢复。
- REST API 与 SSE 事件流。
- 工具调用展示、逐次审批、拒绝和停止。
- 路径、只读权限、工具 allowlist 和审计边界。
- Conversation、Read-only、Code-change 意图路由。
- Coordinator、Research、Review 多角色工作流。
- 可重复的 benchmark、trace、TaskState 和 report。
- 后续的 Docker Sandbox、多 Worker、Agent Swarm 和 Skills 演进。

## 当前状态

| 模块 | 状态 | 当前内容 |
| --- | --- | --- |
| Pico Legacy Runtime | 已迁移 | AgentLoop、Prompt、Context、Memory、Session、Checkpoint、工具、安全策略、CLI、评测和测试 |
| Agent Orchestrator | 已迁移 | LangGraph wrapper、意图路由、Research/Execute/Review 工作流和 backend adapter |
| Web Console | V1 已接入 | Session、任务、SSE、审批、停止、运行制品与后端能力状态 |
| API Server | V1 已实现 | FastAPI REST/SSE、JSON 持久化、恢复、取消和审批 |
| Sandbox Workers | 后续版本 | 每个任务或 Worker 的独立 Docker 执行环境 |
| Skills Registry | 后续版本 | Skill Manifest、版本、评测、发布和回滚 |

## 设计来源与致谢

| 模块 | 设计来源 |
| --- | --- |
| AgentLoop、Prompt 编排、上下文管理、Memory、Session、Checkpoint 和基础工具系统 | 基于 [htxoffical/pico](https://gitee.com/htxoffical/pico) 的设计与实现继续演进 |
| LangGraph 编排、意图路由、Coordinator / Research / Review 协作、审计和双后端评测 | 来源于 [sumiim/pico-langgraph-harness](https://github.com/sumiim/pico-langgraph-harness) |
| `read / write / edit / bash` 四工具聚合和可扩展 Agent 思想 | 参考 [earendil-works/pi](https://github.com/earendil-works/pi) |
| Web 会话、任务状态、工具审批和停止交互 | 交互体验参考 Codex App，前端代码独立实现 |
| FastAPI、React、Vite、Ant Design、TailwindCSS 和 Docker | 基于对应开源项目与生态独立实现 |
| 标准化 Tool Calling | 计划参考 OpenAI Tool Calling、JSON Schema，并预留 MCP 接入 |
| Planning、多 Worker、Agent Swarm 和 Skills 自进化 | 在现有 LangGraph 与可扩展 Agent 思想上进行的 ThreadForge 自主设计 |

ThreadForge 不声称上述项目对本项目提供官方背书。各上游项目及其成果归原作者所有；复用代码继续遵循相应来源的许可与署名要求。

## 开发者快速开始

安装后端和前端依赖：

```powershell
python -m pip install -e .\pico-legacy-runtime
python -m pip install -e ".\api-server[dev]"
python -m pip install -e .\agent-orchestrator
Push-Location .\client
pnpm install
Pop-Location
```

运行 CLI：

```powershell
python -m pico run --backend native --provider openai "分析当前项目结构"
python -m pico run --backend langgraph --task-mode auto --provider openai
```

模型配置沿用 `pico-legacy-runtime/.env.example`，真实密钥不得提交到仓库。

## 文档

- [V1 需求文档](docs/requirements-v1.md)
- [Legacy Runtime README](pico-legacy-runtime/README.md)
- [Agent Harness v1 架构](pico-legacy-runtime/docs/architecture/agent-harness-v1-overview.md)
- [LangGraph 意图路由需求](pico-legacy-runtime/docs/recreation-2/REQUIREMENTS.md)

# ThreadForge 平台、扩展能力与图记忆总需求（已拆分）

> 文档状态：来源草案（Superseded，不再作为版本验收基线）
> 适用版本：`main`（当前基线 `6d40a55`，Worker `0.2.17`）
> 目标：在不破坏 V1 安全边界和本地历史所有权的前提下，补齐 Agent 平台基础，并建设工具、MCP、Skills 和 MRAgent 风格长期图记忆。

本总需求已按优先级拆分为三个独立版本文档：

- `requirements-v1.1-agent-mvp.md`：最小可用 Coding Agent 主链路。
- `requirements-v1.2-agent-hardening-and-productivity.md`：显著提升安全性、可靠性和实用性的核心增强。
- `requirements-v1.3-extension-ecosystem.md`：MCP、Skills、生态扩展和高级协作。

后续实施、CI 和完成定义以对应版本文档为准；本文仅保留拆分前的完整来源和决策上下文。

## 1. 文档定位

V1.1 是 V1 之后的平台与扩展里程碑，不是对 V1 需求的回写。V1 的核心任务仍然是本地 Worker 驱动的 Coding Agent、会话和任务生命周期、审批、审计以及多用户归属。本文同时吸收 `pico-main/docs/architecture/modernization-todolist.md` 中尚未进入 V1 的基础设施要求，避免把 LangGraph、Sandbox、调度和交付能力遗漏在四个扩展主题之外。

本文件把四类能力拆成四个独立边界：

1. **工具（Tools）**：ThreadForge 自己提供并直接执行的受限操作。
2. **MCP**：外部 MCP Server 提供的能力，必须经过 ThreadForge 的适配和授权。
3. **Skills**：可组合的 Agent 能力包，不等于单个工具，也不等于 MCP Server。
4. **长期记忆（Graph Memory）**：跨会话保存和主动重构项目事实、决策与任务经历的记忆系统。

四者都必须通过同一个 application service 和审计边界接入，不能因为引入扩展能力而绕过 Worker、workspace、owner 或审批校验。

## 2. 当前公开代码基线

以下状态以 GitHub 公开 `main` 代码为准。文档中的页面、类型或占位接口不能被当作已经具备执行能力。

### 2.1 已实现

| 领域 | 当前事实 | 主要位置 |
| --- | --- | --- |
| 内置工具 | 已有 `list_files`、`read_file`、`search`、`run_shell`、`write_file`、`patch_file` 六个受限工具；包含显式规格、参数校验、workspace 边界、风险级别、审批和执行审计。 | `pico-legacy-runtime/pico/tools.py` |
| 子 Agent | `delegate` 是受限的只读子 Agent 工具，有深度限制，不属于基础工具 allowlist。 | `pico-legacy-runtime/pico/tools.py` |
| 短期记忆 | 已有 working memory、episodic notes、文件摘要和 freshness 校验，带固定数量上限。 | `pico-legacy-runtime/pico/features/memory.py` |
| Markdown 长期记忆 | 已有 `.pico/memory/MEMORY.md` 与 `topics/*.md`，默认主题包含项目约定、关键决策、依赖事实和用户偏好；支持去重、替换和敏感信息拒绝。 | `pico-legacy-runtime/pico/features/memory.py` |
| Agent 编排 | 已有 Conversation、Read-only、Code-change 意图，以及 Coordinator、Research、Execute、Review 和 LangGraph backend adapter。 | `agent-orchestrator/` |
| LangGraph 路由 | 当前已有串行的意图识别、Research、Execute、Review 路由，支持有限的 review 修复循环和结构化意图重试。 | 尚无 CoT/Planning 提案阶段、同阶段 fan-out/fan-in 并发、可恢复的图 checkpoint 或并发写入合并。 |
| 多用户身份 | GitHub OAuth、`owner_id` 归属和对象级隔离已接入公开部署路径。 | `docs/github-oauth-setup.md`、`docs/multi-user-v15-roadmap.md` |
| Worker 归属 | 一个用户可绑定多台 Worker；Worker 可授权多个 workspace；任务固定路由到 `device_id + workspace_id`。 | `docs/local-worker-v1.md` |
| 本地历史 | Worker 本地保存会话正文、模型配置、`.env` 和运行产物；中央服务保存设备、workspace、session 索引、Task 状态和审批审计。 | `docs/local-worker-v1.md` |
| Worker 发布 | Windows 自包含 Worker、版本清单、Ed25519 签名、SHA-256 校验和私有 CD 发布链路已有约定；当前稳定制品不进入 GitHub。 | `docs/local-worker-v1.md`、`docs/public-web-deployment.md` |
| 目录授权 | Worker 已有 workspace 选择请求和一次性授权流程，但尚未提供完整的前端资源树浏览器。 | `docs/local-worker-v1.md` |

### 2.2 部分实现或仅有占位

| 领域 | 当前事实 | 不能宣称的能力 |
| --- | --- | --- |
| 工具注册 | `BASE_TOOL_SPECS` 已能描述当前工具，但尚未形成面向 builtin/MCP/Skill 的统一版本化 Registry 和 application service。 | 不能把任意外部工具直接加入现有基础 allowlist。 |
| MCP | 前端有 MCP 页面和 `GET /api/v1/mcp/servers` 元数据读取；页面主要展示连接状态。 | 没有完整 MCP Client、Server 生命周期、工具适配、凭据隔离、审批和审计闭环。 |
| Skills | 前端有 Skills 页面和 `GET /api/v1/skills` 元数据读取；`skills-registry/README.md` 明确 Registry 尚未实现。 | 没有 manifest 解析、安装/启停、依赖检查、权限过滤、评测、发布、回滚或自进化执行。 |
| LangGraph 记忆 | LangGraph 子执行器保留内存 session，并关闭父级 durable memory 写入。 | LangGraph 当前不是长期图记忆后端，也不能直接把子任务状态写入父会话。 |
| 多 Worker | 设备和 workspace 路由可用，但调度、租约、跨 Worker 并发编排尚未完成。 | 不能宣称已经支持多 Worker 任务拆分或 Agent Swarm。 |
| 关联工作区 | 尚无跨设备/跨 Worker 的统一关联工作区上下文。 | 不得把多个 workspace 自动合并，也不得跨 owner 共享上下文。 |
| 端到端加密 | 当前使用 HTTPS/WSS、设备令牌和本地权限控制。 | 尚无应用层端到端密文会话协议；中央服务仍可看到其负责的控制元数据。 |
| 资源浏览 | 当前可以注册 workspace 并返回 workspace 元数据，但没有类似 Codex 的本地资源树、只读预览、拖拽引用或受控互联网搜索/抓取工具。 | 不能宣称前端可以浏览用户电脑的全部文件或任意互联网内容。 |

### 2.3 明确未实现

- MRAgent 的 Cue–Tag–Content 图、SQLite 图存储和 FTS5 检索。
- 主动记忆重构、基于证据的图扩展/剪枝和异步记忆蒸馏流水线。
- 长期记忆的统一导出、删除、审计和跨 Worker 单写者队列。
- 类似 Codex 的本地资源树、互联网资源搜索/抓取以及前端拖拽引用。
- MCP 写入能力、Skills 自进化和 Docker Sandbox 的完整生产闭环。

## 3. 总体架构与不变量

```text
Web / Electron
      |
      | REST + SSE（身份、索引、任务、审批、事件）
      v
Central API / Application Service
      |-- owner / device / workspace authorization
      |-- Tool Registry
      |-- MCP Adapter
      |-- Skill Manager
      |-- Memory Service
      `-- Task / Approval / Event broker
      |
      | authenticated WSS
      v
Local Worker Companion
      |-- workspace files / Git / shell
      |-- local session history and model .env
      |-- tool executor
      `-- workspace-scoped memory store
```

必须保持以下不变量：

1. 浏览器和 Electron 不直接连接 Worker；中央服务负责身份、路由、审批归属和事件顺序。
2. 每个可执行请求都必须同时通过 `owner_id`、`device_id`、`workspace_id` 和任务归属校验。
3. API Key、会话正文、模型答案和本地文件内容默认只留在 Worker；中央服务只保存必要索引和控制元数据。
4. 外部 MCP 和 Skill 不能继承 Worker 的全部文件、Shell 或网络权限。
5. 扩展能力默认只读；写文件、执行命令、网络访问、发布和改变记忆都需要声明权限并按策略审批。
6. 任何记忆事实必须可追溯到来源、时间和 workspace，低置信度内容不能直接成为 durable fact。
7. V1 现有 JSON Session/Run 合同和 `native` 运行路径保持兼容；V1.1 的数据库只服务新增图记忆和扩展元数据，不隐式改写 V1 数据。

## 4. 工具系统需求

### 4.1 目标与范围

工具系统是 ThreadForge 的可信执行边界。首期稳定现有六个 builtin 工具，再以适配层承载 MCP 和 Skill 暴露的工具；不通过模型输出字符串绕过 Registry。

每个工具必须具备以下不可变合同：

```text
name
version
source: builtin | mcp | skill
input_schema / output_schema
risk_level
permission_scope
approval_policy
executor
timeout / cancellation
audit_formatter
```

### 4.2 需求

- 建立版本化 `ToolRegistry`，启动时拒绝重复名称、非法 schema 和缺失执行器。
- 将路径解析、符号链接检查、Shell 超时、子进程树终止、只读模式和审批策略统一放在 application service 边界。
- allowlist 按来源分层：builtin、每个 MCP Server、每个 Skill 分别管理；MCP/Skill 不能自动修改 builtin allowlist。
- 工具调用必须带 `trace_id`、`task_id`、`run_id`、`workspace_id`、来源和版本；审计记录输入摘要而不是秘密和完整文件内容。
- 审批必须绑定具体 `tool_call_id`，一次审批只适用于一次调用；超时、拒绝、取消和重复决策都要有确定结果。
- 统一支持取消、超时和 Worker 断线收敛；不能只停止 HTTP 请求而留下 Shell 子进程。
- 统一暴露 dry-run/能力探测接口，让前端显示“可用、需要审批、不可用”的真实状态。
- `delegate` 保持只读且有深度/步数预算；若将来升级为可写子 Agent，必须使用独立 Task、Run 和审批。

### 4.3 验收标准

- 六个 builtin 工具的旧回归全部通过，输入 schema 和风险级别可由 Registry 查询。
- 任一未注册、越界、无权限或过期版本的调用在执行前失败，并生成审计事件。
- Shell 超时会收敛整个进程树；取消后不会继续产生工具事件。
- 同一工具调用的审批结果幂等，重放 SSE 不会重复执行。
- API、CLI 和未来桌面端通过同一个 application service，不复制工具策略。

### 4.4 Pi tool use 借鉴与资源访问

`pico-main/docs/architecture/modernization-todolist.md` 的 Phase 3 给出了可借鉴的 Pi 风格工具界面：模型侧只看到稳定的 `read`、`write`、`edit`、`bash` 四类工具，其中 `read` 通过结构化 `operation=read/list/search` 聚合文件读取、目录列举和搜索；`delegate` 留在编排控制面。ThreadForge 需要吸收这一点，但不能直接丢弃当前已稳定的六工具兼容合同。

- 新增 provider-neutral 的四工具模型层；当前 `list_files/read_file/search` 先通过 `read` adapter 兼容，`write_file` 通过 `write` 兼容，`patch_file` 通过 `edit` 兼容，`run_shell` 通过 `bash` 兼容。
- 模型请求使用原生 JSON Schema 和 provider tool calling；不再新增依赖 `<tool>` XML 或自由格式 JSON 的协议。旧解析器仅作为迁移期 legacy adapter。
- 每个工具调用仍必须带 schema、版本、权限、审批、超时、取消、来源和审计字段；四工具只是模型界面收敛，不是安全边界放宽。
- `read` 的 `list`、`read`、`search` 操作只能访问已授权 workspace；不能因为“查看资源”而获得 `bash`、写入或删除权限。
- 需要分别提供本地资源和互联网资源能力：
  - `local_resource`：由 Worker 在已授权根目录内列目录、读取文本/图片/常见二进制元数据、搜索文件名或内容，并返回相对路径、大小、类型、修改时间和 freshness。大文件采用分页或预览，不把完整文件无条件送入模型。
  - `web_search`：通过受控搜索提供商检索公开网页，返回标题、URL、摘要、发布时间和来源标识；不得把搜索结果当作已验证事实。
  - `web_fetch`：按 URL 获取公开网页或文档正文，限制协议、重定向、响应大小、内容类型和超时；默认拒绝 loopback、RFC1918、云元数据地址、文件协议和非 HTTP(S) 协议。
- 互联网工具必须在 Tool Registry 中标记 `source=web`，独立配置网络 allowlist、速率限制、缓存和审计；不能让模型通过 `bash`、MCP 或任意 URL 绕过这些限制。
- 每个互联网结果必须保留 URL、抓取时间、内容摘要和脱敏后的响应元数据；网页中的指令视为不可信内容，不能自动改变工具权限或审批结果。
- 有登录、付费、写入、提交表单或其他外部副作用的网络操作不属于首期 `web_fetch`，需要后续独立的浏览器自动化和逐次审批设计。

### 4.5 本地资源浏览与拖拽引用

前端需要内置只读资源浏览器，使用户无需离开 ThreadForge 页面即可查看已授权本地资源。这里的“所有资源”只指用户明确授权的 Worker 根目录及其子目录，不指整个操作系统磁盘。

- 首次使用或新增根目录时，Worker 仍必须要求用户进行一次明确的目录授权；授权结果绑定 `device_id + workspace_id`，服务端不能接受客户端伪造的绝对路径。
- 授权完成后，前端通过 Worker 资源 API 按需加载目录树，支持目录展开、文件名搜索、文本/图片预览、文件类型过滤、大小和修改时间显示。目录树必须分页或虚拟化，不能一次性读取整个磁盘。
- 资源 API 只提供 `list/stat/read/search/preview` 等只读操作；不提供前端直接 `delete/write/rename/move` 接口。资源浏览器中的删除、重命名、覆盖、上传和拖拽移动均必须不可用。
- 拖拽只表示“引用资源”：用户可以把文件或目录拖到会话输入框、任务草稿、上下文面板或记忆候选区，生成受权限校验的相对路径/资源 ID；拖拽不得改变本地文件系统。
- 拖拽引用进入模型上下文前必须显示名称、相对路径、大小和将要读取的内容范围；大文件只附加摘要或用户选定的片段。提交任务后仍由 Worker 在执行时重新校验路径和 freshness。
- 前端刷新、断线或换设备后，资源树只恢复索引和授权状态；实际内容必须重新向拥有该 workspace 的 Worker 请求，不能把本地文件内容写入中央缓存。
- 资源树必须明确显示 Worker 在线状态、授权根目录、只读状态和权限不足错误；不能把离线、空目录或未授权误显示为“没有文件”。

建议接口：

```text
GET  /api/v1/devices/{device_id}/workspaces/{workspace_id}/resources?path=.
GET  /api/v1/devices/{device_id}/workspaces/{workspace_id}/resources/{resource_id}
POST /api/v1/devices/{device_id}/workspaces/{workspace_id}/resource-search
POST /api/v1/sessions/{session_id}/resource-attachments
```

这些接口只创建读取请求或会话引用，不执行文件系统修改。资源响应必须过滤绝对路径、符号链接越界、隐藏凭据和不允许的二进制内容，并带 `owner_id`、`device_id`、`workspace_id`、`trace_id` 和过期时间。

验收标准：用户授权一个 workspace 后能在前端逐层浏览和预览其目录；拖拽文件只能形成会话引用，不能删除、移动或改写文件；越界路径、离线 Worker、撤销设备和过期授权均被拒绝；互联网搜索/抓取与本地文件读取在工具列表、权限和审计中可区分。

## 5. MCP 系统需求

### 5.1 概念边界

MCP Server 是外部能力提供者，MCP tool 是 Server 暴露的单个能力。ThreadForge 不是把 Server 当作内置工具，而是把发现到的工具适配成 `ToolRegistry` 条目，并保留 Server 边界。

### 5.2 Server 配置和生命周期

每个 Server 独立保存：

- `server_id`、显示名称、协议版本和传输类型。
- 本地 Worker 上的启动配置、环境变量引用和凭据引用。
- 工具 allowlist、超时、并发数、速率限制和网络目标限制。
- `candidate/active/disabled/error` 状态、最近握手、schema 版本和健康信息。

凭据只存 Worker 本地安全存储或系统密钥环，不写入中央数据库、Git、SSE、任务结果或日志。中央服务只保存非敏感的 Server 元数据和授权关系。

### 5.3 调用策略

- 默认只读；写入、删除、执行命令、外部网络和有计费副作用的工具必须显式声明并逐次审批。
- MCP 工具必须经过参数 schema 校验、ThreadForge 权限映射和独立超时；不能继承宿主 workspace 的任意路径权限。
- 每次调用带 `server_id`、`tool_name`、工具版本、`trace_id`、`approval_id`（如有）和来源。
- Server 断线、握手失败、schema 变化、超时、速率限制和远端错误必须转为可观测的结构化事件。
- Server 返回的文件路径、凭据、原始网络响应和大块二进制必须经过脱敏、大小限制和路径策略后才能进入模型上下文。
- 首期只实现受控只读 MCP；写入 MCP 只有在安全评测和审批 UX 完成后才开放。

### 5.4 API 与验收

建议 API：

```text
GET    /api/v1/mcp/servers
POST   /api/v1/mcp/servers             # 创建本地配置，不上传凭据
POST   /api/v1/mcp/servers/{id}/test
PATCH  /api/v1/mcp/servers/{id}        # 启用/停用/更新 allowlist
DELETE /api/v1/mcp/servers/{id}
GET    /api/v1/mcp/servers/{id}/tools
```

验收要求：未授权 Server 的工具不会进入模型工具列表；撤销 Server 后已有连接被终止；凭据扫描不会出现在中央日志；同一个工具的 MCP 调用和 builtin 调用在审计中可区分。

## 6. Skills 系统需求

### 6.1 概念和 manifest

Skill 是可组合的 Agent 能力包，能够声明提示词、流程、工具和 MCP 依赖；它不是“自动获得全部工具权限”的插件。

最小 manifest：

```yaml
id: threadforge.example.skill
version: 0.1.0
description: "..."
entrypoint: package.module:run
required_tools: [read_file, search]
required_mcp: []
permissions: [workspace.read]
supported_platforms: [windows-x86_64, linux-x86_64]
dependencies: []
checksum: sha256:...
signature: ed25519:...
evaluation: pending
```

### 6.2 生命周期与权限

状态只允许按审核流程变化：

```text
candidate -> evaluated -> active -> deprecated
             |             |
             +-> rejected  +-> rolled_back
```

- 安装、启用或升级不得自动扩大权限；manifest 只能申请权限，最终由用户或管理员批准。
- Skill 的工具调用仍由 Tool Registry 执行，MCP 依赖仍由 MCP Adapter 执行；Skill 不能直接打开文件、Shell 或网络连接。
- Skill 升级必须校验签名、依赖、平台、版本兼容性和回滚点。
- 自进化只能生成隔离的 candidate、评测报告和差异；不得直接修改 active Skill、提高权限或发布到其他用户。
- 每个 Skill 运行带 skill id/version、依赖版本、权限快照和 trace id，支持禁用后立即阻断新调用。

### 6.3 Registry 与 API

Registry 需要提供版本解析、安装、启停、依赖检查、评测、发布、回滚和卸载；API、CLI、Coordinator 和前端必须共享同一个 application service，不能各自实现一套生命周期。

建议 API：

```text
GET    /api/v1/skills
POST   /api/v1/skills/install
POST   /api/v1/skills/{id}/evaluate
POST   /api/v1/skills/{id}/enable
POST   /api/v1/skills/{id}/disable
POST   /api/v1/skills/{id}/rollback
DELETE /api/v1/skills/{id}
```

前端页面必须显示真实的 `planned`、`candidate`、`active`、`error` 状态；接口为空或未接入时显示“未启用”，不能显示成已安装。

### 6.4 验收标准

- 非法 manifest、未签名制品、平台不匹配和缺失依赖在安装前拒绝。
- 没有权限的 Skill 无法调用对应工具或 MCP；权限变更会写入审计。
- 评测失败的 candidate 不会进入 active；active 升级失败可以原子回滚。
- 卸载 Skill 不删除会话历史和图记忆，只移除其自身制品和注册信息。

## 7. MRAgent 风格长期图记忆

### 7.1 设计目标

现有 `LayeredMemory` 继续承担单次运行的 bounded working memory、文件摘要、episodic notes 和 Markdown durable memory。图记忆是独立的 V1.1 服务，不直接替换旧接口，也不把所有聊天原文永久写入图。

参考 MRAgent 的“记忆被重构而不是简单检索”思想，首版采用 Cue–Tag–Content 关系，并扩展为以下节点类型：

| 节点 | 用途 |
| --- | --- |
| `Cue` | 当前问题、任务目标或用户显式提问中提取的检索线索。 |
| `Tag` | 项目、模块、技术、错误、决策等可复用的语义标签。 |
| `Episode` | 一次任务的请求、计划、工具调用、错误、审批、结果和状态摘要。 |
| `Semantic` | 经来源验证的项目约定、架构决策、依赖事实和用户偏好。 |
| `Topic` | durable Markdown 主题或图内聚类的稳定入口。 |
| `Content` | 受大小限制的摘要、引用片段或结构化事实，不保存不必要的原文。 |

所有节点和边至少携带：

```text
owner_id, workspace_id, session_id, run_id
source, provenance, created_at, updated_at
confidence, freshness, sensitivity, schema_version
```

文件相关事实还必须记录相对路径和内容 freshness（例如 SHA-256）；文件变化后只能标记 stale 或重新验证，不能继续当作最新事实注入上下文。

### 7.2 存储和兼容

推荐目录：

```text
THREADFORGE_DATA_DIR/
└── memory/
    └── {owner_id}/
        └── {workspace_id}/
            └── graph.db
```

首版使用 SQLite 邻接表 + FTS5，原因是单 Worker、本地历史和低运维成本与当前部署形态一致。引入 SQLite 是对 V1“Session/Run 使用 JSON、不引入数据库”约束的**明确、局部放宽**：不得借此把 V1 的所有状态迁移到数据库。

图数据库表至少包括 `nodes`、`edges`、`contents`、`sources`、`reconstruction_runs` 和 `schema_version`；写入使用事务，读写分离到 `MemoryService`，并保留从 Markdown durable memory 导入和导出的适配器。

### 7.3 主动记忆重构流程

新任务规划前执行有预算的重构：

1. 从用户请求和当前 workspace 上下文提取一个或多个 `Cue`。
2. 通过 FTS5 和精确标签匹配找到候选 `Tag`。
3. 使用 `Cue + Tag` 定位相关 `Episode`、`Semantic` 和 `Topic`。
4. 沿证据边扩展，依据 freshness、confidence、来源一致性和节点预算剪枝。
5. 达到证据阈值后生成有 provenance 的上下文片段；证据不足时明确返回未知，不用猜测补全。
6. 将本次检索的节点、边、过滤原因和预算写入 `reconstruction_runs`，便于审计和调参。

任务结束后异步执行蒸馏：

- 从用户任务、工具调用、错误、决策和最终状态生成候选 Episode。
- 将稳定、可验证、跨会话有价值的事实生成 Semantic candidate。
- 去重、合并同主题、过滤密钥/Token/个人隐私和短期状态。
- 低置信度、无来源或只在单次回答中出现的内容进入 candidate 区，不直接进入 durable fact。
- 只有通过规则或用户确认的内容才能提升为 `Semantic`；保留 supersedes/rejected 关系，不静默覆盖历史。

### 7.4 多 Worker 与本地所有权

- 图记忆默认按 `owner_id + workspace_id` 隔离；同一用户的多台 Worker 只在中央服务确认归属后共享索引。
- Worker 离线时可以读取本地已缓存图；中央服务不能凭空读取另一台 Worker 的会话正文。
- 多 Worker 并发写同一 workspace 时使用单写者队列或 SQLite 写锁，事件带 `device_id` 和幂等键。
- “关联工作区”作为后续能力：它只能生成显式的虚拟上下文视图，不能改变底层 workspace 所有权，也不能跨 owner 关联。
- 端到端加密不是本里程碑的前置条件；如后续开启，必须先定义密钥轮换、设备撤销、搜索能力和恢复流程，不能自行发明不可审计的密码协议。

### 7.5 记忆 API

建议通过 application service 暴露：

```text
GET    /api/v1/workspaces/{workspace_id}/memory/topics
POST   /api/v1/workspaces/{workspace_id}/memory/search
GET    /api/v1/workspaces/{workspace_id}/memory/reconstruction/{id}
POST   /api/v1/workspaces/{workspace_id}/memory/candidates/{id}/promote
POST   /api/v1/workspaces/{workspace_id}/memory/candidates/{id}/reject
GET    /api/v1/workspaces/{workspace_id}/memory/export
DELETE /api/v1/workspaces/{workspace_id}/memory/{node_id}
```

搜索接口必须限制最大节点数、边数、耗时和返回内容大小；删除和导出必须尊重 owner/workspace 权限并留下审计事件。API 返回节点摘要和 provenance，不默认返回完整会话正文。

### 7.6 图记忆验收标准

- 旧 `LayeredMemory` 和 Markdown durable memory 回归不变，历史数据可以导入、导出和回滚。
- 给定相同 Cue、图快照和预算，重构结果可重复；超预算时有明确的剪枝事件。
- 过期文件摘要不会被当作新事实；来源缺失、敏感信息和低置信度候选不会自动晋升。
- 任务结束后的异步蒸馏失败不阻断任务最终结果，失败可重试且不重复写入。
- 用户能按 workspace 查看、导出和删除记忆；删除后检索不会再次返回该节点。
- 多 Worker 并发写入不产生重复节点、跨 workspace 泄露或丢失已确认事实。

## 8. LangGraph 编排与平台基础需求

本节把 `modernization-todolist.md` 中不属于单一扩展主题、但决定整个 Agent 是否可靠的基础能力统一纳入需求。它们是工具、MCP、Skills 和图记忆的共同依赖。

### 8.1 当前编排状态

`agent-orchestrator/src/langgraph_pico/graph.py` 当前已经提供：

- `intent_router`：自动或显式识别 `conversation`、`read_only`、`code_change`，并对格式错误进行有限重试。
- `research_delegate`：只读 Research 子 Agent。
- `execute_change`：代码修改执行和有限的 Review 修复重试。
- `review_delegate`：只读 Review 子 Agent，并把结果转换为 `pass/needs_fix`。
- `finalize`：汇总状态、子任务和审计结果。

当前限制必须保留在产品状态中：子执行器使用内存 Session，并设置 `allow_checkpoint=False`、`allow_durable_memory_write=False`；图节点按串行边执行，不代表已经具备并发编排或 durable replay。

### 8.2 目标 LangGraph 流程

目标图以“安全的内部推理/计划提案 → 意图识别与策略校验 → 执行 → Review”为主线：

```text
START
  -> prepare_reasoning_plan
  -> intent_router
  -> plan_validate_and_research
  -> execute
  -> review
       |-- pass -> finalize
       |-- needs_fix -> execute (bounded loop)
       `-- blocked/failed -> finalize
```

具体要求：

- `prepare_reasoning_plan` 可以使用模型内部推理形成任务分解、假设、验收条件和风险提示，但不得把原始隐藏 CoT 返回前端、写入长期记忆或作为审计正文；对外只保存短的 `plan_summary`、依据和决策理由。
- Planning 输出是未授权提案，必须经过 `intent_router`、workspace/工具权限、预算和审批策略校验后才能成为可执行计划。用户显式指定的 `read_only`、`code_change` 或 `conversation` 可以覆盖自动路由，但不能绕过权限。
- `intent_router` 使用严格 JSON Schema，未知意图、额外字段、冲突的 `requires_research` 或多次格式错误都必须安全失败并给出可操作的错误，而不是猜测执行。
- `plan_validate_and_research` 根据意图决定是否读取本地资源、检索互联网或派生 Research 子任务；每个计划步骤必须有依赖、输入来源、所需工具、风险级别、预计预算和验收条件。
- `execute` 只执行已验证计划，工具调用统一通过 Tool Registry；写入步骤必须使用当前 Worker 的授权 workspace，不能直接写主工作区之外的路径。
- `review` 至少检查变更路径、需求验收、工具/测试结果、策略违规和新鲜度；Review 失败只能回到受预算限制的 `execute` 修复循环，不得无限自我修改。
- `finalize` 汇总最终回答、计划状态、变更摘要、Review 结果、拒绝/失败原因和可重试建议；基础任务失败时，记忆蒸馏异步失败不能覆盖原始失败原因。

### 8.2.1 Agent Turn 协议：`talk / tool / final / retry`

执行节点不能继续沿用“任何非空普通文本都视为 final”的隐式合同。每轮模型响应必须解析为一个明确的 Agent Turn；任务只有收到通过完成门禁的 `final` 才能结束。

四种结果分为两类：

```text
模型可主动选择：talk | tool | final
运行时控制结果：retry
```

- `talk`：向用户说明当前判断、正在进行的步骤或下一步动作。它产生可流式展示的 `assistant.commentary` 事件，但不写入 `final_answer`、不满足任何证据条件、不得把 Task 标记完成；事件正文随会话历史保存在 Worker 本地，中央服务只中转当前展示和必要控制元数据。
- `tool`：请求执行一个经过 schema、权限、审批和预算校验的工具。工具结果形成当前 Run 的结构化 evidence，再回到执行循环，让模型继续在 `talk/tool/final` 中选择。
- `final`：提交最终回答候选。候选必须先经过确定性完成门禁和对应意图的 Review；只有 `review=pass` 才能进入 `finalize`。
- `retry`：由运行时产生，不是模型可以主动选择的业务动作。格式错误、未知类型、非法工具参数、伪 final、完成条件不足或 Review 拒绝时，运行时返回带稳定错误码的修正提示并继续下一轮。

推荐的结构化合同：

```json
{
  "type": "talk | tool | final",
  "content": "talk/final 使用；tool 时可为空",
  "tool_call": {
    "name": "read_file",
    "arguments": {}
  }
}
```

模型层优先使用 provider 原生 tool calling 和严格 JSON Schema；旧 `<tool>` 协议只作为迁移适配器。迁移后的生产路径不得再把无类型的普通文本自动降级成 final：无法可靠分类时进入 `retry`，不得猜测任务已经完成。

运行循环：

```text
model
  |-- talk  -> emit commentary --------------------+
  |-- tool  -> validate/approve/execute/evidence --+--> model
  |-- final -> completion gate -> review
  |               |                  |-- pass -------> finalize
  |               |                  `-- needs_fix --+
  |               `-- reject -> retry --------------+
  `-- invalid -> retry ------------------------------+
```

为防止只说不做和成本失控，必须限制连续 `talk` 次数、总模型轮数、总 token、工具步数和总耗时。默认连续 `talk` 上限建议为 2；超过后下一轮必须产生 `tool` 或可通过门禁的 `final`，否则以明确的 `talk_limit_reached`/`step_limit_reached` 停止，不能伪造最终答案。

完成门禁至少包含：

- `conversation`：可以零工具直接 final，但若回答依赖 workspace、互联网或其他外部事实，必须存在对应的当前 Run evidence。
- `read_only`：用户要求读取、搜索、检查或重新确认资源时，当前 Run 必须存在成功的 `list/read/search` evidence；用户明确要求“重新读取”时，旧会话、旧摘要和长期记忆不能替代本轮读取，并校验文件 freshness。
- `code_change`：必须存在成功的写入/patch evidence、非空受影响路径，以及与验收条件对应的验证结果；没有变更、审批拒绝或验证缺失不能标记成功。
- `talk`、模型自述和阶段标签都不属于 evidence。确定性门禁必须先于模型 Review，不能只依赖另一个模型判断“看起来完成了”。
- `finish_success()` 只能在完成门禁和 Review 均通过后调用；前端 checklist 必须由真实节点、工具和 Review 事件推导，禁止在终止函数中无条件全部标绿。

SSE/前端必须区分 `assistant.commentary`、`tool.*`、`review.*` 和 `assistant.final`。`talk` 可以在任务运行区按时间显示或折叠，但不能混入用户气泡，也不能在刷新恢复后变成最终回答。浏览器断线不终止循环，重连后按事件序号恢复尚未结束的 commentary 和工具状态。

### 8.3 同阶段并发、汇聚和写入隔离

同一阶段的节点只有在依赖图证明互不依赖时才允许并发。LangGraph 实现可以使用 fan-out/fan-in（例如 `Send` 或等价调度器），但必须满足：

- 只读 Research、文件扫描、网页检索、静态分析和 Review 分面可以并行；它们读取同一个不可变 workspace snapshot，并为每个节点生成独立结果。
- 可能写入文件、生成 patch 或运行测试的节点不得共享可写目录。Git 使用独立 worktree，非 Git workspace 使用独立目录 snapshot；Coordinator 负责冲突检测和显式合并。
- fan-in 必须按稳定的 `node_id/sequence` 排序汇聚，不能依赖线程完成顺序；重复投递由 `idempotency_key` 去重。
- 全局和每个 root Task 都要限制最大 fan-out、并发数、总 token、工具调用数、CPU/内存、磁盘和网络预算。预算耗尽时停止派生新节点，并让已运行节点收敛。
- 一个节点失败时，计划必须根据策略选择“取消同阶段节点、保留可用结果后继续、或整体失败”；不能静默丢弃错误。
- 并发节点事件必须携带 `root_task_id`、`plan_id`、`stage_id`、`node_id`、`parent_node_id`、`worker_id`、`attempt` 和 `sequence`，前端可重放且不误把子节点显示成用户消息。

### 8.4 图状态、Checkpoint 和恢复

- `AgentState` 必须可序列化，至少包含计划版本、当前阶段、节点状态、依赖、预算、审批、工具调用和最终汇总引用；模型原始隐藏 CoT不进入持久状态。
- 每次阶段切换、fan-in、审批等待、工具调用终态和 Review 决策都产生 checkpoint。Checkpoint 不可变，恢复产生新的 `run_id`，并记录 `source_run_id/source_checkpoint_id`。
- 服务重启或 Worker 断线后，不能假装从任意 Python 栈继续；如果无法安全恢复，原 Run 标记 `interrupted`，原审批失效，由用户显式重试。
- 子 Agent 默认只能写自己的临时 Session、Run 和 artifact；父 Session、父 checkpoint 和 durable memory 只能由 Coordinator 在验证结果后写入。
- 图恢复必须验证 `owner_id`、`device_id`、`workspace_id`、runtime identity、schema version 和 Worker capability，拒绝跨 workspace 或旧版本状态注入。

### 8.5 Application Service、REST 和 SSE 合同

- FastAPI 路由只负责 HTTP 校验、认证、调用 application service 和响应转换；Web、Electron、CLI 和未来移动端复用同一 application service。
- 冻结 Workspace、Session、Task、Run、Approval、Artifact 和 Child Task 的 ID、归属及状态迁移；Session 在绑定 workspace 后不可隐式切换。
- 统一错误响应为 `code/message/details/request_id`，并为所有公开 schema 生成 OpenAPI；前端使用生成的 TypeScript client，不手工复制 DTO。
- 保留 `GET /health/live` 与 `GET /health/ready`；就绪检查要覆盖数据库、事件总线和 Worker 路由依赖。
- SSE 使用统一事件信封和单调序号，支持 heartbeat、`Last-Event-ID` 续传、事件重放、乱序保护、去重和慢消费者背压。SSE 断开不得自动取消 Agent，取消只能通过显式 REST。
- Web Approval 必须持久化 pending 状态，通过 `approval.required` 通知前端；批准/拒绝校验 task、run、tool_call、owner 和 approval 状态，重复决策幂等。
- 取消状态固定为 `cancel_requested -> cancelling -> cancelled`；必须传播到模型、图节点、工具、子 Agent、Worker 和 Sandbox，并清理进程树。

### 8.6 控制面持久化与迁移

- 新 application service 使用 SQLite 保存 Session/Task/Run/Approval、状态迁移、SSE sequence、Worker lease 和索引；大体积 trace/report/patch 继续落文件，数据库只保存路径、摘要、大小和校验和。
- 为现有 JSON SessionStore/RunStore/TaskState 提供幂等导入器，重复导入保持稳定 ID，保留原始文件并记录 migration version。
- 导入必须处理损坏 JSON、缺失 artifact、child_task_states、重复执行、迁移中断和回滚；迁移期只允许明确的唯一写入者，不允许无限期双写。
- SQLite 控制面与本节新增的图记忆 SQLite 分库或明确表边界；图记忆不能暗中接管 V1 Task/Run 状态。
- 进入真正多实例/多租户阶段前，完成 SQLite 到 PostgreSQL 的迁移演练、数据一致性校验、回滚和跨租户越权测试。

### 8.7 Docker Sandbox 与文件变更隔离

- 定义 `ContainerSandboxManager.create/exec/cancel/destroy/inspect`，每个任务或并行 Worker 使用独立临时容器和可写层。
- 容器使用非 root 用户、只读 rootfs、`no-new-privileges`，删除非必要 capabilities；禁止 privileged、host network、host PID/IPC 和 Docker Socket 挂载。
- 只挂载经过真实路径校验的任务 workspace 和受控临时目录；默认关闭网络，网络能力按目标域名、任务和有效期审批。
- 限制 CPU、内存、PIDs、临时磁盘、命令时间、输出大小和子进程数量；取消、超时和异常退出都必须销毁容器，不遗留 worker。
- 区分 `policy_violation` 与 `container_sandbox_violation`，并在 API、SSE、UI、metrics 和审计中使用不同标签。
- Sandbox 未完成前，无人值守模式不得开放模型任意 `bash`；有人值守宿主机模式必须明确显示风险警告。

### 8.8 Scheduler、多 Worker 和 Swarm

- 定义统一的 `Scheduler/Worker/Lease/Heartbeat/Retry/Result` contracts；Phase 1 进程内实现，后续 Redis/独立 Worker 不改变 REST/SSE 合同。
- 每个 root Task、plan、stage、node、model call、tool call、Worker 和 Sandbox 使用关联 ID；租约过期、重复投递、Worker 断线和重连必须可收敛。
- 一个 Worker 的能力包含平台、工具、Sandbox、MCP 和版本；调度只把任务发送到满足 capability、owner 和 workspace 归属的 Worker。
- Swarm 由 Coordinator 根据计划自动派生，不允许用户直接创建无预算的 Agent 树；每个子 Agent 使用最小 allowlist、独立上下文、预算和工作区隔离。
- 支持串行/并行消融评测，只有在质量、耗时或可靠性有证据收益时才扩大并发；合并冲突必须可解释、可拒绝和可回滚。

### 8.9 前端、CLI、打包与运维

- Web 前端使用 React + Vite + TypeScript，组件层沿用 Ant Design，TailwindCSS 只负责布局和少量原子样式；使用 pnpm workspace 锁定 Node/pnpm、依赖、lint、格式化、类型检查、单元测试和生产构建。
- 前端必须处理真实模型/工具/审批/计划/子 Agent 事件，支持刷新、断网、后端重启和任务终态恢复；Markdown/代码渲染必须严格 HTML 清理。
- 前端显示 `policy_boundary`、`container_sandbox`、Worker 在线状态和能力版本，不能用占位数据伪装能力；支持桌面宽度、移动宽度、键盘焦点和审批可访问性。
- 生成 OpenAPI TypeScript client，避免 REST/SSE DTO 在 Web、Electron 和 CLI 之间手工分叉；资源树、拖拽引用和任务页面必须使用同一权限/能力接口。
- CLI 作为 Web 的并列入口长期保留，逐步迁移到同一 application service；迁移不得改变旧参数、退出码和 benchmark 语义。
- LangGraph 作为正式可选后端打包，锁定依赖版本，统一开发、CI、Docker 和发布 constraints；缺依赖时提供可操作安装提示。
- 模型层定义 provider-neutral 的 `ModelRequest/ModelResponse/ToolCall/ToolResult/Usage`，Right Code/OpenAI-compatible adapter 必须保留 response/call id、usage、prompt cache、流式文本和流式 tool arguments 的合同；真实 provider 测试之外必须有 FakeModelClient/fake server 合同测试。
- 工作区后续可提供受控 Git URL 克隆，但必须校验协议、目标路径、凭据和 SSRF；不提供无边界 ZIP 上传。
- 模型配置遵循环境演进：开发模式可由服务端环境变量提供，生产多用户模式由 Worker 本地 `.env` 或管理员 allowlist 控制；API Key 不下发浏览器、不进入中央日志或图记忆。
- 生产基准为 Linux Docker，同时支持 Windows Docker Desktop 本地开发；Worker 安装器按平台单独签名、版本检查和 CD 发布，不能把二进制制品提交到公开 Git。
- 提供控制面、前端和不可信 Worker/Sandbox 的 Dockerfile 与 Compose；SSE 反向代理关闭缓冲，数据卷只保存必要状态和 artifact。
- 提供 `.env.example`、升级/备份/恢复/故障排查文档、结构化日志、metrics、trace correlation、基础告警和兼容版本检查。
- 公网模式要求 TLS、GitHub OAuth、安全 Cookie、CSRF/CORS、限流、安全响应头和跨租户访问测试；公网地址、API Key 和 Worker 制品不得进入公共仓库。

### 8.10 编排与平台验收

- FakeModelClient 能完整跑通 `prepare_reasoning_plan -> intent_router -> research/plan_validate -> execute -> review -> finalize`，并覆盖 conversation/read-only/code-change 分支。
- Agent Turn 合同覆盖 `talk -> tool -> talk -> final`、连续 talk 限额、非法输出 retry、final 门禁拒绝和 Review 修复；模型输出“我再看一遍……”但当前 Run 为零读取时不得结束任务。
- 同一阶段的两个只读节点确实并发执行，事件顺序在 fan-in 后稳定；写入节点使用独立 worktree/snapshot，冲突不会静默覆盖。
- Review `needs_fix` 最多循环配置次数，超过后以可解释终态结束；取消、审批拒绝、超时、Worker 断线和服务重启均不会继续执行后续节点。
- 从 checkpoint 恢复会生成新的 Run 并保留来源关系；跨 owner、device、workspace、runtime 或 schema 的状态恢复会被拒绝。
- CI 覆盖 API/SSE 合同、迁移回滚、Sandbox 生命周期、Scheduler lease、并发幂等、前端构建、CLI 兼容和安全边界。

### 8.11 最小可用 Agent 编排范围（Agent MVP）

Agent MVP 的目标不是一次完成 V1.1 全部平台能力，而是先让当前 Web/Electron + 本地 Worker 链路成为能够稳定阅读、修改和解释代码的单 Worker Coding Agent。MVP 复用现有六个 builtin 工具、审批、workspace 边界、本地会话和中央任务路由，不等待扩展平台完成。

#### MVP 必须完成

1. **生产 Worker 接入 LangGraph**：Web 下发的真实任务必须进入正式图入口，不能只有 CLI/benchmark 支持 LangGraph；Worker 安装包包含锁定版本的编排依赖，并上报 backend/capability/version。
2. **最小结构化 Planning 和意图**：实现 `prepare_reasoning_plan -> intent_router -> execute -> review -> finalize`，覆盖 `conversation/read_only/code_change`。计划只需保存任务摘要、步骤、所需 evidence、工具、预算和验收条件，不实现复杂 Swarm 分解。
3. **Agent Turn 循环**：完整实现 `talk/tool/final/retry`、连续 talk 限额、总预算、取消传播和结构化事件；普通文本不得自动成为 final。
4. **当前六工具可可靠使用**：继续使用 `list_files/read_file/search/run_shell/write_file/patch_file`，保留路径边界、参数校验、审批、超时、进程树清理和审计；Pi 四工具 Registry 与 MCP 工具不阻塞 MVP。
5. **Evidence Ledger 和确定性完成门禁**：每个工具结果记录 run、intent、plan step、workspace、freshness、状态和摘要；conversation/read-only/code-change 使用不同的必需证据规则，门禁失败进入 retry 或明确失败。
6. **真实 Review**：所有意图都经过完成条件检查；read-only Review 检查是否取得本轮新证据，code-change Review 检查受影响路径和验证结果。模型 Review 只能补充质量判断，不能覆盖确定性门禁。
7. **准确 TaskState 和前端状态**：阶段、checklist、工具数、读取数、talk、Review 和 final 均由真实事件驱动；零工具任务不得伪装成已完成全部执行步骤。刷新、SSE 重连和换前端后仍能恢复当前终态与事件顺序。
8. **现有安全与多用户边界不回退**：继续校验 `owner_id + device_id + workspace_id`，保留 GitHub OAuth、设备撤销、审批绑定、密钥脱敏和本地历史所有权；无人值守模式不得绕过现有 Shell/写入审批。
9. **失败可解释且可停止**：模型格式错误、talk 超限、step/token/time 预算耗尽、审批拒绝、Worker 断线和服务重启必须形成稳定终态，不得假成功、无限重试或遗留子进程。MVP 可以把无法恢复的运行标记为 `interrupted` 后让用户重试，不强制实现 durable graph replay。
10. **CI 和真实制品验收**：CI 必须覆盖 Turn 解析、三类意图、零证据 final 拒绝、工具与审批、Review 循环上限、取消/断线、事件恢复和前端显示；至少用一次实际打包 Worker 经 Web 创建 read-only 与 code-change 任务，证明生产入口不是 CLI 专用代码。

#### MVP 明确延期，不作为可用门槛

- MCP Client、MCP 工具适配和 MCP 前端管理。
- Skills Registry、安装/签名/升级、自进化和 Skill Marketplace。
- MRAgent 图记忆、SQLite + FTS5、主动重构和异步蒸馏；继续使用现有 LayeredMemory/Markdown memory。
- 同阶段 fan-out/fan-in、并行 Research、Swarm、跨 Worker 拆分与冲突合并；MVP 单任务串行执行。
- Docker Sandbox、独立 worktree/snapshot 自动合并；MVP 保留现有 workspace 边界、审批和进程清理，并禁止无人值守高风险执行。
- Pi 四工具统一 Registry、互联网搜索/抓取、完整前端资源树和拖拽引用；它们是后续体验与扩展能力，不阻塞基本 Coding Agent。
- durable LangGraph checkpoint replay、SQLite 控制面迁移、PostgreSQL、多实例调度和关联工作区。
- 移动端验收、应用层端到端加密和跨设备实时协作。

#### MVP 可用标准

用户能够在已授权 workspace 中创建会话；Agent 可以先用 `talk` 解释进度，再真实读取或修改文件，并在取得足够 evidence 和 Review 通过后返回 final。任何一句“我接下来会读取/检查/修改”的说明都不能单独结束任务；任务失败、取消或断线时，用户能看到真实原因并安全重试。

## 9. 统一安全、隐私与可观测性

- 所有请求使用服务端认证身份，不接受客户端自报 `X-User-Id` 作为授权依据。
- 设备令牌、MCP 凭据、模型 API Key 和 Worker `.env` 只在本地安全存储；日志统一做 secret-shaped 过滤。
- 审计事件至少记录 actor、owner、device、workspace、session、task、run、source、version、decision、timestamp 和结果摘要。
- 前端状态只能来自后端真实能力探测；`planned/available=false` 必须与未接入能力区分。
- 记忆、MCP、Skill 的错误不能泄露绝对路径、Token、完整网络响应或其他 workspace 内容。
- 每个扩展都要有耗时、失败率、拒绝数、超时数、重试数和资源预算指标；异常要关联 `trace_id`。

## 10. 数据迁移和版本策略

1. 先增加 schema 版本和只读探测，不改变 V1 JSON 文件。
2. 从现有 `MEMORY.md`、`topics/*.md` 和运行报告生成可回滚的图导入批次；导入记录来源为 `markdown_migration`。
3. 图记忆和扩展元数据按 workspace 独立迁移；任何一个 workspace 失败不阻断其他 workspace。
4. 每次 schema 升级保留备份、迁移日志和降级路径；不得删除原 Markdown durable memory。
5. Worker 版本不兼容时只关闭图记忆写入或降级为旧记忆读取，不影响基础任务执行。

## 11. 分阶段实施计划

### P0：合同冻结与回归基线

- 记录 `native`、`langgraph` 和 CLI 的公共入口、参数、返回行为、审计事件及 `task_state.json/trace.jsonl/report.json` 兼容字段。
- 为 prompt prefix、LayeredMemory、context reduction、Session resume、路径/符号链接边界、只读模式和审批拒绝建立快照或合同测试。
- 编写 Workspace/Session/Task/Run/Checkpoint、Scheduler/TaskRunner、Approval、SSE 流式策略、持久化和安全术语 ADR。
- 冻结旧 `<tool>...</tool>` 解析器的 legacy adapter 退役策略，避免新实现重新依赖自由格式协议。

### P1：Application Service 与可靠事件流

- 建立统一 application service，接入 FastAPI REST、SSE 和 CLI adapter；完成稳定错误合同、OpenAPI 和 health checks。
- 完成异步审批、取消状态机、超时、重启中断、事件序号、`Last-Event-ID`、heartbeat、重放和慢消费者处理。
- 将 JSON SessionStore/RunStore 幂等导入 SQLite 控制面，保留 artifacts、原始文件、迁移日志和回滚路径。

### P2：LangGraph 编排与结构化 Planning

- 实现 `prepare_reasoning_plan -> intent_router -> plan_validate/research -> execute -> review -> finalize` 图，并为 conversation/read-only/code-change 提供明确分支。
- 计划、意图、工具调用、审批、节点状态和 Review 结果全部结构化；隐藏 CoT 不出现在前端、长期记忆或审计正文。
- 实现 `talk/tool/final/retry` Agent Turn、按意图区分的 evidence ledger 和确定性完成门禁；先完成 8.11 的串行 Agent MVP，再扩展并发图。
- 实现同阶段只读节点的 fan-out/fan-in、稳定汇聚、幂等投递、取消传播、节点预算和 Review 修复上限。
- 增加 checkpoint 恢复、子 Task/Run 映射和跨 runtime/workspace 拒绝；确认子执行器不直接写父级 durable memory。

### P3：工具、Sandbox 与调度执行面

- 完成 Pi 风格四工具 Registry、MCP 只读适配、Worker 资源浏览、受控互联网工具和 Skills 基础生命周期。
- 完成 Docker Sandbox 生命周期、非 root/只读 rootfs/网络与资源限制、worktree/snapshot 隔离和异常清理。
- 完成 Scheduler/Worker/Lease/Heartbeat/Retry/Result 合同，并在单机多 Worker 上验证断线、租约过期、重复投递和冲突合并。

### P4：前端、交付与运维

- 前端完成生成式 API client、事件重连/去重、计划与子 Agent 展示、diff/review、资源树和拖拽引用、Markdown 清理和可访问性。
- 完成 LangGraph 依赖打包、CLI 迁移、Docker Compose、反向代理 SSE 配置、迁移/备份/恢复、结构化日志、metrics 和告警。
- 完成 TLS、OAuth、安全 Cookie、CSRF/CORS、限流、安全响应头和多用户跨租户越权验收。

### P5：多用户、多 Worker 和质量闭环

- 完成 PostgreSQL 迁移演练、配额、管理员审计、Worker 能力版本和多租户数据隔离。
- 完成 Swarm 并行消融评测、Skills candidate 评测/发布/回滚和图记忆 benchmark；所有阶段只通过 CI 发布。

### G0：合同和边界冻结

- 冻结 ToolRegistry、MCP、Skill manifest、MemoryService 的接口和错误码。
- 完成敏感信息、owner/workspace 隔离和审计字段清单。
- 为前端占位页面补齐真实 capability 状态。

### G1：工具 Registry 与只读 MCP

- 将六个 builtin 工具适配为统一 Registry 条目。
- 引入 Pi 风格的 `read/write/edit/bash` 模型界面和原生 provider tool calling；保留六工具兼容 adapter，完成 `read(list/read/search)` 的只读聚合。
- 实现 Worker 作用域内的只读资源树 API、文本/图片预览、搜索和会话拖拽引用；目录授权、撤销和离线状态必须可见。
- 实现受控的 `web_search`/`web_fetch` 只读能力，加入 SSRF、大小、内容类型、速率和来源审计限制。
- 实现单 Worker 本地 MCP Client、独立 allowlist/凭据/超时和只读调用。
- 完成断线、schema 变化、超时和审批回归。

### G2：Skills Registry 基础生命周期

- 实现 manifest 校验、签名、依赖检查、candidate/active/rollback。
- 接入工具和 MCP 权限快照；暂不开放自进化自动发布。

### G3：图记忆存储和迁移

- 新增 `threadforge_memory` 包、SQLite + FTS5 schema 和 Markdown 导入/导出。
- 实现节点/边 provenance、freshness、confidence、去重和删除。
- 仅在用户显式开启的 workspace 中写入图，失败自动降级到 LayeredMemory。

### G4：主动重构和异步蒸馏

- 实现 Cue → Tag → Episode/Semantic 的预算遍历、证据阈值和剪枝。
- 任务结束异步蒸馏，候选区审核后再晋升 durable fact。
- 增加重构审计、指标和可重复 benchmark。

### G5：多 Worker 一致性

- 单写者队列、幂等事件、离线缓存和冲突解决。
- 之后再评估关联工作区、端到端加密和跨 Worker 协同；这些不属于 G3/G4 的隐式范围。

## 12. CI 验收与发布门槛

本需求文档本身不要求本地测试。代码实现阶段统一通过 GitHub Actions 执行：

Agent MVP 使用独立的最小发布门槛，只要求与当前生产 Coding Agent 直接相关的检查通过：

- `unit`：Agent Turn schema/解析、talk 限额、evidence ledger、三类意图完成门禁和真实 TaskState。
- `regression`：现有 Pico 六工具、审批、路径边界、LayeredMemory、Session/Run 和 Worker 协议不回退。
- `orchestration`：串行 LangGraph 主流程、`talk -> tool -> final`、非法输出 retry、零证据 final 拒绝和 Review 修复上限。
- `integration`：FakeModelClient + 实际 Worker 生产入口，覆盖 Web 创建 read-only/code-change 任务、取消、断线和事件恢复。
- `frontend`：talk、tool、review、final 分层展示，checklist 不虚假完成，刷新/SSE 重连后状态一致。
- `security`：owner/device/workspace 隔离、审批重放、路径逃逸、凭据脱敏和高风险工具限制。

MCP、Skills、图记忆、并行 fan-out/fan-in、Sandbox、数据库迁移和多 Worker Swarm 对应的检查在功能进入实现阶段后再转为强制；它们未实现时必须报告 `planned/available=false`，但不阻塞 Agent MVP 发布。

完整 V1.1 继续执行以下全量门槛：

- `unit`：Registry、schema、memory graph、freshness、脱敏和权限边界。
- `regression`：现有 Pico 工具、LayeredMemory、Session/Run 和 Worker 协议。
- `contract`：REST、SSE、MCP、Skill、Memory application service 合同。
- `orchestration`：FakeModelClient + LangGraph 全流程、意图/计划分支、fan-out/fan-in、checkpoint 恢复和 Review 修复上限。
- `integration`：FakeModelClient + 本地 Worker，多用户、多 workspace、多 Worker 断线和审批闭环。
- `sandbox`：容器创建/取消/销毁、mount/network/resource 边界、worktree/snapshot 隔离和异常清理。
- `frontend`：类型检查、生产构建、SSE 恢复、资源树只读/拖拽引用和可访问性。
- `migration`：JSON 导入、SQLite schema、重复导入、损坏数据、备份恢复和 PostgreSQL 演练。
- `security`：路径逃逸、凭据泄露、跨 owner/workspace 访问、未审批副作用和过期记忆。
- 资源安全：本地资源越界、符号链接逃逸、拖拽修改/删除、互联网 SSRF、恶意网页指令和过大响应。

发布必须满足：CI 全部通过、数据库迁移可回滚、制品签名通过、前端能力状态与后端一致、没有把 Worker 二进制或公网凭据提交到 GitHub。文档-only PR 不应触发 Worker 制品发布，除非仓库工作流有明确强制规则。

## 13. 风险与明确不做事项

主要风险是记忆污染、代码事实过期、蒸馏成本、MCP 外部副作用、Skill 权限膨胀、图节点并发冲突、计划错误、fan-out 资源耗尽、Sandbox 配置错误和迁移不一致。每个风险必须有可观测指标、回滚路径和降级行为。

本里程碑明确不做：

- 用 Neo4j 或云端向量数据库替换本地 SQLite 首版。
- 把完整会话正文或所有工具输出永久写入图。
- 让模型自动批准工具、MCP、Skill 或记忆晋升。
- 通过“关联工作区”绕过 workspace 权限或复制其他用户数据。
- 把 MCP、Skills、图记忆占位页面伪装成已经可执行的生产能力。
- 在没有密钥管理、设备撤销和恢复设计前宣称已经完成端到端加密。
- 将原始隐藏 CoT 发送给前端、写入公共日志或当作长期记忆事实。
- 允许前端资源树通过拖拽直接删除、移动、重命名或修改本地文件。
- 允许 `web_search/web_fetch` 访问内网、云元数据、任意协议或执行外部副作用。

## 14. 完成定义

### 14.1 Agent MVP 完成定义

只有同时满足以下条件，最小可用 Agent 才可标记完成并进入当前生产链路：

1. Web 创建的真实任务由打包 Worker 进入 LangGraph 主流程，而不是只在 CLI 或评测中可选。
2. `talk/tool/final/retry`、三类意图、evidence ledger、确定性完成门禁和 Review 否决权均已实现。
3. 要求读取资源的任务在零本轮读取 evidence 时不能完成；代码修改在没有受影响路径和验证结果时不能完成。
4. 前端准确显示 commentary、工具、Review、失败和 final；刷新或重连不会把运行中说明变成最终回答。
5. 现有六工具、审批、取消、workspace 安全、多用户归属、本地历史和 Worker 自动更新不回退。
6. 8.11 和 12 节定义的 Agent MVP CI 与实际打包 Worker 验收全部通过。
7. 延期能力明确显示不可用，不用占位响应伪装成已实现。

### 14.2 完整 V1.1 完成定义

只有同时满足以下条件，完整 V1.1 平台与扩展里程碑才可标记完成：

1. Application Service、LangGraph、Scheduler、Sandbox、工具、MCP、Skills、Memory 均有独立边界和版本化合同。
2. `prepare_reasoning_plan -> intent_router -> execute -> review` 主流程、只读并发 fan-out/fan-in、checkpoint 恢复和 Review 修复上限均有 CI 证据。
3. 所有调用均经过 owner/device/workspace 权限、审批（如适用）、资源预算和审计。
4. 控制面状态可从 V1 JSON 迁移到 SQLite 并回滚；图记忆能够按 Cue 主动重构、异步蒸馏并保留 provenance/freshness/confidence。
5. Docker Sandbox、worktree/snapshot、Worker lease 和断线恢复不会留下越权进程、容器、临时目录或重复任务。
6. 用户可以查看、导出、删除和回滚扩展及记忆；Worker 离线、版本不兼容或图写入失败时基础任务仍可运行。
7. Web、Electron 和 CLI 的 API/SSE/审批/资源浏览行为一致，前端不把占位能力显示为已启用。
8. 公开仓库只包含源码和文档；Worker 安装包、API Key、公网入口凭据和个人问题日志不进入公共 Git 历史。
9. CI 的 orchestration、sandbox、migration、frontend、unit、regression、contract、integration 和 security 门槛全部通过，并完成一次真实多用户、多 Worker 验收。

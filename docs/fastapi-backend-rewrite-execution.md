# ThreadForge V1 FastAPI 后端重写执行文档

> 状态：已复核的可执行草案
> 适用范围：ThreadForge V1 后端
> 基线：`main@9b560b14ed827f911e05316128fbd471cb6c92e3`
> 最后复核：2026-08-02
> 权威需求：[V1 需求基线](./requirements-v1.md)
> 相关边界：[API Server README](../api-server/README.md) · [Legacy Runtime 架构](../pico-legacy-runtime/docs/architecture/agent-harness-v1-overview.md)
> 仓库：[sumiim/ThreadForge](https://github.com/sumiim/ThreadForge/tree/9b560b14ed827f911e05316128fbd471cb6c92e3)

## 1. 结论与执行原则

当前 `api-server` 只有边界说明，没有可重写的既有 FastAPI 实现。因此本次工作的准确含义是：**从空工程实现独立 FastAPI 服务，同时对 Pico Legacy Runtime 做最小、向后兼容的可插拔改造**。

V1 不重写 AgentLoop、Prompt、Memory、Checkpoint、工具协议或模型客户端。FastAPI 只负责 HTTP/SSE 适配；任务编排、审批等待、取消协调和状态查询放在 application service 与基础设施层；Agent 决策继续由 `pico-legacy-runtime` 负责。

以下决策在实施开始前冻结：

| 主题 | V1 决策 | 说明 |
| --- | --- | --- |
| Web 默认后端 | `native` AgentLoop | 服从 `requirements-v1.md`；LangGraph 仅保留未来适配位 |
| Web 工具集合 | `list_files/read_file/search/run_shell/write_file/patch_file` | 显式排除 `delegate`，保持 V1 Task 1:1 Run 和无 child Agent 范围 |
| 持久化 | JSON `SessionStore`、`RunStore` 和新增 JSON 控制状态 | 不引入 SQLite、PostgreSQL 或 Redis |
| 并发 | 全局最多一个活动 root Task | 单进程、单 TaskRunner worker；查询和 SSE 可并发 |
| 模型输出 | 非流式 `/responses` | 只发真实 `message.completed`，不得人工切片伪造 token 流 |
| 审批 | `per_call_only` | `write_file`、`patch_file`、`run_shell` 每次单独审批；默认 fail closed |
| 取消 | 协作式取消 + 受管 Shell process group/Job 终止 | 已发出的同步模型请求使用一次有限尝试并丢弃迟到结果 |
| 执行隔离 | 后端运行环境执行 + `policy_boundary` | UI 和 API 元数据必须明确“尚无独立容器沙盒” |
| 访问范围 | 单用户、loopback | 默认只监听 `127.0.0.1`，不实现登录和公网部署 |
| SSE 恢复 | 运行期内存事件 + 连接时快照 | V1 不承诺跨进程可靠重放；可靠事件重放留到 V1.2 |

`pico-main/docs/architecture/modernization-todolist.md` 中的分层、TaskRunner、审批和取消设计可复用，但其中 SQLite、LangGraph Web 默认、可靠事件重放、多 worker 和容器沙盒不得反向扩大本 V1 范围。注意：pico-main 的 M1 路线采用 SQLite 与 LangGraph Web 默认，本 V1 依据 `requirements-v1.md` 有意收窄为 JSON 与 `native`，这是两套文档各自 M1 的差异，不是冲突。

### 1.1 可行性结论

方案在“单进程、单活动 Task、native Runtime、JSON 状态、有人值守逐次审批”的 V1 约束内可实施，没有要求数据库、分布式队列或重写 AgentLoop。正式开工前只有两个强制 go/no-go 门槛：Phase 0 全矩阵回归基线必须通过；Windows Job Object spike 必须证明受管子进程/孙进程可清理。后者失败时不得用 `taskkill` 弱化承诺，必须先修正 containment 实现或暂停 Windows `run_shell` 交付。

仍然存在但已明确接受的产品限制是：同步 provider 请求不能立即中断、SSE 不能跨进程重放、已批准 Shell 不具备文件系统隔离、Linux 主动 daemonize 可逃离普通 process group。这些限制均与 [V1 需求基线](./requirements-v1.md) 的本机有人值守定位一致，并已在状态、UI、安全和测试合同中显式呈现；它们不是隐藏的实现假设。

## 2. 当前基线盘点

### 2.1 可直接复用的能力

| 现有模块 | 可复用能力 | V1 用法 |
| --- | --- | --- |
| `pico.runtime.Pico` | Session、Prompt、Memory、工具注册、审计和报告 | 每个 Run 创建独立实例 |
| `pico.agent_loop.AgentLoop` | native 控制循环 | Web 默认执行入口 |
| `pico.session_store.SessionStore` | JSON Session 保存与恢复 | 扩展原子写、列表和安全读取后复用 |
| `pico.run_store.RunStore` | `task_state.json`、`trace.jsonl`、`report.json` | 作为 Run artifact 唯一写入者 |
| `pico.event_sink.EventSink` | Runtime 审计事件扩展点 | 组合 JSONL sink 与 Web event sink |
| `pico.tool_executor.ToolExecutor` | 参数校验、风险判断、审批闸门、结果元数据 | 保留唯一工具执行入口 |
| `pico.security` | 环境变量过滤、secret redaction | HTTP 日志、SSE 和 artifacts 继续复用 |
| `pico.workspace.WorkspaceContext` | 仓库上下文构建 | 只对已允许 Workspace 构建 |
| `OpenAICompatibleModelClient` | Right Code `/responses` 调用 | 使用服务端 `PICO_OPENAI_*` 配置 |

### 2.2 必须补齐的缺口

1. `api-server` 尚无包结构、依赖、FastAPI app、路由、测试或启动入口。
2. 当前 `SessionStore.save()` 非原子写，且没有列表接口和并发读保护。
3. 当前 Run artifact 能按 `run_id` 读取，但没有 Task、Session、Run 的 Web 查询索引。
4. `Pico.approve()` 直接调用终端 `input()`，无法支持 Web 异步审批。
5. AgentLoop 没有 cancellation token，也没有模型/工具步骤边界取消检查。
6. `run_shell` 使用阻塞 `subprocess.run()`，无法在取消时终止活动进程树。
7. Runtime 只生成内部 `task_id/run_id`；Web 必须在返回 `202` 前先生成并持久化 ID。
8. EventSink 是审计事件接口，不是稳定的公开 SSE 合同；需要翻译层和线程安全 broker。
9. 当前 `sandbox_violation` 实际表示策略边界违规，V1 API/UI 必须映射为 `policy.violation`，不能声称已有 Docker sandbox。
10. API 进程重启后无法恢复 Python 调用栈；必须定义中断 Run 和失效审批的恢复语义。

### 2.3 基线验证记录

- 远端 `main` 和本地 `HEAD` 均为 `9b560b14ed827f911e05316128fbd471cb6c92e3`。
- 评审指出的 6 文件集合（`session_store/run_store/event_sink/tool_executor/safety_invariants/public_api_contract`）已复现为 `30 passed, 1 skipped in 4.58s`，确认原文未列文件时的数字口径有歧义。
- 后端改造直接覆盖的 7 个测试文件使用下列明确命令复测，结果为 `37 passed, 1 skipped in 4.54s`；跳过项是当前 Windows 账号不能创建符号链接：

```powershell
python -m pytest -q -rs `
  tests/test_session_store.py `
  tests/test_run_store.py `
  tests/test_event_sink.py `
  tests/test_task_state.py `
  tests/test_tool_executor.py `
  tests/test_safety_invariants.py `
  tests/test_public_api_contract.py
```

- 全量 `python -m pytest -q` 在 Windows NT 10.0.19045、Python 3.12.7 上实测 `250 passed, 1 skipped in 261.42s`。这只是本地参考；Phase 0 仍必须在 CI 的 Linux/Windows、Python 3.10/3.12 矩阵中记录正式基线。

## 3. V1 目标与非目标

### 3.1 必须交付

- Workspace allowlist 查询和真实路径边界校验。
- Session 创建、列表、详情、历史消息和页面刷新恢复。
- Task 创建、状态查询、最终回答和单活动 root Task 约束。
- Task 生命周期、模型完成、工具、审批、取消和错误 SSE 事件。
- 风险工具的持久化单次审批、批准、拒绝、超时和 stale 决策处理。
- 运行中取消；取消后不再开始新的模型或工具步骤，并终止活动 Shell 的受管 process group/Windows Job。
- `task_state.json`、`trace.jsonl`、`report.json` 安全查询。
- Right Code GPT 服务端配置、live/ready 健康检查、结构化日志和 request ID。
- Windows 本地开发方式、Linux Docker Compose 和 CI 测试。

### 3.2 明确不做

- 登录、Cookie Session、租户、远程访问或公网安全承诺。
- 数据库、可靠队列、跨进程事件重放、Task 重试或 Run 恢复。
- LangGraph Web 默认、Planning、child Task 展示、多 worker 或 swarm。
- provider token streaming、WebSocket 或运行中追加消息。
- 四工具标准化、原生 tool calling、MCP 或 Skills API。
- Docker 工具沙盒、网络白名单、Git URL 克隆或 ZIP 上传。

## 4. 目标架构

```text
React Web Console
  |-- REST: workspace / session / task / approval / cancel / artifact
  `-- SSE : task snapshot + lifecycle / model / tool / approval / error
                         |
                      FastAPI
                         |
                Application Services
        SessionService / TaskService / ArtifactService
              |            |             |
       JSON repositories  EventBroker  TaskRunner(1)
                                      |          |
                                ApprovalGate  CancellationToken
                                      \          /
                                     NativeRuntimeAdapter
                                             |
                                    Pico Legacy Runtime
                                      |           |
                                SessionStore   RunStore
```

### 4.1 层职责

| 层 | 负责 | 禁止 |
| --- | --- | --- |
| API | Pydantic 校验、状态码、request ID、调用 service、响应转换 | 创建 Runtime、读写裸 JSON、阻塞等待审批或 Agent |
| Application | 用例编排、状态迁移、幂等、业务错误、全局活动 Task 规则 | 依赖 FastAPI Request/Response |
| Domain | 状态枚举、实体、转换规则、错误类型 | 文件系统、线程、HTTP |
| Infrastructure | JSON repository、TaskRunner、EventBroker、ApprovalGate、Runtime adapter | 定义公开 HTTP 合同 |
| Legacy Runtime | Agent 决策、Prompt/Memory、工具校验执行、审计 artifacts | 知道 HTTP、SSE、FastAPI 或前端状态 |

### 4.2 关键边界

- FastAPI route 必须是薄适配器；route 中不得直接调用 `Pico.ask()`。
- 同步 AgentLoop 必须运行在 API event loop 之外。
- Runtime 每个 Run 新建，禁止跨并发请求共享 `Pico` 实例。
- NativeRuntimeAdapter 必须显式传入 V1 Web tool allowlist，禁止 `delegate`；CLI 的默认工具集合不变。
- `TaskRepository` 是 Web 控制状态的唯一写入者；`RunStore` 是 Run artifacts 的唯一写入者。
- Web adapter 必须为每个 Run 注入 rooted 于 `THREADFORGE_DATA_DIR` 的 `SessionStore`/`RunStore`；`Pico.__init__` 的默认 `run_store` 指向工作区内 `.pico/runs`，且构造时会立即 `session_store.save(session)`。Web 路径不得复用工作区内默认落点，否则会污染用户仓库并可能出现在 Git status；虽然现有 workspace 文件快照会忽略 `.pico`，这不构成允许写入的理由。
- Web adapter 不得调用 CLI `build_agent()` 或 `load_project_env(workspace.repo_root)`；现有 CLI 会加载工作区 `.env` 并覆盖进程环境，而 Web 的 provider base URL、model 和 API Key 只能来自服务启动时冻结的 Settings。NativeRuntimeAdapter 必须直接构造 WorkspaceContext、model client 和 Pico。
- EventSink 先生成内部事件，`PublicEventSink` 再翻译为稳定 SSE 事件；前端不得消费 legacy trace 名称。
- 所有 Runtime 增强保持向后兼容，原 CLI 不改变默认行为。

## 5. 目标目录结构

```text
api-server/
├── pyproject.toml
├── README.md
├── src/threadforge_api/
│   ├── __init__.py
│   ├── __main__.py
│   ├── main.py
│   ├── config.py
│   ├── lifespan.py
│   ├── logging.py
│   ├── api/
│   │   ├── dependencies.py
│   │   ├── errors.py
│   │   ├── models.py
│   │   └── routers/
│   │       ├── health.py
│   │       ├── workspaces.py
│   │       ├── sessions.py
│   │       ├── tasks.py
│   │       └── runs.py
│   ├── application/
│   │   ├── artifact_service.py
│   │   ├── session_service.py
│   │   └── task_service.py
│   ├── domain/
│   │   ├── entities.py
│   │   ├── enums.py
│   │   ├── errors.py
│   │   └── events.py
│   └── infrastructure/
│       ├── approval_gate.py
│       ├── execution_boundary.py
│       ├── event_broker.py
│       ├── fenced_run_store.py
│       ├── json_repositories.py
│       ├── native_runtime.py
│       ├── public_event_sink.py
│       ├── task_runner.py
│       └── workspace_catalog.py
├── tests/
│   ├── contract/
│   ├── integration/
│   └── unit/
├── Dockerfile
└── .env.example
```

Legacy Runtime 只改以下明确位置：

```text
pico-legacy-runtime/pico/
├── agent_loop.py      # 接受预分配 ID；增加取消检查和取消终态
├── approval.py        # ApprovalRequest/Outcome/Strategy 公共合同
├── execution_hooks.py # 模型/工具步骤的可插拔线性化 hook
├── runtime.py         # 注入 approval strategy 与 cancellation token
├── session_store.py   # 原子写、list、损坏文件错误
├── shell_process.py   # 跨平台可追踪、可终止的 Shell 进程树
├── task_state.py      # user_cancelled/service_restarted/process_cleanup_failed
├── tool_context.py    # 向 shell runner 传取消能力
├── tool_executor.py   # 类型化审批结果、取消复检和工具错误映射
├── tools.py           # 可追踪、可终止的 shell 进程
└── providers/
    └── clients.py     # Web 单次模型尝试；CLI 默认重试保持兼容
```

不得为 Web 复制一份 AgentLoop、ToolExecutor 或 provider client。

### 5.1 包依赖

`api-server` 生产依赖保持精简：

- `fastapi`：HTTP、OpenAPI 和依赖注入。
- `uvicorn[standard]`：单进程 ASGI 服务。
- `pydantic-settings`：集中配置加载与校验。
- `anyio`：event loop 与 Runner 线程间的受控协作。
- `filelock`：对数据目录持有跨平台进程级独占锁，拒绝第二个 API 进程。
- 本仓库构建出的 `pico` 包：Runtime、Store、Provider 与安全策略。

Legacy Runtime 增加带 `sys_platform == "win32"` marker 的 `pywin32` 依赖，用 Windows Job Object 管理 Shell 进程树；Linux 不安装该依赖。测试/开发依赖至少包含 `pytest`、`pytest-asyncio`、`httpx`、`ruff` 和 `build`。SSE 使用 Starlette 的 `StreamingResponse`，V1 不为简单事件流额外引入 broker/数据库客户端。实施时在约束文件中锁定经 Python 3.10/3.12 和 Windows/Linux CI 验证的版本；Docker、CI 和本地开发必须使用同一依赖来源。

## 6. 领域合同与状态机

### 6.1 标识关系

V1 使用以下关系：

```text
Workspace 1 --- N Session 1 --- N Task 1 --- 1 Run
                                      |
                                      N Approval
```

- `workspace_id`：配置文件中的稳定 ID，不暴露任意后端文件系统路径选择。
- `session_id`：`ses_` + `uuid4().hex`（32 位小写十六进制）。
- `task_id`：`task_` + `uuid4().hex`，由 API 在返回 `202` 前生成。
- `run_id`：`run_` + `uuid4().hex`，由 API 与 Task 同时生成。
- `approval_id`：`apr_` + `uuid4().hex`，每次风险工具调用唯一。
- V1 一个 Task 只有一个 Run；模型中仍保留 `run_id`，为 V1.2 重试预留，不在 V1 实现一对多行为。
- `task_id/run_id/approval_id` 统一由 API 侧按上文格式生成；legacy runtime 仅把 ID 当作不透明字符串存储，API 格式不必与 legacy 内部 `task_...` 短随机后缀一致，但 V1 内不得混用两种格式。

### 6.2 Task 状态

公开状态固定为：

```text
queued
  -> running
  -> waiting_for_approval -> running
  -> cancel_requested -> cancelled
  -> completed
  -> failed
```

允许的状态迁移：

| 当前状态 | 操作/事件 | 下一状态 |
| --- | --- | --- |
| `queued` | TaskRunner 获得执行权 | `running` |
| `queued` | 用户取消 | `cancelled` |
| `running` | 风险工具请求审批 | `waiting_for_approval` |
| `waiting_for_approval` | 批准 | `running`，随后只执行对应 tool call |
| `waiting_for_approval` | 拒绝 | `running`，工具返回 approval denied，由 Agent 决定后续 |
| `waiting_for_approval` | 审批超时/过期 | `running`，工具返回 approval expired，由 Agent 决定后续 |
| `running`/`waiting_for_approval` | 用户取消 | `cancel_requested` |
| `cancel_requested` | Runner 确认停止并完成清理 | `cancelled` |
| `cancel_requested` | 受管进程清理无法确认 | `failed`，`stop_reason=process_cleanup_failed` |
| 非终态 | 服务关闭超时并提升 fencing generation | `failed`，`stop_reason=service_shutdown_timeout` |
| `running` | 正常最终回答 | `completed` |
| 非终态 | 未处理异常/模型错误/持久化错误 | `failed` |

终态 `completed/cancelled/failed` 不得再次迁移。重复取消返回当前快照，不重复发取消事件。

Legacy `TaskState.status` 继续使用 `running/completed/stopped/failed`；API 映射规则为：

| Legacy | API |
| --- | --- |
| `completed` + `final_answer_returned` | `completed` |
| `stopped` + `user_cancelled` | `cancelled` |
| `stopped` + 其他 stop reason | `failed`（`stop_reason` 透传；系统停止提示不是真实 final answer，不得标为 `completed`） |
| `failed` | `failed` |
| 其他运行中状态 | 以 `TaskRepository` 控制状态为准 |

### 6.3 Approval 状态

```text
pending -> approved
        -> rejected
        -> expired
        -> cancelled
```

- Approval 必须绑定 `task_id/run_id/tool_call_id/tool_name/args_digest`，批准范围不能扩大到其他调用。
- `tool_call_id` 是 Runtime 在模型输出被解析为 tool call 后、参数校验前生成的内部标识；V1 未启用 provider 原生 tool calling，因此不使用 provider 返回的 call id。校验通过后才发布 `tool.requested`，approval 由该标识 + `args_digest` 精确限定；校验失败的 `tool.failed` 也沿用同一标识并标记 `phase=validation`。
- `args_digest` 必须在 redaction 前对原始 args 的 canonical JSON（`sort_keys=True, separators=(",", ":"), ensure_ascii=False` 后 UTF-8 编码）计算 HMAC-SHA256；HMAC key 由 ApprovalGate 启动时随机生成且不落盘，重启后旧 approval 本来就会失效。持久化只保存 digest。`args_preview` 先生成结构化 redacted object；若其序列化长度超出 `THREADFORGE_APPROVAL_PREVIEW_MAX_CHARS`，改存 `{"_truncated": true, "text": "..."}` 形式的已脱敏文本预览，完整 secret 不得进入 approval JSON。
- 相同 decision 的重复请求返回 `200`；冲突 decision 返回 `409 approval_already_resolved`。
- Task 取消会唤醒等待线程并把 pending approval 标记为 `cancelled`。
- 批准与取消竞态必须在同一个 ApprovalGate 临界区串行化：一旦 Task 已持久化 `cancel_requested`，后到的批准返回 `409 approval_stale`；即使批准先到，工具实际执行前仍必须再次检查 cancellation token。
- 进程重启后 pending approval 标记为 `expired`，原 Run 标记 `failed`，`stop_reason=service_restarted`。
- 审批超时后 approval 标记为 `expired`，Gate 向工具返回可区分的 "approval expired" 结果（`tool.failed`、`tool_error_code=approval_expired`），Task 回到 `running` 由 Agent 决定后续；不得与普通拒绝共用同一错误码，否则审计与 `approval.resolved` 不一致。

### 6.4 全局活动 Task

- `TaskService.create_task()` 必须在同一把进程锁内完成“检查 active task -> 写入 queued Task -> 注册 Runner”。
- 已有 `queued/running/waiting_for_approval/cancel_requested` root Task 时返回 `409 active_task_exists`，响应 details 带活动 `task_id`。
- 终态持久化必须发生在释放全局活动位之前。
- 正式 launcher 必须在启动 Uvicorn 前对数据目录持有跨平台进程级独占锁，并将锁句柄保留到 OS 进程 teardown（用户代码不显式提前释放）；第二个 API 进程启动失败，防止两个进程分别认为自己拥有活动位。
- V1 不支持多 API 进程；Uvicorn workers 必须固定为 `1`，部署配置和进程锁共同强制该约束。

## 7. 配置与 Workspace 安全

### 7.1 服务端配置

V1 配置项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `THREADFORGE_HOST` | `127.0.0.1` | V1 只允许 IPv4/IPv6 loopback；其他值启动失败 |
| `THREADFORGE_PORT` | `8000` | API 端口；校验范围 `1-65535` |
| `THREADFORGE_DATA_DIR` | 必填 | Session、Task、Approval、Run artifacts；必须位于所有 Workspace 外 |
| `THREADFORGE_WORKSPACES_FILE` | 必填 | Workspace allowlist JSON 文件 |
| `THREADFORGE_WEB_ORIGIN` | `http://127.0.0.1:5173` | 精确 CORS origin，不允许 `*` |
| `THREADFORGE_APPROVAL_TIMEOUT_SECONDS` | `1800` | 单次审批超时；校验范围 `30-86400` 秒 |
| `THREADFORGE_APPROVAL_PREVIEW_MAX_CHARS` | `4000` | redacted approval preview 总字符上限；校验范围 `256-10000` |
| `THREADFORGE_MODEL_TIMEOUT_SECONDS` | `120` | Web OpenAI-compatible 客户端的单次 socket timeout；校验范围 `5-300` 秒 |
| `THREADFORGE_SHELL_CLEANUP_GRACE_SECONDS` | `5` | Shell 先终止再强杀的宽限期；校验范围 `1-30` 秒 |
| `THREADFORGE_SHELL_OUTPUT_MAX_BYTES` | `1048576` | stdout+stderr 保留上限；校验范围 `64 KiB-16 MiB` |
| `THREADFORGE_SSE_HEARTBEAT_SECONDS` | `15` | SSE heartbeat；校验范围 `5-60` 秒 |
| `THREADFORGE_SSE_QUEUE_SIZE` | `256` | 每个 subscriber 的事件队列容量；校验范围 `16-4096` |
| `THREADFORGE_MAX_STEPS` | `6` | Task 步数上限；API 校验范围 `1-25`，与 legacy `max_steps` 对齐 |
| `THREADFORGE_MAX_NEW_TOKENS` | `512` | 每个模型步骤的输出上限；校验范围 `64-8192` |
| `THREADFORGE_MODEL_TEMPERATURE` | `0.2` | Right Code sampling temperature；校验范围 `0-2` |
| `THREADFORGE_TASK_INPUT_MAX_CHARS` | `20000` | Task input 字符上限；配置校验范围 `1000-100000` |
| `THREADFORGE_ARTIFACT_MAX_BYTES` | `10485760` | 单次 artifact HTTP 读取上限；校验范围 `1-100 MiB` |
| `THREADFORGE_OPENAPI_ENABLED` | `true` | loopback 开发环境的 `/docs/openapi.json` 开关；生产 Compose 设为 `false` |
| `THREADFORGE_LOG_LEVEL` | `INFO` | `DEBUG/INFO/WARNING/ERROR` |

Right Code 继续使用服务启动环境中的 `PICO_OPENAI_API_BASE`、`PICO_OPENAI_API_KEY`、`PICO_OPENAI_MODEL`。lifespan 加载后将其冻结到 Settings；后续 Workspace `.env` 不参与配置。API Key 不返回响应、不记录请求头，也不包含在 Shell 子进程的显式环境 allowlist 中。但 V1 没有独立执行沙盒：用户批准的恶意 Shell 仍可能尝试读取后端进程有权访问的宿主/容器资源，因此逐次审批和 UI 警告是必要限制，不是凭据隔离的替代品。

服务允许在未配置 API Key 时启动并提供 Workspace/Session/历史查询；创建 Task 时若模型配置不完整，返回 `503 model_not_configured`，不得先创建永久 queued Task。base URL/model 采用现有 `PICO_OPENAI_*` 默认/环境规则，但只能在启动阶段解析一次。

所有配置通过一个 Pydantic Settings 对象在 lifespan 启动时一次性校验，业务代码不得散落读取 `os.environ`。

正式启动入口固定为 `python -m threadforge_api` 或项目声明的 `threadforge-api` console script，两者都调用 `threadforge_api.__main__.main`。该入口读取同一 Settings 并以 `workers=1` 调用 Uvicorn；生产和普通本地运行不得绕过该入口直接覆盖 host/workers。`uvicorn --reload` 只作为明确记录中断语义的开发命令。

### 7.2 Workspace allowlist

`workspaces.json` 示例：

```json
{
  "workspaces": [
    {
      "id": "threadforge",
      "name": "ThreadForge",
      "path": "D:/study place and codes/python codes/ThreadForge"
    }
  ]
}
```

启动校验必须：

1. 将配置路径解析为绝对真实路径。
2. 要求路径存在且为目录。
3. 拒绝重复 `id`、重复真实路径，以及数据目录与任一 Workspace 互为包含关系的危险配置。
4. 拒绝 `workspaces.json` 位于任一允许 Workspace 内，避免 Agent 修改自己的授权配置。
5. 保存启动时解析出的 canonical path；API 请求只接收 `workspace_id`，不接收绝对路径。
6. 每次创建 Session、创建 Task 和 Runner 构造 Runtime 前都重新解析路径并确认仍等于启动时 canonical path，防止 Session 创建后发生符号链接/联接点替换。
7. Artifact 和工具路径必须通过 `Path.resolve()` 后验证仍在对应 Workspace 或 Run 目录内。

`GET /api/v1/workspaces` 只返回 `workspace_id/name/display_path/available/is_git/execution_environment/container_sandbox_enabled`；`display_path` 可供本机 UI 展示，但不得被后续请求当作授权凭证。

## 8. JSON 持久化设计

### 8.1 数据布局

```text
THREADFORGE_DATA_DIR/
├── sessions/
│   └── {session_id}.json
├── tasks/
│   └── {task_id}.json
├── approvals/
│   └── {approval_id}.json
├── recovery.jsonl
└── runs/
    └── {run_id}/
        ├── task_state.json
        ├── trace.jsonl
        └── report.json
```

Session JSON 继续由 `SessionStore` 管理；Run 目录继续由 `RunStore` 管理。新增 `JsonTaskRepository` 和 `JsonApprovalRepository` 只保存 Web 控制状态与索引，不复制 `trace/report` 正文。

### 8.2 写入规则

- JSON snapshot 使用同目录临时文件，执行 `flush + os.fsync` 后 `Path.replace()` 原子替换；POSIX 再 fsync 父目录。无法完成持久写时返回 persistence error，不能假装成功。
- `trace.jsonl` 保持追加写；每行先完成 redaction，再序列化。
- 进程内 repository 以锁保护“读-改-写”；禁止 route 直接打开 JSON。
- 时间统一存 UTC RFC 3339；所有 schema 带 `schema_version`。
- 数据目录在 POSIX 创建为 `0700`、状态文件为 `0600`；Windows 使用当前用户 ACL，不授予其他本地账号写权限。权限无法收紧时 ready=false，不能继续写包含会话/工具结果的 JSON。
- 未识别的新字段读取时保留或忽略，但不得因增加字段破坏旧会话读取。
- 损坏 JSON 不静默覆盖：ready 返回 `503`，查询返回稳定错误，日志只记录路径和异常类型。
- 列表接口按 `updated_at DESC, id DESC` 稳定排序。
- 运行期并发读取：Session/Task 查询与 Runtime 写入同一文件时，原子替换保证不出现半截 JSON，读取允许短暂过期，不得在锁内等待 Agent 完成；`trace.jsonl` 运行中读取须容忍末行未完成，跳过不完整 JSON 行。

JSON 跨文件迁移没有数据库事务，必须按以下顺序并 fail closed：

| 迁移 | 写入顺序 | 第二步失败时的补偿 |
| --- | --- | --- |
| 请求审批 | 写 Task `waiting_for_approval` -> 写 pending Approval | 将已创建的 Approval 写 `cancelled/persistence_error`（若存在），并将 Task 回滚为 `running`；不进入等待，Run failed |
| 批准/拒绝/过期 | 写 Task `running` -> 写 Approval outcome | 将 Task 回滚为 `waiting_for_approval`、Approval 回滚为 `pending`；补偿无法确认时 ready=false，工具不得执行 |
| 取消等待审批 | 写 Task `cancel_requested` -> 写 Approval `cancelled` | cancellation token 仍生效；记录补偿失败，终态收敛时重试 Approval 写入 |
| Run 终态 | 写 Run artifacts -> 写 Task 终态 | Task 写失败则保持 active 位并令 ready=false，不接受新 Task；重启时以已落盘的 terminal `task_state.json` 收敛 Task |

审批迁移的两步都带相同 `transition_id`，Task 终态写校验 RunGate generation；启动 reconciliation 依据这些字段、只追加且 fsync 的 `recovery.jsonl` 以及 terminal `task_state.json` 识别未完成迁移。任何 approved 状态只有在对应 Task 成功恢复为 `running` 后才允许 ApprovalGate 返回 `APPROVED`。补偿写本身失败时服务立即 ready=false，保留 active 位并等待人工重启/reconciliation，不能继续接受任务。

### 8.3 启动恢复

FastAPI lifespan 启动时执行一次 reconciliation：

1. 扫描 Task JSON。
2. 对 `queued/running/waiting_for_approval/cancel_requested` 任务写入 `failed`、`stop_reason=service_restarted`。
3. 若已有 `task_state.json`，通过 `TaskState.from_dict()` 写入相同失败原因，并向 trace 追加 `run_interrupted`；若 report 尚不存在，写入最小恢复报告。
4. 将对应 pending approval 写入 `expired`。
5. 不尝试恢复 Python 调用栈，不修改已完成 Run 的 artifact。
6. 清除内存 active task 标记后才把 ready 置为 true。

V1 页面刷新通过 JSON snapshot 恢复；服务进程重启只恢复可查询终态，不恢复执行。

## 9. REST API 合同

### 9.1 通用规则

- 前缀固定为 `/api/v1`；健康检查不带版本前缀。
- JSON 使用 `snake_case`；时间为 UTC RFC 3339。
- 成功响应直接返回资源对象，不再套无信息的 `data` 层。
- 所有响应带 `X-Request-ID`；客户端值只在匹配 `[A-Za-z0-9._-]{1,64}` 时接受，否则忽略并生成 `req_` + UUID4 hex，防止日志注入。
- 错误响应固定为：

```json
{
  "error": {
    "code": "active_task_exists",
    "message": "another root task is active",
    "details": {"task_id": "task_..."},
    "request_id": "req_..."
  }
}
```

不得把 Python traceback、绝对内部 artifact 路径、模型 API Key 或未脱敏工具输出放进响应。

### 9.2 Health

#### `GET /health/live`

只证明进程和 event loop 可响应，返回 `200 {"status":"ok"}`，不检查模型网络。

#### `GET /health/ready`

检查配置已加载、Workspace 配置有效、数据目录可读写、repository reconciliation 已完成、TaskRunner 可用。Right Code 网络不作为 ready 的持续依赖，避免短时外部故障让本地服务退出负载。

### 9.3 Workspace

#### `GET /api/v1/workspaces`

返回允许选择的 Workspace：

```json
{
  "items": [
    {
      "workspace_id": "threadforge",
      "name": "ThreadForge",
      "display_path": "D:/.../ThreadForge",
      "available": true,
      "is_git": true,
      "execution_environment": "backend_process",
      "container_sandbox_enabled": false
    }
  ]
}
```

### 9.4 Session

#### `POST /api/v1/sessions`

请求：

```json
{"workspace_id":"threadforge","title":"ThreadForge work"}
```

返回 `201`。`workspace_id` 创建后不可修改；title 为空时使用稳定默认值，不调用模型生成标题。

#### `GET /api/v1/sessions?limit=50&offset=0`

返回 Session 摘要列表与 `total/limit/offset`。V1 数据量小，使用 offset 分页即可。

#### `GET /api/v1/sessions/{session_id}`

支持 `message_limit`（默认 100，范围 1-500），返回 Session 元数据、最近消息历史、`message_total/has_more` 和关联 Task 摘要；单条公开 message content 经 redaction 后最多 4000 字符，完整 Session JSON 不直接下发。不存在返回 `404 session_not_found`；JSON 损坏返回 `500 session_corrupted`，不得自动新建同 ID Session。

### 9.5 Task

#### `POST /api/v1/tasks`

请求：

```json
{
  "session_id": "ses_...",
  "input": "分析当前项目结构",
  "max_steps": 6
}
```

- `input` 去除首尾空白后必须非空，长度不得超过 `THREADFORGE_TASK_INPUT_MAX_CHARS`。
- `max_steps` 只能在服务端允许范围（`1-25`，见配置 `THREADFORGE_MAX_STEPS`）内；省略时使用配置默认值。
- `session_id` 不存在返回 `404 session_not_found`；Task 继承 Session 的 Workspace，请求体不得传路径、provider、API Key 或 approval policy。
- 模型配置不完整（`PICO_OPENAI_API_BASE/API_KEY/MODEL` 任一缺失）时返回 `503 model_not_configured`，不得先创建永久 `queued` Task（见 7.1）。
- TaskRunner 不可用或 submit 失败时不得留下永久 `queued` 记录；服务将其收敛为 `failed`，释放活动位，并返回 `503 task_runner_unavailable`，error details 带已写入审计记录的 `task_id`。
- 成功时先持久化 `queued` Task，再提交 TaskRunner，返回 `202`：

```json
{
  "task_id": "task_...",
  "run_id": "run_...",
  "session_id": "ses_...",
  "status": "queued",
  "events_url": "/api/v1/tasks/task_.../events"
}
```

已有活动 root Task 返回 `409 active_task_exists`。

#### `GET /api/v1/tasks/{task_id}`

返回 Task snapshot，至少包含：

```json
{
  "task_id": "task_...",
  "run_id": "run_...",
  "session_id": "ses_...",
  "workspace_id": "threadforge",
  "status": "waiting_for_approval",
  "input": "...",
  "final_answer": null,
  "stop_reason": null,
  "pending_approval": {
    "approval_id": "apr_...",
    "tool_name": "run_shell",
    "args_preview": {"command": "python -m pytest -q"},
    "created_at": "...",
    "expires_at": "..."
  },
  "created_at": "...",
  "updated_at": "...",
  "execution_environment": "backend_process",
  "container_sandbox_enabled": false
}
```

`input/final_answer/args_preview` 均经过 secret redaction。运行时轮数、工具步数可从最新 `task_state.json` 合并到响应，但 artifact 不存在时不得让 Task snapshot 查询失败。

#### `POST /api/v1/tasks/{task_id}/cancel`

- `queued` 直接转 `cancelled`；活动任务写 `cancel_requested`、设置 token、唤醒审批和终止 Shell。
- 返回 `202` 和最新 Task snapshot；终态重复调用返回 `200` 和原终态。
- SSE 断开不触发取消。

#### `POST /api/v1/tasks/{task_id}/approvals/{approval_id}`

请求：

```json
{"decision":"approved"}
```

`decision` 仅允许 `approved/rejected`。接口必须校验 task、run、tool call、pending 状态和过期时间，再持久化决策并唤醒 Runner。未知审批返回 `404`，stale 或冲突决策返回 `409`。

### 9.6 Run artifacts

#### `GET /api/v1/runs/{run_id}/artifacts`

返回固定 allowlist 中实际存在的 artifact 元数据：

```json
{
  "run_id": "run_...",
  "items": [
    {
      "name": "task_state",
      "media_type": "application/json",
      "size_bytes": 1234,
      "sha256": "...",
      "content_url": "/api/v1/runs/run_.../artifacts/task_state"
    }
  ]
}
```

为使“查询 artifact”闭环可用，增加支持接口：

#### `GET /api/v1/runs/{run_id}/artifacts/{name}`

`name` 只允许 `task_state/trace/report`，分别映射固定文件名。禁止接收文件路径，禁止目录遍历。JSON 返回解析后的 JSON；trace 以 `application/x-ndjson` 返回。超过 `THREADFORGE_ARTIFACT_MAX_BYTES` 返回 `413 artifact_too_large`，不把超限文件一次性读入内存。

## 10. SSE 合同

### 10.1 连接

`GET /api/v1/tasks/{task_id}/events` 返回 `text/event-stream`，响应头至少包含：

```text
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

连接建立顺序必须在 EventBroker 的同一临界区内完成“注册 subscriber -> 读取当前 snapshot”，随后第一条发送 `task.snapshot`。这样页面刷新不依赖历史事件重放，也不会在订阅与快照之间丢失状态。`task_id` 不存在时先返回 `404 task_not_found`，不进入事件流。

### 10.2 事件信封

```text
id: evt_...
event: approval.required
data: {"event_id":"evt_...","sequence":7,"type":"approval.required","timestamp":"...","task_id":"task_...","run_id":"run_...","data":{...}}
```

字段固定为：

| 字段 | 语义 |
| --- | --- |
| `event_id` | 当前进程内唯一事件 ID |
| `sequence` | 当前 Task 内单调递增序号，仅保证当前进程生命周期 |
| `type` | 公开事件类型 |
| `timestamp` | UTC RFC 3339 |
| `task_id/run_id` | 归属标识 |
| `data` | 经过 schema 校验和 redaction 的类型化 payload |

### 10.3 V1 事件类型

| 事件 | 触发时机 |
| --- | --- |
| `task.snapshot` | 每次 SSE 连接建立后的第一条消息 |
| `task.queued` | Task 已持久化并进入 Runner |
| `task.started` | Runtime 开始执行 |
| `model.started` | 发起一次真实模型请求前 |
| `model.completed` | 模型响应返回并完成解析；只含类型和 usage 摘要，不泄露原始响应 |
| `message.completed` | 产生真实最终回答；非流式模型只发这一种消息内容事件 |
| `tool.requested` | 模型工具调用已解析且参数校验通过；风险工具此时尚未执行 |
| `tool.started` | 审批（如需要）通过且最后一次取消检查通过，即将实际执行 |
| `tool.completed` | 工具成功或 partial success |
| `tool.failed` | 参数校验失败、工具拒绝、超时或执行失败 |
| `approval.required` | pending approval 已持久化后 |
| `approval.resolved` | 批准、拒绝、过期或取消已持久化后 |
| `task.cancel_requested` | 取消请求已持久化并传播后 |
| `task.cancelled` | Runtime 已停止且资源已清理 |
| `task.completed` | Task 成功终止 |
| `task.failed` | Task 失败终止 |
| `policy.violation` | legacy `sandbox_violation` 的 V1 公共映射 |
| `error` | 可向用户展示的非敏感运行错误 |

V1 禁止发送 `message.delta`、`container_sandbox.*` 或假造的 planning/agent 事件。

终态事件顺序固定：只有 `TaskState.status=completed` 且 `stop_reason=final_answer_returned` 时，才先持久化包含 final answer 的 Task snapshot，再发布 `message.completed`，最后发布 `task.completed`。step/retry limit 的系统停止提示、取消和失败不发布 `message.completed`，只进入 Task snapshot/对应终态事件。终态事件发布后不得再出现新的 model/tool/approval 事件。

### 10.4 连接与背压

- 每个 subscriber 使用容量为 `THREADFORGE_SSE_QUEUE_SIZE` 的有界队列；慢消费者队列满时关闭该连接并记录指标，不阻塞 Runtime 线程。
- 按 `THREADFORGE_SSE_HEARTBEAT_SECONDS`（默认 15 秒）发送 SSE comment heartbeat；heartbeat 不占业务 sequence。
- 浏览器断线不取消 Task；重连后以 `task.snapshot` 恢复当前状态。
- EventBroker 不作为持久化真相来源。Task/Approval 终态必须先写 repository，再发布事件。
- V1 不承诺 `Last-Event-ID` 跨进程重放；若请求携带该头，可忽略并由 snapshot 收敛状态。
- 终态事件（`task.completed/cancelled/failed`）发布后服务端结束该 SSE 流；对已终态 Task 重连时返回终态 `task.snapshot` 后立即结束，不再发送 heartbeat。
- Web client 收到终态事件或终态 snapshot 后必须主动 `EventSource.close()`，避免浏览器对已正常结束的终态流自动重连。

### 10.5 Legacy 事件翻译

`PublicEventSink` 组合 `JsonlSink`，不能替代原审计 trace。映射固定为：

| Legacy trace | Public SSE |
| --- | --- |
| `run_started` | 无；`task.started` 由 TaskRunner 发布，避免重复 |
| `model_requested` | 无；`model.started` 由 ExecutionBoundary 发布，避免取消竞态 |
| `model_parsed` | 无；`model.completed` 由 ExecutionBoundary 在取消复检后发布 |
| `tool_executed` | 无；`tool.completed/tool.failed` 由 ExecutionBoundary 发布 |
| `run_finished` | 由 TaskRunner 根据终态发布 `message.completed` 与 Task 终态事件 |
| `sandbox_violation` | `policy.violation` |

`model.started`、`tool.requested`、`tool.started`、`approval.*` 和取消事件由新的 Runtime hook/Application Service 生成，不从事后 trace 猜测。取消请求持久化是线性化点：其后不得发布新的 `model.started/tool.started`；已经发布 `tool.started` 的调用属于活动步骤，Shell 必须终止，短暂的同步文件写入允许完成，但完成后不得开始下一步。

## 11. Runtime 最小改造

Web 构造 Runtime 时必须显式传入：

```python
Pico(
    ...,
    session_store=SessionStore(settings.data_dir / "sessions"),
    run_store=RunStore(settings.data_dir / "runs"),
)
```

model client 以 `timeout=THREADFORGE_MODEL_TIMEOUT_SECONDS`、`max_attempts=1`、`temperature=THREADFORGE_MODEL_TEMPERATURE` 构造；`Pico` 以 `max_steps=task.max_steps`、`max_new_tokens=THREADFORGE_MAX_NEW_TOKENS` 构造，`allowed_tools` 固定为下方 `WEB_V1_ALLOWED_TOOLS`。NativeRuntimeAdapter 启动 Run 前要断言两个 store 的 resolved root 均位于 `THREADFORGE_DATA_DIR`，且不位于 Workspace 内；断言失败则在调用 `Pico.__init__` 前 fail closed。实际注入 Runtime 的 RunStore 是持有该 rooted RunStore 和 Run lease 的 `FencedRunStore` 代理，forced shutdown 后拒绝旧 generation 的迟到 artifact 写入。集成测试必须证明一次 Web Run 不会在 Workspace 中创建 `.pico`。CLI 继续使用 Workspace 内 `.pico`，两条路径不得共享 builder 默认值。

Web Runtime 的 `allowed_tools` 固定为：

```python
WEB_V1_ALLOWED_TOOLS = (
    "list_files",
    "read_file",
    "search",
    "run_shell",
    "write_file",
    "patch_file",
)
```

不得从请求体覆盖该 allowlist。`delegate` 在 V1 Web 中不可见且调用会得到 `tool_not_allowed`；否则 native delegate 会创建额外 child Task/Run，使 `Task 1:1 Run`、SSE 归属和单活动 root Task 合同失真。

### 11.1 预分配 ID

向 native 调用增加向后兼容的关键字参数：

```python
Pico.ask(user_message, *, task_id=None, run_id=None)
```

未传时保持现有自动生成行为；Web adapter 必须传 API 已持久化的 ID。AgentLoop 创建 `TaskState` 时使用传入值。现有 CLI、测试和 delegate 调用不需要修改。

### 11.2 CancellationToken

新增 Runtime 无关的最小接口：

```python
class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...
```

没有注入 token 时使用永不取消实现。必须在以下边界检查：

1. 进入 AgentLoop 前。
2. 每轮开始、构建 prompt 前。
3. 模型调用前。
4. 模型返回后、解析/执行工具前。
5. 审批等待期间。
6. 工具参数校验后、实际执行前。
7. 工具返回后、开始下一轮前。
8. 写最终回答和 durable memory 前。

单独的 `ExecutionHooks` 协议负责把“检查取消 + 公开事件”放在同一个 RunGate 临界区；CLI 使用 no-op hooks，Web 注入 `ExecutionBoundary`：

```python
class ExecutionHooks(Protocol):
    def before_model(self, task_state) -> None: ...
    def after_model(self, task_state, metadata) -> None: ...
    def tool_requested(self, task_state, tool_call) -> None: ...
    def before_tool(self, task_state, tool_call) -> None: ...
    def after_tool(self, task_state, result) -> None: ...
```

`before_model/tool_requested/before_tool` 在 RunGate 内检查 token 并发布对应事件；`after_model` 在同一门上复检取消，取消已持久化时丢弃响应且不发 `model.completed`；`after_tool` 允许为已经开始的活动工具发布完成/失败，再由 AgentLoop 在下一边界收敛取消。EventBroker 发送失败只记录日志和关闭受影响 subscriber，不能放开已取消步骤或让 Runtime 失败。

取消抛出专用 `RunCancelled`，由 AgentLoop 捕获并写入：

```text
TaskState.status = stopped
TaskState.stop_reason = user_cancelled
```

随后仍执行 best-effort 的 task state、trace 和 report finalization。取消收敛后 `ask()` 正常返回（不向 adapter 抛出）；adapter 以 `TaskState.status == stopped` 且 `stop_reason == user_cancelled` 为信号发布 `task.cancelled`，不得发送 `message.completed`。最终回答一律从 `TaskState.final_answer` 读取。

### 11.3 Web ApprovalStrategy

把 `Pico.approve(name, args)` 改为委托 strategy，但保留现有 `ask/auto/never` 行为：

```python
class ApprovalOutcome(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

class ApprovalStrategy(Protocol):
    def decide(self, request: ApprovalRequest) -> ApprovalOutcome: ...
```

- CLI `ask` strategy 继续使用 `input()` 并返回 `APPROVED/REJECTED`；`auto/never` 分别返回固定 outcome，现有工具可见行为不变。
- Web strategy 调用 `ApprovalGate.request()`：先持久化 pending approval，再发布事件，然后在 Runner 线程中等待 condition/event。
- FastAPI event loop 和 HTTP 请求线程不得等待 Agent 完成。
- 等待可被批准、拒绝、超时和 cancellation token 唤醒；等待以持久化审批状态为谓词（状态离开 `pending` 即返回），不依赖 notify/condition 计数，避免决策在等待开始前到达被丢失。
- Gate 返回 `APPROVED` 前，必须用阻塞线程持有的原始 args 重新计算 HMAC，并使用 constant-time compare 与持久化 `args_digest` 比较；不匹配返回 `REJECTED` 并记录 `approval_digest_mismatch`。
- `ToolExecutor` 将 `REJECTED` 映射为 `approval_denied`，将 `EXPIRED` 映射为 `approval_expired`；`CANCELLED` 触发 `RunCancelled`，不能伪装成工具拒绝。
- `APPROVED` 只代表当前精确 tool call 获批；`ToolExecutor` 在实际执行前再次检查 cancellation token，下一次风险工具必须创建新 approval。

### 11.4 Shell 取消

`run_shell` 从一次性 `subprocess.run()` 改为可跟踪的受管进程句柄（Linux 使用 `Popen`，Windows 使用 suspended Win32 process + Job Object）：

- Linux 使用独立 process group/session；取消或超时时终止整个 group，宽限期后强制 kill。
- Windows 必须创建带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object，将 suspended 状态的 `cmd.exe` 先加入 Job 再 resume，且不允许 breakaway；取消或超时时终止 Job。`taskkill /T /F` 只能作为清理 fallback，不能作为唯一实现。
- 如果 Windows Job Object 创建、进程分配或 resume 失败，Web `run_shell` 必须在命令开始前 fail closed，返回 `process_containment_unavailable`，不得降级为无约束执行。
- 平台实现封装在 Legacy Runtime 的 `pico/shell_process.py`，避免平台分支散落，也避免 Runtime 反向依赖 API 包。
- 继续使用过滤后的环境变量、Workspace cwd、输出裁剪和 1-120 秒 timeout。
- stdout/stderr 必须避免管道死锁；读取线程持续 drain 两个 pipe，但合计只保留 `THREADFORGE_SHELL_OUTPUT_MAX_BYTES`，超出部分丢弃并设置 `output_truncated=true`，不能因停止读取而让子进程阻塞。
- 终止完成并确认受管 process group/Job 中无活动进程前，Task 不得进入 `cancelled` 终态；确认失败时 Task 进入 `failed`、`stop_reason=process_cleanup_failed`，不得虚报取消成功。
- Shell 取消、超时和非零退出必须在 tool metadata 中可区分。
- Linux 中故意调用 `setsid`/daemonize 逃离 process group 的命令不属于 V1 可证明的清理范围；V1 依赖逐次人工审批并在 UI 明示该限制，只有后续独立容器/cgroup 沙盒才能形成对抗性硬边界。

### 11.5 同步模型请求

Right Code 客户端当前使用同步 HTTP。V1 行为固定为：

- AgentLoop 在线程中发请求，FastAPI event loop 不被阻塞。
- `OpenAICompatibleModelClient.__init__` 新增向后兼容的 `max_attempts=3` 参数并校验为正整数；CLI 不传该参数，继续保持当前三次尝试行为。
- Web adapter 必须传 `timeout=THREADFORGE_MODEL_TIMEOUT_SECONDS` 和 `max_attempts=1`。Web 的一次模型步骤因此最多只有一个在途 HTTP 尝试，不会出现取消后继续发第二、第三次重试。
- 当前 `urllib` timeout 是 socket 操作超时，不是可强杀线程的严格 wall-clock deadline。Task 在请求返回/抛出前保持 `cancel_requested`，UI 不承诺精确 120 秒内进入终态；有限单次 timeout 保证不会再叠加三轮重试和退避。
- 用户取消无法保证立即中断已发请求；token 立即标记取消。
- 请求返回或超时后先检查 token，迟到响应不得写为最终回答、不得触发工具、不得开始下一次模型调用。

## 12. TaskRunner、审批与事件协作

### 12.1 TaskRunner

V1 使用一个受控 daemon Runner 线程，不使用 `ThreadPoolExecutor`。原因是正在执行同步模型请求的 executor 线程无法被 Python 强制终止，且解释器退出会等待它；daemon 线程配合有限模型 timeout 和终态 fencing 才能让服务关闭时间有界。

1. `TaskService` 原子创建 queued Task。
2. Service 发布 `task.queued`，再由 `TaskRunner.submit()` 创建并登记唯一 daemon 线程处理不可变 `RunRequest`；线程启动失败立即收敛为 failed。
3. Runner 写 `running` 并发布 `task.started`，再构建独立 Runtime、SessionStore、RunStore、EventSink 和 ApprovalStrategy（`SessionStore`/`RunStore` 必须 rooted 于 `THREADFORGE_DATA_DIR`，见 4.2）。
4. 调用 `NativeRuntimeAdapter.run()`。
5. 在 `finally` 中收敛 Task 终态、清理 approval/shell/token、释放 active task。

`NativeRuntimeAdapter.run()` 必须捕获模型、解析、工具和持久化异常：如果 `agent.current_task_state` 仍为 `running`，调用现有 `finalize_failed_run()` 写入失败 task state、trace 和 report；如果 Runtime 已经写入终态，不重复覆盖。公开 `task.completed/cancelled/failed` 只由 TaskRunner 根据最终 TaskState 发布一次，不能直接按“是否抛异常”猜测结果。

shutdown 等待超过 `THREADFORGE_MODEL_TIMEOUT_SECONDS + THREADFORGE_SHELL_CLEANUP_GRACE_SECONDS` 后，TaskRunner 先提升该 Run 的 fencing generation，再由 coordinator 写入 Task 与 Run artifact 的 `failed/service_shutdown_timeout` 终态；`FencedRunStore`、ApprovalGate 和 EventBroker 随后拒绝旧 generation 的迟到写入/事件。迟到 Runner 只能完成进程内局部清理，daemon 线程最终由进程退出回收；JSON 原子写保证不会留下半截 snapshot，trace reader 继续容忍最后一行未完成。

禁止使用 FastAPI `BackgroundTasks` 直接承载长时间 Agent，因为其生命周期、取消和状态所有权不足以满足本合同。

### 12.2 写入与发布顺序

控制面使用每个活动 Run 独立的 `RunGate` 作为线性化锁，具体顺序固定：

| 操作 | RunGate 内顺序 | RunGate 释放后 |
| --- | --- | --- |
| 创建 Task | 持久化 queued -> 发布 `task.queued` -> 登记 Runner | 启动 daemon Runner |
| 解决审批 | 校验 -> 持久化 outcome -> 写 Task `running` -> 发布 `approval.resolved` -> 设置内存 outcome/notify | Runner 被唤醒 |
| 请求取消 | 持久化 `cancel_requested` -> 设置 cancellation token -> 发布 `task.cancel_requested` | 唤醒审批并终止活动 Shell |
| 开始模型 | 检查 token -> 发布 `model.started` | 发出已经线性化的单次 HTTP 请求 |
| 开始工具 | 检查 token -> 发布 `tool.started` | 执行已经线性化的活动工具 |
| 写终态 | 校验 fencing generation -> 持久化终态 -> 发布终态事件 | 清理并释放 active task |

任何 publish 前都必须已释放 repository 文件锁，但可继续持有短时 RunGate。这样 `approval.resolved` 一定先于获批工具的 `tool.started`，`task.cancel_requested` 一定先于取消终态，并且取消持久化后不可能再线性化新的模型/工具步骤。工具审计 trace 仍由 Runtime 自身写入；公开终态以 TaskRepository snapshot 为准。

### 12.3 线程安全

- Task/Approval repository 使用进程内可重入锁。
- EventBroker 从 Runtime 线程发布到 asyncio subscriber 时使用 loop 的线程安全调度入口。
- ApprovalGate 使用 `threading.Condition/Event`，不持有 repository 锁等待用户。
- Lock 顺序固定为 `active-task lock -> RunGate -> repository lock -> approval gate lock`；任何路径不得逆序。
- FencedRunStore 的“校验 generation + 底层 artifact 写入”必须在同一个 RunGate 临界区完成，避免 forced shutdown 在校验和 replace 之间插入并被旧 Runner 覆盖。
- 每次 TaskRepository 终态写入必须在 RunGate 内校验 fencing generation；forced shutdown 后的迟到 Runner 写入被拒绝并记录 `stale_runner_write_rejected`。
- Event publish 和日志调用不得发生在 repository 文件锁内部。

## 13. FastAPI 实现要求

### 13.1 App factory 与 lifespan

提供 `create_app(settings: Settings | None = None) -> FastAPI`，测试不得依赖模块导入时创建真实目录或线程。

launcher/lifespan 启动顺序：

1. 加载并校验 Settings。
2. 只读解析 WorkspaceCatalog，先验证数据目录与每个 Workspace 互不包含，且 workspaces 配置文件位于所有 Workspace 外；验证完成前不得在 Workspace 中创建任何文件。
3. launcher 创建数据目录、取得 process lock，再启动 Uvicorn；直接绕过 launcher 启动 ASGI app 属于不受支持的运行方式。
4. 初始化 repositories 并执行 startup reconciliation。
5. 创建 EventBroker、ApprovalGate、TaskRunner 和 services。
6. 标记 ready。

关闭顺序：

1. ready=false，停止接收新 Task。
2. 请求取消活动 Task 并唤醒审批。
3. 在 `THREADFORGE_MODEL_TIMEOUT_SECONDS + THREADFORGE_SHELL_CLEANUP_GRACE_SECONDS` 内等待 Shell/Runner 清理；超时则写 `service_shutdown_timeout` 并提升 fencing generation。
4. 关闭 EventBroker subscriber。
5. 标记 Runner 关闭并退出 lifespan；迟到 daemon Runner 不再拥有控制状态写权限。
6. `uvicorn.run()` 返回后 launcher 立即结束，不再执行应用逻辑；process lock 由 OS 在进程 teardown 时释放。

### 13.2 中间件

- Request ID：验证或生成 ID，并写响应头、日志上下文和错误合同。
- CORS：只允许配置的本地 Web origin 和必要方法/请求头。
- Trusted Host：仅 loopback host/配置的本地开发 host。
- 使用同步 JSON repository/文件锁的 REST handler 必须定义为 FastAPI `def` 或显式 `anyio.to_thread.run_sync`；不得在 event loop 上直接做文件 I/O。SSE generator 保持 async，Runtime 只通过线程安全 EventBroker 与其交互。
- 访问日志：记录 method、route template、status、duration、request_id；不得记录 task input、approval args、Authorization 或 API Key。
- 全局异常处理：领域错误到稳定 HTTP 状态；未知错误返回通用 `internal_error`。

### 13.3 OpenAPI

- 所有公开 request/response/error/event payload 使用 Pydantic 模型。
- 为 `202/409/404/422/500` 等实际响应声明 schema。
- SSE endpoint 的 OpenAPI description 链接到本文事件合同，不伪装为普通 JSON 响应。
- `/docs` 与 `/openapi.json` 由 `THREADFORGE_OPENAPI_ENABLED` 控制；loopback 开发默认开启，生产 Compose 固定关闭。

## 14. 分阶段执行清单

以下顺序按依赖排列。每个工作包单独可审查，未满足完成定义不得进入下一阶段。

### Phase 0：合同冻结与基线

- [ ] `B0.1` 将当前完整 pytest 结果、Python/OS 和耗时记录到 CI artifact。
- [ ] `B0.2` 为 Session JSON、TaskState、trace、report 建立 golden/contract fixtures。
- [ ] `B0.3` 冻结 native `Pico.ask()`、工具审批、路径逃逸、secret redaction 和 CLI 公共行为。
- [ ] `B0.4` 为 legacy `sandbox_violation -> policy.violation` 添加兼容测试。
- [ ] `B0.5` 将本文中的 REST、SSE、状态机和错误码转为测试用 schema fixtures。
- [ ] `B0.6` 在 Windows CI 做 Shell containment spike：用 Job Object 覆盖普通子进程、孙进程和 `cmd /c start /b`；若无法在 resume 前完成 Job 分配，Phase 3 不得以弱化取消语义继续。

完成定义：未增加功能的 baseline commit 在 Linux/Windows、Python 3.10/3.12 CI 可重复通过；所有后续 Runtime 改动均有合同保护。

### Phase 1：API 工程与健康检查

- [ ] `B1.1` 创建 `api-server/pyproject.toml`、src layout、app factory 和测试配置。
- [ ] `B1.2` 实现 Settings、配置校验、request ID、日志和错误处理中间件。
- [ ] `B1.3` 实现 lifespan container 与 `/health/live`、`/health/ready`。
- [ ] `B1.4` 更新 CI：安装 legacy runtime 和 api-server，执行 lint、unit、contract、build。
- [ ] `B1.5` 构建 wheel，确认包中包含 routers、services 和 infrastructure 模块。

完成定义：无真实模型凭据也可启动服务；live/ready、OpenAPI 和错误合同测试通过；导入模块不产生文件系统副作用。

### Phase 2：Workspace 与 JSON repositories

- [ ] `B2.1` 实现 WorkspaceCatalog、allowlist 配置和 canonical path 校验。
- [ ] `B2.2` 扩展 SessionStore 原子写和列表能力，保持旧调用兼容。
- [ ] `B2.3` 实现 Task/Approval JSON repository、schema version、transition_id/generation、稳定排序和跨文件 fail-closed 补偿。
- [ ] `B2.4` 实现启动 reconciliation 和损坏 JSON 处理。
- [ ] `B2.5` 实现 Workspace/Session application service 与 REST 路由。

完成定义：不能通过 API 选择未配置路径；Session 可创建、列表、详情和恢复；并发读取不会看到半截 JSON。

### Phase 3：Runtime 可插拔改造

- [ ] `B3.1` 为 `Pico.ask()` 增加可选预分配 task/run ID。
- [ ] `B3.2` 引入 CancellationToken、RunCancelled、ExecutionHooks 和 AgentLoop/ToolExecutor 的线性化边界检查。
- [ ] `B3.3` 引入 ApprovalStrategy/ApprovalOutcome，改造 ToolExecutor 的拒绝、过期、取消映射，并保留 CLI `ask/auto/never` 兼容测试。
- [ ] `B3.4` 将 Shell 改为 Linux process group/Windows Job Object；增加超时、取消、partial success、普通子进程、孙进程、Windows `start /b` 与 containment 初始化失败测试。
- [ ] `B3.5` 为 OpenAI-compatible client 增加 `max_attempts` 参数；合同测试证明 CLI 默认三次、Web 明确一次，且取消后不会再发重试请求。
- [ ] `B3.6` 增加 `user_cancelled/process_cleanup_failed` stop reason 和最终 artifact 合同测试。

完成定义：原 CLI 行为和全量 legacy 测试通过；FakeModelClient 集成测试证明取消后不会开始新模型/工具步骤；Linux 受管 process group 与 Windows Job Object 中的测试进程均被清理，清理无法确认时不得写 `cancelled`。

### Phase 4：TaskRunner、审批与事件

- [ ] `B4.1` 实现 NativeRuntimeAdapter 与 FencedRunStore，每个 Run 新建 Runtime；必须注入 rooted 于 `THREADFORGE_DATA_DIR` 的 SessionStore/RunStore（见 4.2），把预分配 task/run ID 传入 `Pico.ask()`，并用 `finalize_failed_run()` 收敛仍处于 running 的异常 Run。
- [ ] `B4.2` 实现单 worker TaskRunner 和全局 active task 原子约束。
- [ ] `B4.3` 实现 ApprovalGate 的持久化、等待、决策、超时、取消和 stale 语义。
- [ ] `B4.4` 实现 EventBroker、有界 subscriber 队列和 heartbeat。
- [ ] `B4.5` 实现 PublicEventSink/Translator，保留原 JsonlSink。
- [ ] `B4.6` 实现 shutdown 取消和资源清理。

完成定义：后台任务不占用 event loop；审批 REST 可以唤醒精确工具调用；SSE 断开不取消 Task；取消审批等待和活动 Shell 均可最终到达 `cancelled`。

### Phase 5：Task、SSE 与 Artifact API

- [ ] `B5.1` 实现 TaskService 创建、查询、取消和终态收敛。
- [ ] `B5.2` 实现 Task REST 路由和全量状态码。
- [ ] `B5.3` 实现 approval REST 路由与幂等/冲突处理。
- [ ] `B5.4` 实现 SSE endpoint、`task.snapshot` 和全部 V1 事件 schema。
- [ ] `B5.5` 实现 artifact 列表与固定名称内容查询。
- [ ] `B5.6` 使用 FakeModelClient 完成只读、审批批准、审批拒绝、取消和刷新恢复集成测试。

完成定义：V1 核心 API 有 OpenAPI/contract test；端到端闭环不依赖真实模型；所有输出经过 redaction。

### Phase 6：Right Code、交付与验收

- [ ] `B6.1` 从服务端环境构建 Right Code client，加入配置缺失和超时错误映射，并固定传入 `max_attempts=1`（见 11.5）。
- [ ] `B6.2` 编写 `.env.example`，不得包含真实 secret。
- [ ] `B6.3` 提供 Windows 本地启动脚本/文档和前后端开发 origin 配置。
- [ ] `B6.4` 提供 Linux API Dockerfile 与根目录 `compose.yaml`，只绑定 loopback。
- [ ] `B6.5` Compose 挂载 Workspace 和数据卷，并在 README 明示工具在宿主/控制容器内执行、尚无独立沙盒。
- [ ] `B6.6` 完成真实 Right Code 人工 smoke test，不把凭据或响应正文上传 CI artifact。
- [ ] `B6.7` 完成下文验收矩阵和故障注入。

完成定义：新环境按文档可启动；API/Web 闭环可用；CI、Docker smoke、真实 provider smoke 和 V1 验收全部通过。

## 15. 测试策略

### 15.1 单元测试

- Settings/launcher：缺失配置、非法 host、非法 origin，以及 Uvicorn 调用固定 `workers=1`。
- Data directory：Workspace 包含关系、POSIX mode/Windows ACL、不可写目录和 process lock 竞争。
- Workspace：不存在路径、重复路径、大小写差异、路径逃逸，以及 Session 创建后/Task 启动前发生的符号链接或联接点替换。
- Repository：原子写、排序、损坏 JSON、未知字段、并发读写、schema version。
- 跨文件迁移：在 Approval/Task/Run 每个写入边界注入失败，验证 transition_id reconciliation、approved 不执行和 active 位不提前释放。
- NativeRuntimeAdapter：两个 store 必须解析到数据目录；Workspace 内 store、默认 RunStore 和构造后新建 Workspace `.pico` 均判失败。
- Provider 配置隔离：Workspace `.env` 中伪造 `PICO_OPENAI_API_BASE/API_KEY/MODEL` 不影响 Web model client；Web 路径未调用 CLI project-env loader。
- Tool allowlist：Web prompt 不出现 `delegate`，模型强行返回 delegate 调用时不创建 child Task/Run；CLI 默认工具合同不变。
- Fencing：forced shutdown 提升 generation 后，迟到 Runner 的 Task、Approval、Run artifact 和 SSE 写入全部被拒绝；coordinator 写入的 `service_shutdown_timeout` 终态保持不变。
- Native 异常：FakeModelClient 在首轮/工具后抛异常时，TaskState、trace、report 和 API Task 均收敛为 failed，公开终态事件只出现一次。
- 状态机：所有允许迁移和非法迁移，终态不可变，active task 竞争。
- Approval：原始 args HMAC 与 constant-time 复核、digest mismatch、脱敏 preview、批准、拒绝、批准/取消竞态、重复同决策、冲突决策、超时、取消、重启失效。
- ExecutionBoundary：用 barrier 重复竞争取消与 `before_model/tool_requested/before_tool`，取消 sequence 之后不得出现新的 requested/started 事件。
- EventBroker：事件顺序、有界队列、慢消费者、断线、heartbeat、snapshot 原子性。
- Translator：legacy 事件映射、payload 裁剪、secret redaction、禁止虚假事件。
- Artifact：固定名称、目录遍历、缺失文件、损坏 JSON、大小限制、sha256。

### 15.2 Runtime 回归测试

- 原 CLI `ask/auto/never` 审批行为不变。
- 不传 task/run ID 时仍自动生成；传入时 artifacts 使用预分配 ID。
- 取消发生在模型前、模型等待中、模型返回后、审批等待中、Shell 运行中和工具完成后。
- 取消后模型迟到响应不会写 final answer 或执行工具。
- OpenAI-compatible client 的 CLI 默认 `max_attempts=3`；Web adapter 显式使用 `max_attempts=1`，5xx/网络错误和取消后均不会再发第二次 Web 请求。
- Linux Shell 取消能清理受管 process group；Windows Job Object 用普通子进程、孙进程和 `cmd /c start /b` 验证 kill-on-close，Job 初始化失败时命令未执行。
- 路径逃逸、read-only、tool allowlist、环境变量过滤和 redaction 不回归。

### 15.3 API 合同测试

- 每个 endpoint 的成功和所有声明错误状态。
- Pydantic schema 与 OpenAPI snapshot。
- `X-Request-ID` 正常/异常输入。
- CORS 和 Host 拒绝。
- Task 创建竞争只成功一个，其余返回 409。
- 终态取消幂等、approval stale/冲突语义。
- SSE media type、headers、首条 snapshot、事件信封、heartbeat，以及 `approval.resolved < tool.started`、`task.cancel_requested < task.cancelled` 顺序。

### 15.4 集成场景

| 场景 | Fake model 脚本 | 预期 |
| --- | --- | --- |
| 只读回答 | 直接返回 final | `message.completed -> task.completed`，artifacts 可查询 |
| 读取后回答 | `read_file -> final` | 自动执行只读工具，无 approval |
| 写入批准 | `write_file -> final` | `approval.required`；批准后只执行该调用 |
| 写入拒绝 | `patch_file -> final` | 文件不变；approval rejected 有审计 |
| Shell 取消 | `run_shell` 长命令及普通孙进程 | 受管 group/Job 清空，Task 最终 cancelled |
| Windows 脱壳尝试 | `cmd /c start /b` 派生标记进程 | 进程仍受 Job 约束；取消后标记进程不存在 |
| 模型等待时取消 | 延迟 Fake client | 返回后丢弃结果，不开始工具 |
| SSE 断线重连 | 执行中断开连接 | Task 继续；重连首条 snapshot 反映真实状态 |
| 服务重启 | pending approval 时重启 | Task failed/service_restarted，approval expired |
| 并发创建 | 同时 POST 两个 Task | 仅一个 202，另一个 409 |

### 15.5 测试命令目标

```powershell
python -m pip install -e .\pico-legacy-runtime
python -m pip install -e .\api-server

Push-Location .\pico-legacy-runtime
python -m pytest -q
Pop-Location

Push-Location .\api-server
python -m pytest -q
Pop-Location
```

CI 不调用真实 Right Code；所有自动化测试使用 deterministic FakeModelClient。真实 provider 仅做人工 smoke，并验证日志/artifact 不含 API Key。

## 16. V1 验收矩阵

| 需求 | 验收步骤 | 证据 |
| --- | --- | --- |
| 选择 Workspace | GET allowlist，创建绑定 Session，尝试伪造路径 | API contract + path escape test |
| 创建/恢复 Session | 创建、列表、详情；重启 API 后再次查询 | Session JSON + integration test |
| 创建 Task | POST 返回 202 和预分配 task/run ID | Task JSON + OpenAPI test |
| 观察执行 | SSE 首条 snapshot，随后 lifecycle/model/tool 事件 | 录制的 NDJSON/SSE fixture |
| 只读最终回答 | Fake/Right Code 返回 final | Task completed + report.json |
| 风险工具审批 | 三种风险工具逐次触发，分别批准/拒绝 | Approval JSON + 文件断言 + trace |
| 停止 | 审批等待、模型等待、长 Shell 三处取消 | cancelled 状态 + 无后续 tool + 无残留进程 |
| Artifact 查询 | 列表并读取三种固定 artifact | sha256 + schema test |
| 单活动 Task | 并发提交两个 root Task | 一个 202、一个 409 |
| 页面刷新恢复 | 执行中/终态刷新并重连 SSE | task.snapshot + GET Task |
| 本地安全边界 | loopback、精确 CORS、secret redaction、后端执行/无沙盒警告 | config/security tests |
| 交付 | Windows 开发启动、Linux Compose smoke | CI artifact + 操作文档 |

所有验收项都必须产生自动化证据；只有真实 Right Code 连通性允许保留人工 smoke。

## 17. Docker 与本地运行目标

### 17.1 本地开发

目标命令：

```powershell
$env:THREADFORGE_WORKSPACES_FILE = "D:\path\to\workspaces.json"
$env:THREADFORGE_DATA_DIR = "D:\path\to\threadforge-data"
$env:PICO_OPENAI_API_KEY = "..."
python -m threadforge_api
```

热重载使用 `python -m threadforge_api --reload`，仍由 launcher 持有 process lock。`--reload` 仅限 loopback 开发；reload 会中断活动 Run，启动 reconciliation 会将其标记为 `service_restarted`。launcher 不提供 workers 参数，不得直接调用 Uvicorn 绕过该限制。

### 17.2 Linux Compose

Compose 必须：

- API 端口只发布到 `127.0.0.1`。
- 数据目录使用持久卷。
- 允许 Workspace 通过显式 bind mount 接入，不挂载宿主根目录、用户主目录或 Docker socket。
- API 容器以非 root 用户运行；挂载目录权限不足时 ready 失败并给出可操作错误。
- 设置 `stop_grace_period >= model timeout + shell cleanup grace + 5s`（默认配置至少 130 秒），让 TaskRunner 有机会取消 Shell 并完成 artifacts。
- 不把 `PICO_OPENAI_API_KEY` 写入镜像层或 compose 文件；通过环境/secret 注入。
- 明示这只是控制服务容器，不等于独立的不可信工具 sandbox。

## 18. 安全与可观测性门槛

- 新增公开 payload 必须先经过结构化 schema，再经过 secret redaction。
- 日志字段至少包含 `timestamp/level/request_id/task_id/run_id/event/duration_ms/error_type`。
- 不记录完整 prompt、模型原始响应、Shell 环境、API Key、Authorization、完整源码或审批 secret。
- 记录 Task 状态迁移、approval 决策、取消传播、Shell 终止结果和 EventBroker 慢消费者。
- `read_file/write_file/patch_file` 继续受 Workspace real-path 边界；`run_shell` 只保证 Workspace cwd、子进程环境 allowlist、逐次审批和受管进程生命周期，不声称具备文件系统或进程凭据隔离。
- 未实现容器隔离前，所有 UI/API Task snapshot 返回 `execution_environment: "backend_process"` 和 `container_sandbox_enabled: false`。
- HTTP 绑定到非 loopback、CORS wildcard、Uvicorn 多 worker、数据目录不可写均作为启动配置错误处理。

## 19. 风险与应对

| 风险 | 后果 | V1 应对 |
| --- | --- | --- |
| 同步模型请求不可立即取消 | 用户看到一段时间的 cancelling | Web 固定一次 HTTP 尝试和有限 socket timeout；返回后丢弃；不承诺严格 wall-clock 截止 |
| JSON 无跨进程事务 | 多进程竞争损坏状态 | 固定单进程/单 worker；原子写和进程锁 |
| Web 审批阻塞 Runner 线程 | 唯一 worker 被占用 | V1 只允许一个活动 Task；condition 可取消/超时 |
| SSE 非持久化 | 断线期间事件不可重放 | 首条原子 snapshot；终态和审批持久化；V1.2 再做可靠重放 |
| 后端运行环境工具执行 | 命令可能影响本机或控制服务容器 | loopback、逐次审批、文件工具路径边界、Shell 最小环境和醒目标识 |
| 已批准 Shell 读取后端资源 | 子进程可能访问 API 进程同权限可见的文件、进程信息或挂载 | 子进程 env 不含 key、命令逐次审批、最小权限运行、UI 明示；真正隔离留到 container sandbox |
| Shell 子进程残留 | 取消后仍改文件或占资源 | Linux process group、Windows Job Object；清理确认失败写 failed，不虚报 cancelled |
| Linux 命令主动 daemonize/setsid | 子进程可能逃离受管 process group | 逐次人工审批和 UI 警告；V1 不声称具备对抗性隔离，后续 container/cgroup sandbox 解决 |
| Legacy Runtime 改动破坏 CLI | 既有能力回归 | 所有新参数可选，Phase 0 合同测试，双平台全量测试 |
| Session/Task 双存储漂移 | Web 状态与 Runtime 历史不一致 | 明确所有权；Task 终态在 Runner finally 收敛并校验 artifacts |

## 20. 完成定义

FastAPI 后端重写只有在以下条件全部满足时才算完成：

- [ ] `api-server` 是可安装、可构建、可测试的独立 Python 包。
- [ ] 所有 V1 REST/SSE 合同均已实现并进入 OpenAPI/contract tests。
- [ ] FastAPI route 中不存在 AgentLoop、文件存储或审批等待业务逻辑。
- [ ] Web 默认使用 native backend，未引入数据库或伪 token streaming。
- [ ] 每个 Run 使用独立 Runtime；全局单活动 root Task 在并发测试中成立。
- [ ] 三个风险工具均为 `per_call_only`，拒绝/超时/断线均 fail closed。
- [ ] 取消后不再开始新模型/工具步骤；受管 Linux process group/Windows Job 完成清理，无法确认时以 failed 收敛而非虚报 cancelled。
- [ ] Session、Task 终态和三类 artifacts 在刷新后可恢复查询。
- [ ] legacy CLI 和完整测试矩阵无回归。
- [ ] Windows 本地开发和 Linux Compose smoke 通过。
- [ ] API、SSE、日志和 artifacts 通过 secret redaction 与路径安全测试。
- [ ] UI/API 明确显示工具在后端运行环境执行、尚无独立容器沙盒。

## 21. V1 后的演进接口

V1 只预留边界，不实现以下能力：

- `RuntimeAdapter` 可在后续增加 LangGraph 实现，但 V1 只注册 native。
- `TaskRepository` 可在 V1.2 替换为 SQLite，但 service 不依赖 JSON 细节。
- `EventBroker` 可在 V1.2 增加持久 journal 与 `Last-Event-ID` 重放。
- `TaskRunner` 可在后续替换为 Redis/独立 worker，但 REST/SSE 合同不改。
- Task/Run 已使用独立 ID，为重试、恢复和 `Task 1:N Run` 预留。
- `execution_environment/container_sandbox_enabled` 字段为后续独立容器沙盒提供明确升级点，不能在实现前返回虚假值。

这些预留不得表现为未实现 API、假数据或 `501` 空壳；对应里程碑真正开始时再扩展 OpenAPI。

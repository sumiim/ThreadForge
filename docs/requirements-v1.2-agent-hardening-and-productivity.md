# ThreadForge V1.2 Agent 安全加固与生产力需求

> 文档状态：需求基线（Draft）
> 前置条件：V1.1 最小可用 Agent 已完成
> 目标：显著提升核心 Coding Agent 的安全性、可恢复性、上下文质量和跨设备日常使用体验。

## 1. 版本定位

V1.2 不再解决“Agent 会不会正确结束”这一基础问题，而是把已可用的单 Worker Agent 提升为可以长期使用的工程产品。

本版本只接纳会直接增强核心工作流的能力：可靠事件和恢复、原生工具协议、Sandbox、本地资源浏览、受控互联网资源、长期图记忆、多 Worker 切换以及有证据收益的并行只读分析。

MCP、Skills、Marketplace 和第三方扩展不属于 V1.2。

## 2. 核心不变量

1. V1.1 的 Agent Turn、Evidence Ledger、完成门禁和 Review 不得回退。
2. 所有新能力继续受 owner、device、workspace、审批和审计约束。
3. 浏览器不得获得对本地文件或 Worker 的不受限控制权；中央服务负责身份、路由和事件顺序，同设备前端仅可通过受限本地资源桥按需访问已授权资源。
4. API Key、完整会话正文和本地文件内容默认保存在 Worker，不进入公共日志和 Git。
5. 并行只用于依赖图证明互不依赖的节点；写入节点必须隔离。
6. 长期记忆必须具备 provenance、freshness、confidence 和删除能力。
7. 新能力失败时基础任务可降级运行，不得把占位能力显示为可用。
8. 原始隐藏 CoT 不得发送到前端，不得写入会话历史、日志、审计、checkpoint、运行产物或长期记忆。

## 3. Application Service 与可靠控制面

### 3.1 统一服务边界

- FastAPI、Web、Electron 和 CLI 复用同一个 application service。
- 冻结 Workspace、Session、Task、Run、Approval、Artifact、Plan、Node 和 Evidence 的 ID 与状态迁移。
- 公开错误统一为 `code/message/details/request_id`。
- 生成 OpenAPI TypeScript client，避免多端手写 DTO 分叉。

### 3.2 持久化与迁移

- 使用 SQLite 保存控制面状态、SSE sequence、审批、Worker lease 和索引。
- 大体积 trace/report/patch 继续保存为本地 artifact，数据库只记录路径、摘要、大小和校验和。
- 提供现有 JSON SessionStore/RunStore 的幂等导入、备份、回滚和损坏数据处理。
- 多实例需求真实出现前完成 SQLite 到 PostgreSQL 的迁移演练，但 V1.2 单实例生产可以继续使用 SQLite。

### 3.3 SSE 与恢复

- 统一事件信封、单调 sequence、heartbeat、`Last-Event-ID`、重放、去重、乱序保护和慢消费者背压。
- 每次阶段切换、审批等待、工具终态、fan-in 和 Review 决策生成不可变 checkpoint。
- 恢复生成新 `run_id`，记录 `source_run_id/source_checkpoint_id`。
- 恢复前重新验证 owner、device、workspace、runtime、schema 和 Worker capability。
- 无法安全恢复时继续使用 `interrupted`，不得假装续跑任意 Python 栈。

## 4. Tool Registry 与模型工具协议

### 4.1 Registry

建立版本化 `ToolRegistry`：

```text
name, version, source
input_schema, output_schema
risk_level, permission_scope
approval_policy
executor, timeout, cancellation
audit_formatter
```

启动时拒绝重复名称、非法 schema、缺失执行器和不兼容版本。API、CLI、LangGraph 和未来扩展都通过相同 Registry 调用工具。

### 4.2 Pi 风格四工具模型界面

模型侧逐步收敛为：

```text
read(operation=list|read|search)
write
edit
bash
```

现有六工具继续通过兼容 adapter 工作。模型请求优先使用 provider 原生 JSON Schema/tool calling；旧 `<tool>` 解析器仅保留迁移期兼容，不再扩展。

### 4.3 安全要求

- 路径解析、符号链接、Shell 超时、进程树终止、只读模式和审批统一放在 application service/Worker 执行边界。
- 工具调用携带 trace、task、run、plan step、workspace、来源和版本。
- 审批绑定具体 `tool_call_id + args_digest`，重放不能重复执行。
- 支持 dry-run 和 capability 探测，让前端显示真实可用性。

### 4.4 多供应商生产适配

当前基线需要明确区分“CLI 能调用”和“Web 产品已支持”：Pico CLI 已包含 OpenAI-compatible、Anthropic-compatible、DeepSeek 和 Ollama 的选择入口，但 Web Worker 生产运行时仍固定实例化 `OpenAICompatibleModelClient`，使用 `/responses` 兼容协议。CLI 中存在 provider 选项不等于 Web/Electron 用户已经能切换供应商。

V1.2 建立 provider-neutral 的 `ModelProviderFactory` 和版本化 adapter 合同，首批生产支持：

```text
OpenAI Responses-compatible
Anthropic Messages-compatible
DeepSeek-compatible
Ollama local
```

每个 adapter 必须统一提供：

```text
provider_id, adapter_version, endpoint_protocol
credential_reference, endpoint, model
streaming, native_tool_calling, structured_output
reasoning_capabilities, sampling_capabilities
context_window, output_token_limit, usage_fields
health, last_probe, error_mapping
```

要求：

- API Key、endpoint 和自定义 header 只存 Worker 本地 `.env` 或系统密钥环；前端通过 Companion 配置，不上传中央服务、不写入 Git。
- Session、消息和图记忆使用 provider-neutral schema。切换 provider/model 后仍可直接读取原有本地会话，不要求先切回创建会话时的供应商。
- 会话可记录创建时和每个 Run 实际使用的 provider/model 快照，但该快照只用于追溯，不成为读取历史的门禁。
- provider 请求、流式事件、工具调用、错误和 usage 统一归一化；供应商特有字段保存在带命名空间的扩展区，不能污染核心合同。
- 前端只能展示 Worker 实际探测成功的供应商和模型；缺少依赖、凭据或兼容协议时显示明确不可用原因，不使用占位成功状态。
- 自定义 OpenAI-compatible endpoint 必须单独探测 `/responses`、工具调用和推理参数兼容性，不能仅因 URL 可连接就判定完全兼容。

### 4.5 推理能力协商与可展示摘要

Worker 为每个 provider/model 上报真实 capability，而不是把 OpenAI 的字段复制给所有供应商：

```json
{
  "provider": "...",
  "model": "...",
  "reasoning": {
    "supported": true,
    "efforts": ["capability-driven values"],
    "summary": "none | provider_approved",
    "supports_temperature": false
  },
  "max_output_tokens": 0,
  "usage_fields": []
}
```

- reasoning effort 的可选值以当前 endpoint/model 探测结果和 adapter 声明为准，不定义跨供应商通用的固定枚举。
- 前端沿用 V1.1 的紧凑选择器并扩展为 provider、model 和 reasoning effort 配置，区分默认值、当前 Run 快照和实际生效值。
- 用户显式选择的 effort 不得在节点之间静默改写；只有用户选择 `自动` 时，LangGraph 才能按 Planning/Execute/Review 节点和 capability 优化强度，具体结果写入 trace。
- V1.2 可选展示供应商明确提供的 reasoning summary。该摘要必须标记来源、经过敏感信息过滤，且不得作为完成证据或替代 plan/evidence。
- 原始隐藏 CoT 属于不可导出的内部推理：即使供应商返回相关内容，也不得进入 SSE、前端、历史、日志、审计、checkpoint 或图记忆。
- 当前 OpenAI-compatible client 尚未收集 reasoning summary 或 reasoning usage；实现时通过 adapter 增量接入，不能从普通输出文本猜测或伪造。

## 5. Docker Sandbox 与变更隔离

- 定义 `create/exec/cancel/destroy/inspect` Sandbox 生命周期。
- 使用非 root、只读 rootfs、`no-new-privileges`，禁止 privileged、host network、host PID/IPC 和 Docker Socket。
- 只挂载经过真实路径校验的 workspace 和临时目录。
- 默认关闭网络；网络按目标、任务和有效期授权。
- 限制 CPU、内存、PIDs、磁盘、命令时间、输出和子进程数。
- 取消、超时和异常退出必须销毁容器和进程树。
- Git workspace 的写入节点使用独立 worktree；非 Git workspace 使用目录 snapshot。
- Review 通过后由 Coordinator 生成可检查 diff，再显式合并；冲突不得静默覆盖。

无人值守模式只有在 Sandbox 和审批策略通过安全验收后才能开放高风险 `bash`。

## 6. 本地资源浏览与引用

前端提供只读资源树，使用户能在 ThreadForge 中查看已授权 workspace：

- 按需展开目录、虚拟化大目录、文件名/内容搜索。
- 文本、图片和常见二进制元数据预览。
- 显示大小、类型、修改时间、freshness 和 Worker 在线状态。
- 拖拽只生成资源引用，不执行移动、删除、重命名、上传或改写。
- 引用进入模型前显示路径、内容范围和大小；大文件只附加摘要或选定片段。
- 刷新只恢复索引，文件内容重新向拥有该 workspace 的 Worker 请求。

资源接口只提供 `list/stat/read/search/preview`，所有绝对路径、符号链接越界、凭据文件和过大内容必须过滤。

### 6.1 会话输入框的多格式附件

- 会话输入框除纯文本外，支持从已授权 workspace 选择或拖入常见代码、Markdown、PDF、Office 文档、图片、日志、JSON/YAML、压缩包及其他可识别文件格式。
- 附件默认保存为 Worker 本地资源引用，不把完整文件上传到中央服务器；提交任务时由对应 Worker 重新校验 owner、device、workspace、路径和 freshness。
- 前端在发送前显示文件名、类型、大小、来源 workspace、解析方式和预计进入上下文的范围；用户可以移除附件或仅选择文件中的部分内容。
- Worker 使用 MIME、文件签名和扩展名共同识别格式，不只信任扩展名；可解析格式提取结构化文本或元数据，不可解析格式降级为只读元数据和明确的“不支持预览”状态。
- 大文件、二进制文件和压缩包不得直接完整注入模型上下文。必须使用大小上限、分页、内容摘要、文件清单、压缩炸弹防护和用户确认；压缩包默认只读列举，不自动解压到 workspace。
- 附件不能绕过资源树的只读边界，不提供删除、覆盖、重命名、移动或静默执行能力；凭据、密钥、系统目录和越界符号链接继续按资源安全策略拒绝。
- 同一条消息支持多个附件，并为每个附件生成稳定的 `resource_id`、内容 hash、引用范围和解析状态；断线重连后可以恢复引用，但文件变化时必须提示 stale 并要求重新确认。
- Web 与 Electron 使用同一附件合同；Web 只能引用已配对 Worker 授权的资源，Electron 可以调用本地选择器，但仍通过 Worker 完成校验和读取。

6.1 验收标准：用户可以在输入框中附加多种常见格式并看到真实解析状态；支持的内容能以受控范围进入 Agent 上下文，不支持或过大的文件会明确降级；刷新、换设备、文件变更、越界路径和恶意压缩包均不会造成静默上传、执行或越权读取。

### 6.2 混合控制面与本地数据通道

V1.2 使用“控制面统一经过 API Server，本地数据面优先直连 Worker”的混合架构。传输边界按数据归属、持久性和 Worker 所在设备划分，不能仅依据某项数据是否出现在统一界面中决定。

控制面必须经过 API Server：

- GitHub 登录、owner 身份、设备配对、Worker 在线状态和 capability。
- `device_id/workspace_id/session_id/task_id/run_id`、展示名称和稳定路由关系。
- 会话与任务索引、计划状态、Run 进度、审批、取消、终态和脱敏工具摘要。
- SSE/WS sequence、租约、幂等键、断线恢复和需要跨前端保持一致的持久元数据。

同设备本地数据面可以由 Web/Electron 前端直接访问 Worker Companion：

- 原生目录选择器、已授权 workspace 的按需目录展开、文件名或内容搜索。
- 只读文件预览、拖拽资源引用、文件类型和本地 freshness 检查。
- 本地 provider/API Key 配置、`.env` 写入和不需要跨设备统一的 Companion 状态。
- 资源树仅在用户点击、展开、搜索或拖拽时请求；前端不得启动时扫描完整 workspace，也不得把完整目录树作为统一状态预加载。

中央服务只保存路由所需的最小 workspace 投影，例如：

```text
device_id
workspace_id
display_name
online
capabilities
```

默认不得上传或持久化本地绝对路径、完整目录树、文件正文、Shell 完整输出、API Key 或 Worker `.env`。任务和工具事件需要统一展示时，只发送经过 Worker 脱敏、限长且有来源标记的摘要；用户明确上传文件属于独立受控流程，不能作为本地资源浏览的隐式副作用。

前端必须使用统一的资源访问接口，由连接解析器选择实际通道：

```text
browseWorkspace(workspace_id)
  -> 同设备且本地通道健康：loopback / Electron IPC
  -> Worker 位于其他设备：API Server 临时受控中转
```

UI 组件不得依赖具体传输方式。同一 workspace 在 Web、Electron 和后续移动端应使用相同的资源引用合同；切换通道不能改变 owner、device、workspace、freshness 和只读权限语义。

远端 Worker 不能通过目标设备的 `127.0.0.1` 直连。电脑 A 或移动端访问电脑 B 的资源时，必须由 API Server 按需中转有界请求和响应，或者在后续 ADR 启用端到端加密 Relay。中转内容默认不落盘、不进入日志、不加入 SSE 历史；若服务器仍能看到明文，界面和威胁模型必须明确说明。不得要求用户开放 Worker LAN 端口、配置公网 IP 或向前端提供 SSH 凭据。

本地资源桥必须满足：

- 仅监听 loopback，禁止监听 `0.0.0.0`、局域网地址或公网接口。
- Electron 使用受限 IPC；Web 使用明确的生产 Origin allowlist、CORS 与 Private Network Access 预检，拒绝任意网页访问。
- 使用短期一次性会话令牌和 nonce，绑定 owner、device、浏览器会话、Origin、过期时间和 capability；长期设备令牌及 API Key 不得暴露给前端 JavaScript。
- 每个请求重新验证 workspace grant、规范化相对路径、符号链接边界、文件类型、大小和 freshness；前端传入的绝对路径不得直接作为授权依据。
- 本地资源接口只提供列举、搜索、读取、预览和生成资源引用，不提供删除、移动、重命名、覆盖或任意 Shell。
- 限制并发、目录深度、单页条目、响应字节、搜索时间和预览大小；大目录使用分页和虚拟化，取消必须中止 Worker 侧读取。
- 完成协议和 Worker 版本握手后才启用直连；本地桥不可用、版本不兼容或认证失败时显示明确原因，不得静默扩大权限。

附件提交时，前端只把不透明资源引用和用户选择范围写入任务。实际任务仍由 API Server 路由到绑定 Worker，并由 Worker 在执行前重新解析引用和验证 freshness；前端直连不能绕过中央任务、审批、预算、审计或 Run 生命周期。

6.2 验收标准：

- 同设备 Web 与 Electron 均能通过受限本地桥按需展开和预览已授权资源；抓取中央 API 存储与日志，确认不存在绝对路径、目录树、文件正文和 provider 凭据。
- 另一台电脑或后续移动端能够通过有界中转浏览远端 Worker 的授权资源；中转断开、超时、重复请求和 Worker 离线均产生明确状态且不遗留服务端正文。
- 相同资源请求经本地直连和远端中转得到一致的权限、分页、freshness、错误码和资源引用语义。
- 未授权 Origin、过期/重放令牌、跨 owner、跨 workspace、路径穿越、符号链接逃逸、超限响应和直连端口扫描均被拒绝并留下脱敏审计事件。
- 本地桥不可用时前端明确显示降级原因；未经用户确认不得把原本只在本地传输的正文静默切换为中央明文中转。
- 刷新和通道切换只恢复最小索引，不预取完整目录；前端不能通过本地直连绕过任务创建、审批、取消、预算和 Run 终态。

## 7. 受控互联网资源

新增两个只读工具：

- `web_search`：返回标题、URL、摘要、发布时间和来源。
- `web_fetch`：获取公开 HTTP(S) 页面或文档正文。

要求：

- 独立网络 allowlist、速率、缓存、大小、类型、重定向和超时限制。
- 拒绝 loopback、RFC1918、云元数据、文件协议和非 HTTP(S) 协议。
- 网页内容视为不可信输入，不能改变工具权限、计划或审批。
- 保留 URL、抓取时间、来源和脱敏响应元数据。
- 登录、付费、表单提交和外部写入不属于 V1.2。

## 8. MRAgent 风格长期图记忆

### 8.1 边界

现有 LayeredMemory 和 Markdown memory 继续作为兼容基础。图记忆是独立服务，不保存全部聊天原文，也不直接替换 V1.1 运行状态。

节点类型：

```text
Cue, Tag, Episode, Semantic, Topic, Content
```

节点和边至少携带 owner、workspace、session、run、source、provenance、时间、confidence、freshness、sensitivity 和 schema version。

### 8.2 存储

- 首版使用本地 SQLite 邻接表 + FTS5。
- 按 `owner_id/workspace_id` 隔离 `graph.db`。
- 事务写入，并提供 Markdown memory 导入、导出和回滚。
- 文件事实保存相对路径与内容 hash；文件变化后标记 stale，未经重验不能注入。

### 8.3 主动重构

新任务规划前：

```text
Cue -> Tag -> Episode/Semantic/Topic
    -> provenance/freshness/confidence 剪枝
    -> 有预算的上下文片段
```

证据不足时返回未知，不凭空补全。每次重构记录访问节点、过滤原因和预算。

任务结束后异步蒸馏 Episode 和 Semantic candidate；敏感、低置信度、无来源和短期状态不得直接晋升 durable fact。蒸馏失败不覆盖原任务结果。

### 8.4 用户控制

- 用户可以查看、搜索、导出、删除、提升或拒绝记忆候选。
- 删除后检索不能再次返回该节点。
- 记忆错误不能泄露其他 workspace 或绝对路径。

## 9. 多 Worker 核心体验

- 一个用户可在前端查看多台 Worker 的平台、版本、能力、在线状态和授权 workspace。
- 创建任务时选择 `device_id + workspace_id`；会话绑定后不得隐式切换。
- 支持用户显式把后续任务切换到另一台设备，但必须提示文件/Git 状态可能不同。
- Scheduler 使用 capability、owner、workspace、lease 和 heartbeat 路由任务。
- 租约过期、重复投递、断线和重连必须幂等收敛。
- V1.2 不自动把一个任务拆到多 Worker，也不实现关联工作区。

## 10. 有界并行编排

只读 Research、文件扫描、网页检索、静态分析和 Review 分面在依赖图互不依赖时可以 fan-out/fan-in：

- 所有并行节点读取同一个不可变 snapshot。
- fan-in 按稳定 node/sequence 排序，不依赖完成先后。
- 使用 idempotency key 去重。
- 限制最大 fan-out、并发、token、工具、CPU、内存、磁盘和网络预算。
- 节点失败必须按计划显式选择取消同阶段、保留部分结果或整体失败。
- 写入节点不得共享可写目录。

并行只有在质量、耗时或可靠性评测显示收益时才默认开启。

## 11. Worker 与交付加固

- Worker 下载清单继续使用 Ed25519、SHA-256 和版本兼容检查。
- Windows EXE/安装器加入 Authenticode；其他平台使用对应签名机制。
- 更新流程实现跨进程互斥、独立临时文件、总体超时、下载进度、校验、回滚和重连状态机。
- Worker 后台进程保持低资源占用，不弹控制台；目录选择器前台显示并支持超时恢复。
- 发布制品和公网入口配置不进入公共 GitHub 仓库。

## 12. 隐私和应用层加密决策

V1.2 必须完成威胁模型，区分 TLS 已覆盖的传输风险与中央服务可见的应用数据。若启用敏感事件载荷端到端加密，必须同时定义：

- 每设备密钥、配对、轮换、撤销和恢复。
- Web/Electron 多端解密授权。
- 加密后服务器索引、搜索、审批和移动端验收的能力损失。
- 元数据仍可见的明确边界。

同设备本地数据通道中的绝对路径、目录树、文件正文和 provider 凭据应保持在前端与 Worker 之间，不经过中央服务。远端 Worker 的临时中转必须单独列出服务器可见字段、日志策略、内存保留时间和响应上限；未来启用端到端加密时保持相同资源引用与路由合同，避免重新设计前端。

没有完成密钥恢复和设备撤销前，不得自行部署不可恢复的加密协议。V1.2 可以先保持 TLS/WSS 和本地数据最小化，但必须形成可执行 ADR 和后续启用条件。

## 13. 实施顺序

1. Application service、统一事件和 SQLite 控制面。
2. Tool Registry、原生 tool calling 和兼容 adapter。
3. 多供应商 `ModelProviderFactory`、capability 探测、推理配置和 provider-neutral 会话合同。
4. Sandbox、worktree/snapshot 和取消清理。
5. 统一资源访问接口、同设备本地资源桥、远端受控中转、本地资源树与受控互联网只读能力。
6. durable checkpoint、Scheduler 和多 Worker 切换。
7. 图记忆存储、重构、蒸馏和用户控制。
8. 只读 fan-out/fan-in 和性能评测。
9. Worker 签名、更新状态机和生产运维验收。

## 14. CI 与验收

- `contract`：REST、SSE、Tool Registry、checkpoint 和 generated client。
- `providers`：OpenAI/Anthropic/DeepSeek/Ollama adapter、流式归一化、工具调用、错误映射、历史跨 provider 读取和 capability 探测。
- `reasoning`：合法/非法 effort、参数兼容、降级记录、供应商摘要脱敏和原始 CoT 不落盘/不出站。
- `migration`：JSON 导入、SQLite schema、损坏数据、重复导入、备份和回滚。
- `sandbox`：创建、执行、取消、销毁、mount/network/resource 和异常清理。
- `resources`：本地越界、拖拽只读、按需目录分页、直连/中转等价性、互联网 SSRF、恶意网页和过大响应。
- `memory`：Cue 重构、freshness、provenance、敏感过滤、删除和并发写入。
- `scheduler`：lease、heartbeat、重复投递、断线重连和 capability 路由。
- `orchestration`：并行时序、稳定 fan-in、预算、取消和 Review。
- `release`：安装、签名、升级、回滚、数据保留和服务重连。
- `security`：跨 owner/workspace、审批、凭据、Sandbox 逃逸、loopback Origin/PNA/短期令牌、远端中转不落盘和加密 ADR。

## 15. V1.2 明确不做

- MCP Server、MCP tools 和 MCP 凭据管理。
- Skills 安装、Registry、自进化和 Marketplace。
- Agent Swarm、跨 Worker 自动拆分和关联工作区。
- 登录网页自动化、付费操作和外部写入。
- 移动端正式客户端。
- 把 SQLite 图记忆迁移到 Neo4j 或云向量数据库。

## 16. 完成定义

V1.2 只有同时满足以下条件才完成：

1. V1.1 主链路和安全边界无回归。
2. Tool Registry、原生 tool calling、Sandbox 和变更隔离进入生产 Worker。
3. 用户能安全浏览和引用本地资源，并使用受控互联网只读资源；同设备访问优先经过受限本地桥，远端访问经过不落盘的有界中转，前端使用同一资源合同且中央服务不持久化绝对路径、目录树和文件正文。
4. checkpoint、可靠 SSE 和多 Worker 切换经过断线/重启验收。
5. 图记忆可重构、蒸馏、追溯、删除并正确处理过期事实。
6. 并行节点可稳定汇聚，写入不会冲突覆盖。
7. Web/Electron 可通过 Companion 使用至少四类目标 provider adapter，配置与凭据保持本地，切换后历史仍可直接读取。
8. 推理强度按 provider/model capability 生效；可选摘要经过脱敏，原始 CoT 从不出站或落盘。
9. Worker 更新、签名、回滚和资源占用满足生产门槛。
10. 全部 V1.2 CI 及真实 Windows/Linux Worker 验收通过。

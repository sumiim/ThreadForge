# ThreadForge V1.1 最小可用 Agent 需求

> 文档状态：需求基线（Draft）
> 基线：`main@6d40a55`，Worker `0.2.17`
> 目标：让 Web/Electron + 本地 Worker 成为可以稳定阅读、修改和解释代码的单 Worker Coding Agent。

## 1. 版本定位

V1.1 只解决核心 Agent 是否真正可用，不建设扩展生态，也不把性能和平台演进项目混入首个可用里程碑。

用户应当能够选择已授权工作区、创建会话、看到 Agent 的过程说明和工具调用，并在有真实证据且 Review 通过后得到最终回答。模型输出一句“我接下来会检查”不能结束任务，界面也不能把没有发生的步骤显示为已完成。

V1.1 复用当前已有能力：

- GitHub OAuth、多用户对象归属和 `owner_id` 隔离。
- 多设备绑定以及 `device_id + workspace_id` 固定路由。
- Worker 本地会话、模型 `.env`、运行产物和六个 builtin 工具。
- 工具参数校验、工作区边界、审批、超时、取消和审计。
- 现有中央 Task、Approval、SSE 和 Worker WSS 链路。

## 2. 当前问题

当前 Web Worker 直接实例化 Pico 并执行原生循环；仓库中的 LangGraph 主要供 CLI 和评测选择。界面上的 Understand、Gather、Analyze、Act、Verify 是 `TaskState` 标签，不是独立图节点。

当前本地配置为 `PICO_OPENAI_MODEL=gpt-5.5`。该模型具备推理能力，但现有 OpenAI-compatible 请求只发送 model、input、输出上限、stream 和 temperature，没有显式 reasoning 配置，也没有收集 reasoning summary/usage。因此当前状态不是可以确定的“no reasoning”，而是由 endpoint/model 默认策略决定实际推理强度。

原生解析器还存在以下结构性问题：

- 非空普通文本可以直接成为 final。
- “只宣布未来动作”的文本依靠不完整正则拦截，无法覆盖自然语言。
- `VERIFY` 只是状态切换，没有独立完成条件检查。
- `finish_success()` 可以无条件把 checklist 全部标绿。
- read-only 请求可能在当前 Run 零读取、零搜索、零工具时结束。

## 3. 核心不变量

1. 普通文本、过程说明和阶段标签都不能隐式结束任务。
2. 只有通过确定性完成门禁和 Review 的 final 才能调用 `finish_success()`。
3. 需要外部证据的请求必须在当前 Run 产生对应 evidence。
4. 用户要求“重新读取/重新检查”时，旧历史、旧摘要和长期记忆不能替代本轮读取。
5. 所有工具继续受 owner、device、workspace、审批、预算和取消约束。
6. 会话正文、模型配置和运行证据继续以 Worker 本地为主；中央服务只保存当前产品必需的控制状态。
7. 前端状态必须由真实事件推导，不得用终止函数补齐未执行步骤。
8. 原始隐藏 CoT 不得发送到前端，不得写入会话历史、日志、审计、运行产物或长期记忆。

## 4. 生产编排流程

真实 Web 任务必须进入以下 LangGraph 主流程：

```text
START
  -> prepare_reasoning_plan
  -> validate_plan_and_intent
  -> execute
       `-> Agent Turn loop: talk | tool | final | retry
  -> review
       |-- pass -> finalize
       |-- needs_fix -> execute（有界循环）
       `-- blocked/failed -> finalize
```

### 4.1 Planning

Planning 只生成可审计、可验证的执行提案，不等于模型的隐藏 CoT。Planner 必须输出严格 JSON，最小合同为：

```json
{
  "schema_version": "1",
  "plan_id": "plan_...",
  "revision": 1,
  "intent": "conversation | read_only | code_change",
  "summary": "给用户和 Review 使用的简短计划摘要",
  "steps": [
    {
      "id": "step_1",
      "goal": "本步骤要达成的结果",
      "dependencies": [],
      "required_tools": ["read_file"],
      "required_evidence": ["fresh_file_read"],
      "done_when": ["验收条件可由证据判定"]
    }
  ],
  "acceptance": ["任务级验收条件"],
  "risk_level": "low | medium | high",
  "budgets": {
    "model_rounds": 0,
    "tool_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "elapsed_seconds": 0
  }
}
```

计划执行前必须完成以下校验：

- schema 与版本合法，所有 step id 唯一，依赖存在且构成无环图。
- intent、验收条件、`done_when` 与 required evidence 不冲突，计划不是只有描述而没有可判定终点。
- required tools 已由当前 Worker 上报且符合 workspace 权限、审批策略和风险级别。
- 计划预算不超过系统、用户和 Worker capability 的共同上限。
- 前端只接收 `summary`、步骤目标、依赖、验收条件和状态，不接收 Planner 的隐藏推理文本。

Planning 与初步意图识别合并为一次模型调用：Planner 产出 `intent` 提案，`validate_plan_and_intent` 根据计划工具、证据和风险执行确定性校验。V1.1 不为 Intent 固定增加第二次模型调用；只有结构无法修复且确实存在歧义时才进入 retry 或要求用户补充信息。

允许在工具失败、证据 freshness 失效、前置条件变化或 Review 返回 `needs_fix` 时重规划。每次重规划生成不可变 revision，保留触发原因和旧计划引用，并重新执行权限、依赖与预算校验；重规划次数必须有上限，不能借重规划绕过审批或无限循环。

### 4.2 Intent

首版只支持三种意图：

| 意图 | 含义 | 工具要求 |
| --- | --- | --- |
| `conversation` | 不依赖外部事实的普通对话 | 可以零工具结束 |
| `read_only` | 读取、搜索、分析或解释工作区 | 按请求生成只读 evidence |
| `code_change` | 修改文件、执行受控命令并验证 | 必须产生变更和验证 evidence |

Intent 使用严格 schema。未知、冲突或多次格式错误必须失败或 retry，不能猜测执行。

Intent 的作用不是增加一个给用户观看的“理解阶段”，而是选择安全边界和完成门禁：

- 计划不需要 workspace 或外部事实时为 `conversation`，允许零工具回答。
- 计划包含 list/read/search 或引用代码事实时至少为 `read_only`，必须产生当前 Run 只读证据。
- 计划包含 write/patch/shell 写入或用户明确要求修改时为 `code_change`，必须经过写入审批、变更证据和验证。
- 确定性规则发现 Planner 提案过低时只能提高风险级别，例如包含写入工具的计划不能声明为 `conversation`。
- Intent 默认属于内部路由元数据，不在前端时间线中占用独立步骤；前端可在任务详情中查看最终分类及其原因摘要。

### 4.3 CoT 与推理配置边界

- Planning 是对外可审计的任务结构，`talk` 是面向用户的过程说明，二者都不是原始 CoT。
- V1.1 前端只展示 `plan_summary`、`talk`、工具证据、Review 结果和 final，不提供“完整思维链”视图。
- 输入区附近提供紧凑的“模型 + 推理强度”选择器，显示效果类似 `GPT-5.5  极高`；点击后可选择当前 Worker 实际支持的模型和推理档位。
- 选择结果作为 Run 级配置快照保存：`model_id/model_display_name/requested_reasoning_effort`。任务开始后修改选择器只影响下一次 Run，不在执行中途静默更换模型或强度。
- 用户显式选择的推理强度默认应用于当前 Run 的 Planning、Execute 和 Review 等模型调用，不向用户暴露多套节点级档位；未来如增加 `自动` 模式，才允许运行时根据节点类型优化强度。
- 当前生产 Worker 的 OpenAI Responses-compatible adapter 必须支持显式推理强度；选择器只列出 provider/model capability 声明支持的档位，不能假设所有兼容端点都接受相同枚举。
- Worker 或 endpoint 不支持原选择时，任务开始前显示明确提示并要求重新选择，不能静默降级成另一个档位。
- 开启推理模式时，仅在 capability 明确允许的情况下发送 `temperature` 等采样参数；不兼容时省略，不得因固定 payload 造成请求失败。
- Planning、Execute、Review 分别配置输入/输出 token 预算，不继续用单一且过小的固定上限约束所有节点；尤其不得默认沿用可能截断计划或 Review 的 `512` 输出 token 上限。
- trace/report 记录 provider、model、requested/effective reasoning effort、降级原因和供应商实际返回的 usage 元数据；不得记录原始 CoT。

## 5. Agent Turn 协议

每轮模型响应必须明确成为以下一种结果：

```text
模型选择：talk | tool | final
运行时结果：retry
```

推荐合同：

```json
{
  "type": "talk | tool | final",
  "content": "talk/final 的文本",
  "tool_call": {
    "name": "read_file",
    "arguments": {}
  }
}
```

### 5.1 `talk`

- 用于说明当前判断、正在做什么或下一步动作。
- 产生 `assistant.commentary` 事件，可在前端实时显示。
- 不写入 `final_answer`，不计为 evidence，不改变任务终态。
- 当前 talk 完成后必须继续让模型选择 `talk/tool/final`。
- 默认连续 talk 上限为 2；超限后必须 tool 或提交可通过门禁的 final。

### 5.2 `tool`

- V1.1 继续使用 `list_files/read_file/search/run_shell/write_file/patch_file`。
- 每次只执行一个经过 schema、权限、审批、预算和 workspace 校验的调用。
- 工具终态写入 Evidence Ledger，然后进入下一轮模型决策。
- 写入、Shell 和其他高风险操作继续按现有策略审批。

### 5.3 `final`

- final 只是最终回答候选，不代表任务已经完成。
- 候选先经过确定性完成门禁，再进入 Review。
- 只有 `review=pass` 才能写入最终回答并结束 Task。
- 无类型普通文本不得自动降级成 final。

### 5.4 `retry`

retry 由运行时产生，适用于：

- 非法或未知 Turn 类型。
- 结构化输出、工具名称或参数错误。
- final 缺少当前 Run evidence。
- Review 要求修复。
- 模型只描述未来动作却申请 final。

retry 必须包含稳定错误码和可操作修正提示，并受总轮数限制。

## 6. Evidence Ledger 与完成门禁

每条 evidence 至少保存：

```text
evidence_id, run_id, plan_id, step_id, intent
tool_call_id, tool_name, status
workspace_id, relative_paths, freshness
created_at, summary, sensitivity
```

完成门禁：

| 意图 | 必需条件 |
| --- | --- |
| `conversation` | final 非空；若引用 workspace 或外部事实，则必须存在对应 evidence |
| `read_only` | 请求要求读取/搜索/检查时，当前 Run 至少有一次成功只读 evidence；“重新读取”必须是本轮新 evidence |
| `code_change` | 至少有一次成功写入或 patch、非空 affected paths、变更后验证结果及所有必要审批终态 |

以下内容不属于 evidence：talk、模型自述、旧 final、阶段标签、未经 freshness 校验的历史摘要。

## 7. Review 与终态

Review 由确定性规则和受限模型判断两部分组成：

1. 规则先检查 required evidence、审批、受影响路径、工具错误、预算和 freshness。
2. 规则通过后，模型 Review 才检查回答是否覆盖用户请求、变更是否符合验收条件。
3. `needs_fix` 只能进入有上限的 execute 循环。
4. 超过修复次数后必须以 `blocked/failed` 结束，不能假成功。

终态至少包括：

```text
completed
failed
cancelled
interrupted
blocked
```

V1.1 不要求服务重启后从任意 Python 调用栈继续；不能安全恢复时标记 `interrupted`，让用户明确重试。

## 8. TaskState、事件与前端

前端必须区分：

```text
plan.*
assistant.commentary
model.started / model.completed
tool.started / tool.completed / tool.failed
review.started / review.completed
assistant.final
task.failed / task.cancelled / task.interrupted
```

要求：

- checklist 项由对应节点或 evidence 完成，不能在 `finish_success()` 时全部补齐。
- talk 显示在运行过程区域，不混入用户气泡，也不冒充 final。
- 工具参数与安全结果预览沿用脱敏和长度限制。
- 刷新或 SSE 重连后按序号恢复当前状态；断开页面不能自动取消任务。
- 终态只能出现一个 final；重复事件必须幂等。

### 8.1 Run 侧边快速索引

每一次 Run 都必须生成独立的侧边快速索引，形态为贴在当前时间线边缘的窄型“缩略导航条/Minimap”，类似长文档或代码编辑器的缩略滚动条，而不是再占用一整列宽侧边栏：

- 索引以 `run_id` 为边界，显示 Run 序号、开始时间、当前状态和终态；重试、恢复或重新执行产生的新 Run 不得覆盖旧 Run。
- 导航条使用短横线、区段和状态标记压缩表示计划步骤、重要 talk、工具调用、审批等待、Review、final 和失败/取消/中断节点。
- 当前视口、正在执行节点和异常节点使用不同粗细、形状或状态图标区分，不能只依赖颜色表达状态。
- 点击短横线必须定位到主时间线中对应事件；点击空白区按比例滚动，拖动视口指示器可快速浏览长 Run。
- 悬停或键盘聚焦索引项时显示安全摘要，例如步骤名称、工具名称、状态和耗时，不直接展开完整内容。
- 滚动主时间线时，导航条同步高亮当前区域；切换 Run 时切换到对应 Minimap，不混合其他 Run 的事件位置。
- 索引状态、成功/失败标记和耗时只能由真实事件及 Evidence Ledger 推导，不能根据模型文字或前端预估提前标记完成。
- 相同步骤下的连续工具事件可以折叠，但必须保留调用数量、失败状态，并允许展开查看脱敏后的参数和结果摘要。
- 导航条保持窄宽度；长 Run 使用区段聚合、抽样渲染和虚拟化，不能随事件增加不断变宽、造成明显卡顿或挤压主工作区。
- 每个索引项使用稳定的 `event_id/node_id/step_id` 锚点；刷新、SSE 重连和历史回放后仍能恢复顺序、展开状态和当前定位。
- 索引只展示安全摘要，不得泄露绝对路径、凭据、隐藏 CoT 或未经脱敏的工具输出。

### 8.2 三级导航节点改名

左侧导航继续使用“Worker/设备 -> 工作区 -> 会话”三级结构，并允许用户修改每一级的显示名称。三个实体统一增加可变展示字段：

```text
display_name
display_name_source: auto | user
display_name_updated_at
```

要求：

- Worker/设备、工作区和会话都支持通过双击或右键菜单进入改名；Enter 保存，Esc 取消，保存失败时恢复原名称并显示原因。
- `display_name` 只用于展示，稳定的 `device_id/workspace_id/session_id` 始终用于路由、归属、权限和数据关联。
- 改名不得修改本地目录名称或绝对路径、Git 分支、Worker 配对关系、会话历史、Run、任务路由和任何底层 ID。
- 自动名称可来自设备类型、目录 basename 或首条用户消息；用户手动修改后标记为 `source=user`，后台不得再自动覆盖。
- 名称必须去除首尾空白、控制字符和可执行 HTML，限制合理长度；空名称恢复为自动名称。
- 同级允许出现相同显示名，但界面必须使用安全的辅助信息消歧：设备显示平台/短 ID，工作区显示脱敏路径或设备名，会话显示时间。
- 改名需要按 owner 校验，并通过版本号或更新时间避免多端同时修改时静默覆盖；Web/Electron 刷新或重连后显示一致结果。
- 若现有 Session 已使用 `title` 字段，迁移时应统一映射到 `display_name`，不得长期维护两个互相冲突的名称来源。

## 9. 预算、取消与失败收敛

每个 Task 必须限制：

- 连续 talk 次数。
- 模型总轮数、token 和总耗时。
- 工具调用数、read_file 次数和 Shell 超时。
- Review 修复次数。

取消必须传播到模型请求、图节点、工具和子进程树。格式错误、talk 超限、预算耗尽、审批拒绝、Worker 断线和服务重启都必须产生明确终态，不得无限 retry 或遗留进程。

## 10. Worker 生产接入

- 本地 Worker 的真实任务入口必须调用 LangGraph backend，不能只修改 CLI。
- 当前 Web 生产路径仍固定使用 OpenAI Responses-compatible client，且尚未显式发送 reasoning 配置；V1.1 只补齐这条生产路径的能力协商和推理强度控制。
- Worker 安装包必须包含锁定版本的 LangGraph/编排依赖。
- Worker capability 上报 `orchestration_backend`、schema 和协议版本。
- Worker capability 同时上报 provider/model、支持的 reasoning effort、采样参数兼容性、最大输出预算和可用 usage 字段。
- 新旧 Worker 不兼容时拒绝下发任务并显示升级入口，不静默回退到有缺陷的 native 路径。
- Session、Run、TaskState 和现有六工具合同保持兼容，必要时提供单向迁移适配器。

## 11. 实施顺序

1. 冻结 Agent Turn、Intent、Plan、Evidence、Review、模型 capability 和事件 schema。
2. 建立计划 DAG 校验、有界重规划、确定性完成门禁及 FakeModelClient 回归。
3. 完成串行 LangGraph 主流程和现有六工具适配。
4. 为当前 OpenAI Responses-compatible 生产 adapter 接入 Run 级模型/推理强度选择、参数兼容和独立 token 预算。
5. 接入本地 Worker 真实任务与打包依赖。
6. 接入 SSE 和前端 plan/talk/review/final 展示。
7. 完成取消、审批、预算、断线和失败收敛。
8. 通过 CI 后使用实际安装包进行 Web 端验收。

## 12. CI 与验收

实现阶段只通过 GitHub Actions 验证，不以本地测试作为合并依据。

- `unit`：Turn 解析、talk 限额、Evidence Ledger、三类完成门禁和 TaskState。
- `planning`：严格 JSON、DAG、`done_when`、权限/预算校验、计划内意图分类和有界重规划。
- `orchestration`：三种意图、`talk -> tool -> talk -> final`、retry、Review 修复和预算终态。
- `model-capability`：模型/推理强度选择器、Run 配置快照、无效档位、采样参数冲突、执行中切换保护、输出截断和 usage 记录。
- `regression`：六工具、审批、路径边界、LayeredMemory、Session/Run 和 Worker 协议。
- `integration`：FakeModelClient + 实际 Worker 生产入口，覆盖 read-only/code-change、取消和断线。
- `frontend`：talk、tool、review、final 分层展示、每 Run Minimap、锚点跳转、长列表性能、三级节点改名及刷新恢复。
- `security`：owner/device/workspace 隔离、审批重放、路径逃逸和凭据脱敏。

必须包含以下回归：模型输出“我再看一遍关键路径”但当前 Run 为零读取时，任务不得完成；模型可以先产生 talk，随后读取文件并在 Review 通过后返回 final。

## 13. V1.1 明确不做

- MCP、Skills 和任何第三方扩展安装。
- MRAgent 图记忆和 SQLite/FTS5 长期记忆。
- Pi 四工具 Registry 和完整多供应商生产适配；V1.1 仅补当前 OpenAI Responses-compatible 路径的推理配置。
- 同阶段 fan-out/fan-in、Swarm 和跨 Worker 子任务拆分。
- Docker Sandbox、自动 worktree/snapshot 合并。
- 完整本地资源树、拖拽引用和互联网搜索/抓取。
- durable graph checkpoint replay、数据库控制面迁移和 PostgreSQL。
- 关联工作区、移动端验收和应用层端到端加密。

## 14. 完成定义

V1.1 只有同时满足以下条件才完成：

1. Web 创建的真实任务由打包 Worker 进入 LangGraph 主流程。
2. Agent Turn、三类意图、Evidence Ledger、完成门禁和 Review 均已生效。
3. 零本轮证据的 read-only 请求和无变更证据的 code-change 请求不能成功。
4. 前端准确展示过程、工具、Review、失败和 final；每个 Run 具有窄型、可跳转、可恢复且状态真实的 Minimap，三级导航节点均可安全改名。
5. 现有多用户、workspace、审批、取消、本地历史和 Worker 更新能力不回退。
6. Planning 可验证、可重规划但有界；用户选择的 Run 级模型和推理强度按 capability 生效，且不会泄露原始 CoT。
7. 所有 V1.1 CI 和一次实际打包 Worker 端到端验收通过。
8. V1.2/V1.3 能力明确显示未启用，不用占位数据伪装完成。

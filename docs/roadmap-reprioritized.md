# ThreadForge 任务重排与 V1.2+ 路线重划

> 文档状态：重排提案（Draft）
> 触发原因：并发能力应前移 + 多 Worker 路线已在多份文档中零散规划但未排期 + 新增需求（审计可视化 / 运行中追加输入 / pi 作为 subagent 与 Judge / 思考回传 / API 供应商管理窗口 / 主循环收敛 / Review 反馈闭环）+ 前端主基调确立
> 前置：`requirements-v1.1-agent-mvp.md`、`requirements-v1.2-agent-hardening-and-productivity.md`、`requirements-v1.3-extension-ecosystem.md`、`multi-user-v15-roadmap.md`

本文把「未实现任务」按当前价值与依赖重新排序、把 V1.2 之后的能力重新划进版本，并记录新增需求（§7）与 native 路径决策（§3，已选定方案 A）及前端主基调（§8）。原三份需求文档继续作为既有能力的验收细节来源，新增需求以本文 §7 为准。

> **当前完成度快照（2026-08，对照 `main`）**：V1.1（P0）✅ 基本完成；V1.2 🟡 SQLite 控制面 / intent 分级预算 / Docker Sandbox 后端已落地，剩并发 / 写隔离 / inbox；V1.3–V1.5 ❌ 未开始。

---

## 1. 重排原则

1. **并发先于功能**：先去掉「全局单活动任务」这一硬天花板，再谈其他。
2. **安全生死线优先**：Docker Sandbox 是唯一决定产品能否用于真实仓库的能力。
3. **存储硬伤优先**：JSON 文件 + reconcile 打补丁已到顶，SQLite 是规模化前提。
4. **生态能力后置**：MCP/Skills/Marketplace/Swarm 价值依赖规模，且依赖底座。
5. **不追 DSH 最强项**：不自建 Marketplace 与插件生态去对标 DeepSeek Harness，只做能借力且可控制的「扩展管理」部分。
6. **前端主基调借鉴 DSH**：前端交互与信息架构以 DeepSeek Harness 为第一参照（见 §8），借鉴会话事件驱动、可折叠思考、轨迹回放等范式，但不复制其插件化内核。

---

## 2. 未实现任务重排（按优先级）

> 状态图例：✅ 已完成 · 🟡 部分完成 · ❌ 未实现。以下「现状」已于 2026-08 对照 `main` 代码（commit `9e29103`）核实回写。

### P0 —— V1.1 收尾（当前最该完成）

| # | 任务 | 现状 | 理由 |
|---|---|---|---|
| 0.1 | 清理 `langgraph_pico` 导入冲突 | ✅ 已修复（卸载残留的 `pico-langgraph-example`） | 阻塞所有 LangGraph 测试与 Worker 生产入口 |
| 0.2 | 前端 talk/plan/review/final 分层展示 + 左侧可折叠运行轨迹条 | ✅ 已完成（`RunTimeline`/`RunTracePanel`/`traceModel` 泳道分层 + 左侧可折叠轨迹） | 替换现有对话索引/Run Minimap；无可定位的运行轨迹则编排不可见 |
| 0.3 | 三级导航改名（device/workspace/session `display_name`） | ✅ 已完成（`display_name` 字段 + rename API + UI） | v1.1 完成定义第 4 条 |
| 0.4 | 推理强度选择器 + Worker capability 上报 | ✅ 已完成（`Composer` 推理强度选择器 + `reasoning_efforts` 上报） | v1.1 完成定义第 6 条 |
| 0.5 | native 路径结构性问题决策 | ✅ 已选定 A（见 §3） | `backend_process` 迁移到 LangGraph，native 降级为 CLI/评测兼容 |
| 0.6 | 审计与轨迹可视化 | ✅ 已完成（`RunArtifactsDrawer`：诊断摘要 / 因果链 / 审计表） | 见 §7.1；审计数据已存在，缺可视化层 |
| 0.7 | 后端统一事件契约（EventSink 补 `started_at`/`ended_at`/`parent_event_id`） | ✅ 已完成（事件信封 + SQLite 事件表 + 脱敏） | 见 §7.1.3；因果图与真实耗时区间的数据地基 |
| 0.8 | 主循环编排收敛决策（单一可复用循环） | 🟡 方向已定（§7.7 目标架构），未落地 | 见 §7.7；pi（earendil-works）+ DSH 双参照，决定 1.5/2.1/2.2/3.6 的实现形态 |
| 0.9 | Review 反馈闭环 + 预算收敛 + 工具容错（基于 08-14 trace） | 未实现 | 见 §7.8 + `issue-log.md`；修复 replan 不收敛、`budget_exhausted` 裸 blocked、工具类型小错误 |

### P1 —— 并发与交互基座（新 V1.2 核心）

| # | 任务 | 现状 | 理由 |
|---|---|---|---|
| 1.1 | **多任务并发（去全局单飞）** | 未实现，`TaskRunner` 仍是 `ActiveTaskExistsError` | 用户确认的最高优先级；解锁「边跑长任务边问问题」 |
| 1.2 | 同一工作区并发写安全（worktree/snapshot） | 未实现 | 并发放开的前提，否则写任务互相覆盖 |
| 1.3 | **Docker Sandbox** | 🟡 部分（`sandbox.py` fail-closed Docker backend + 测试已实现；生产接线待验证） | 安全生死线，`run_shell` 当前审批即全权执行 |
| 1.4 | SQLite 控制面迁移 | ✅ 已完成（`sqlite_store`/`sqlite_repositories` + 幂等导入 + JSON 镜像） | 存储硬伤，为并发/租约/事件重放打底 |
| 1.5 | 运行中追加新请求（inbox） | 未实现 | 见 §7.2；需要 agent loop 队列化输入，与并发同属运行时交互改造 |
| 1.6 | 预算自动调整（方法 A：intent 分级 step_budget） | ✅ 已完成（`INTENT_STEP_BUDGETS` + `_intent_step_budget` + 软预算扩展） | 见 §7.5；修复「继续→预算耗尽」，意图分级硬上限 + 软硬分离 |
| 1.7 | Tool Registry 抽取（纯重构，行为不变） | 未实现 | 2.1/2.2/3.3/3.4 的公共前置；先解耦 `BASE_TOOL_SPECS` 为版本化注册表，零行为变更 |

### P2 —— 工具协议与供应商（新 V1.3）

| # | 任务 | 现状 | 理由 |
|---|---|---|---|
| 2.1 | Tool Registry + Pi 四工具 + 原生 tool calling | 🟡 部分（原生 tool calling 已半接 `clients.py`；无版本化 Registry / 四工具） | 「加工具不改源码」的可扩展 seam，是性价比最高的架构债 |
| 2.2 | 多供应商 `ModelProviderFactory` + 推理协商 | 未实现（Worker 固定 OpenAI-compatible） | 生产路径目前被供应商锁死 |
| 2.3 | durable checkpoint + 可靠 SSE（`Last-Event-ID`/重放/背压） | 🟡 部分（SQLite 事件表 + 幂等重放 + heartbeat；`Last-Event-ID` 续传未确认） | 断线恢复、多 Worker 切换的前置 |
| 2.4 | 有界并行 fan-out/fan-in | 未实现 | 文档自设门槛「评测有收益才开」，排 P2 末位 |
| 2.5 | 思考回传（reasoning 流式展示） | 未实现（client 未捕获 `reasoning_content`） | 见 §7.4；供应商明确提供的思考内容流式回传前端，借鉴 DSH 可折叠思考区 |
| 2.6 | 预算自动调整（方法 C：capability 派生 prompt 预算 + 规划器超时） | 未实现 | 见 §7.5；修复「规划阶段超时」+ prompt 预算硬编码 |
| 2.7 | API/模型供应商管理窗口（前端） | 部分（`WorkerDevices.tsx` 仅单 provider 表单） | 见 §7.6；配置面（CRUD+本地密钥+连接测试）可提前 V1.2，切换生效随 2.2 |

### P3 —— 资源与扩展基础（新 V1.4）

| # | 任务 | 现状 | 理由 |
|---|---|---|---|
| 3.1 | 本地资源树 + 附件 + 拖拽引用 | 未实现 | 生产力刚需，但依赖 Worker 能力与并发 |
| 3.2 | `web_search` / `web_fetch` | 未实现 | 只读、独立实现、价值明确 |
| 3.3 | 只读 MCP Client | 未实现（前端仅占位） | MCP 是开放协议，可借力，不必自造 |
| 3.4 | Skills Registry 基础生命周期 | 未实现（空壳） | 受控扩展管理有长期价值 |
| 3.5 | 图记忆（SQLite+FTS5+重构+蒸馏） | 未实现 | 独立子项目，价值中，难度高，排 P3 末位 |
| 3.6 | pi 作为 subagent + A2A + LLM-as-a-Judge | 🟡 部分（`delegate`/`evaluation` 已有；A2A 未实现） | 见 §7.3；复用现有 delegate 与 evaluation，借 A2A 开放协议 |

### P4 —— 生态与协作（远期 / 冻结候选）

| # | 任务 | 现状 | 理由 |
|---|---|---|---|
| 4.1 | Marketplace 公共目录 | 未实现 | DSH 的 npm 插件生态已占心智，不追 |
| 4.2 | Agent Swarm 跨 Worker | 未实现 | 本地优先工具边际价值低，且最难 |
| 4.3 | 关联工作区 | 未实现 | 依赖多 Worker + 图记忆 |
| 4.4 | 浏览器自动化 | 未实现 | 重工程、高风险、低优先级 |
| 4.5 | 移动端 | 未实现 | 体验延伸，非核心 |

### V2 —— 多租户（`multi-user-v15-roadmap.md` 不变）

PostgreSQL + Alembic、组织/角色/OIDC、持久化调度（租约/心跳/幂等/并发额度）、worktree 执行隔离、可靠事件 outbox。保持原文档，仅把「每用户/工作区并发额度」明确为 1.1 的前置语义。

---

## 3. native 路径的 v1.1 结构性问题（现状核实）

`pico-legacy-runtime/pico/agent_loop.py` 与 `runtime.py` 核实结果：

| v1.1 文档列出的问题 | 当前代码状态 |
|---|---|
| 非空普通文本直接成为 final | ✅ 已修复：`parse()` 对无 `<talk>/<tool>/<final>` 标记的纯文本返回 `retry` |
| 「只宣布未来动作」靠不完整正则拦截 | ❌ 仍在：`is_deferred_action_answer()` 仍是固定动词表的正则，覆盖不了自然语言 |
| VERIFY 只是状态切换、无独立完成条件检查 | ❌ 仍在：`set_phase(PHASE_VERIFY)` 后立即 `finish_success`，中间无门禁 |
| `finish_success()` 无条件标绿 checklist | ✅ 已修复：`finish_success()` 不再触碰 `completed_items` |
| read-only 零读取也能结束 | ❌ 仍在：native 无意图概念，任何请求都可零工具结束 |

**结论**：LangGraph 路径（Worker 生产入口已走 `run_agent(enable_planning=True)`）已用「严格 JSON 协议 + 确定性完成门禁 + Evidence Ledger」覆盖了后三项；native 路径仍保留三项缺陷。

**决策（已选定 A）**：
- **A（已选定）**：`backend_process` 的 `NativeRuntimeAdapter` 迁移到 LangGraph，native 明确降级为「CLI/评测兼容」，不再作为 Web 生产路径维护。这样 native 路径的三项缺陷随生产路径下线而消失，0.5 不产生额外工作量。
- ~~B：若 native 必须保留，则补齐「VERIFY 完成门禁 + read-only 零证据拒绝」，并接受 deferred-action 检测的固有限制。~~

---

## 4. 重排后的版本路线

> 编号以 §2 任务表为准，本节为能力摘要。

```text
V1.1   Agent MVP 收尾
        前端分层展示 + 左侧可折叠运行轨迹条 + 三级改名
        推理强度选择器 + capability 上报
        native 路径决策（已选 A：Web 生产路径全走 LangGraph）
        审计与轨迹可视化 + 后端统一事件契约
        主循环编排收敛决策（架构决策，贯穿 V1.2–V1.4，见 §7.7）
        Review 反馈闭环 / 预算收敛 / 工具容错

V1.2   并发与交互基座
        多任务并发（TaskRunner 去全局单飞，按 workspace 串行写）
        同 workspace 写隔离（worktree/snapshot）
        Docker Sandbox（非 root / 只读 rootfs / 资源与网络限制）
        SQLite 控制面迁移（幂等导入 + 回滚）
        运行中追加新请求（agent loop inbox）
        预算自动调整（方法 A：intent 分级 step_budget）
        Tool Registry 抽取（纯重构，前置）
        API 供应商管理窗口（配置面：CRUD + 本地密钥 + 连接测试）

V1.3   工具协议与供应商
        原生 tool calling + Pi 四工具
        ModelProviderFactory + 推理能力协商
        durable checkpoint + 可靠 SSE
        有界并行 fan-out/fan-in（评测有收益才开）
        思考回传（reasoning 流式展示）
        预算自动调整（方法 C：capability 派生预算）
        API 供应商管理窗口（切换生效，依赖 ModelProviderFactory）

V1.4   资源与扩展基础
        本地资源树 + 附件 + 拖拽引用
        web_search / web_fetch
        只读 MCP Client
        Skills Registry 基础生命周期
        图记忆（SQLite+FTS5）
        pi 作为 subagent + A2A + LLM-as-a-Judge

V1.5   生态与协作（远期）
        Marketplace（冻结候选）
        Agent Swarm 跨 Worker
        关联工作区
        浏览器自动化
        移动端

V2     多租户（原 multi-user-v15-roadmap.md 不变）
```

---

## 5. 关键依赖关系（为什么是这个顺序）

1. **1.1 并发 依赖 1.2 写隔离**：放开并发前必须先有「同 workspace 写串行 + worktree」，否则写任务互相覆盖。二者属同一版本，1.1 先落地「读并发 + 写按 workspace 串行」的安全默认，1.2 再把写隔离升级为 worktree/snapshot。
2. **1.5 与 1.1 共享输入抽象**：1.1 解决「多任务并行」，1.5 解决「单任务内追加输入」，二者都要求 agent loop 的输入从「单条 `user_message`」改为「队列化 inbox」，应在同一版本统一设计，避免两次返工。
3. **1.3 Sandbox 独立**：可并行推进，不依赖并发；但它是「无人值守放开 `bash`」的硬前提。
4. **1.4 SQLite 是 2.3 和 V2 的前置**：没有可靠事件表就没有 `Last-Event-ID` 重放，没有租约表就没有多 Worker 调度。
5. **2.1 Tool Registry 是 3.3/3.4 的前置**：MCP/Skills 的工具必须挂到 Registry，否则又是各自为政。
6. **2.5 思考回传 依赖 2.2**：干净的 reasoning 捕获需要 provider-neutral 的流式合同；可先在 OpenAI-compatible 路径做快速版，若需要可提前到 V1.2。
7. **3.5 图记忆 依赖 1.4 SQLite**：但可降级（失败回退 LayeredMemory），故排 P3 末位。
8. **3.6 复用现有能力，仅 A2A 是新接入**：subagent 复用 `delegate`/`create_role_delegate`，Judge 复用 `pico/evaluation/`；A2A 与 3.3 只读 MCP 同属「开放协议接入」，同期完成凭据/审计/只读策略。
9. **4.x 全部后置**：价值依赖规模，且 DSH 已占领插件生态心智，不硬追。
10. **2.7 是 2.2 的用户可见配置入口**：2.7 提供多供应商 CRUD、本地密钥、默认切换与连接测试（配置面），2.2 提供按 `provider_id`/协议实例化与能力协商（路由面）；二者共享同一供应商定义，V1.3 内先落地 2.7 的配置 + 单默认路由，再接 2.2 的能力协商，避免维护两套 provider 定义。
11. **0.8 主循环收敛是 1.5/2.1/2.2/3.6 的共同主线**：把 `AgentLoop` 提升为一等可复用循环后，inbox（1.5）、原生 tool calling（2.1）、统一 provider（2.2）、pi-as-subagent（3.6）都只是该循环的输入或能力，不再各自往 `graph` 上加节点/边；未做此决策就分头实现，会导致四套输入/工具/子代理接口返工。
12. **1.7 Tool Registry 抽取是 2.1/2.2/3.3/3.4 的公共前置**：先在 V1.2 把 `BASE_TOOL_SPECS` 解耦成版本化注册表（零行为变更），V1.3 上原生 tool calling、多供应商、MCP、Skill 时都只往注册表加条目，不再改工具清单与 prompt 组装。

---

## 6. 与原文档的差异摘要

| 能力 | 原计划 | 新计划 | 变化 |
|---|---|---|---|
| 多任务并发 | V2「持久化调度」隐含 | **V1.2** 显式首位 | 前移 |
| Docker Sandbox | V1.2 第 5 步 | V1.2 | 保持，位置前移到 1.3 |
| SQLite 控制面 | V1.2 第 1 步 | V1.2 | 保持 |
| Tool Registry / 原生 tool calling | V1.2 | V1.3 | 后移 |
| 多供应商 | V1.2 | V1.3 | 后移 |
| 图记忆 | V1.2 | V1.4 | 后移 |
| MCP / Skills | V1.3 | V1.4 | 后移 |
| Marketplace / Swarm / 浏览器 / 移动端 | V1.3 | V1.5（冻结候选） | 大幅后移 |
| 审计与轨迹可视化 | 无 | V1.1 | 新增（§7.1） |
| 运行中追加新请求 | 无（V1 明确「不包含」） | V1.2 | 新增（§7.2） |
| pi subagent + A2A + Judge | 无 | V1.4 | 新增（§7.3） |
| 思考回传（reasoning 流式展示） | 无（v1.2 §4.5 仅「可选摘要」） | V1.3 | 新增（§7.4） |
| 前端主基调借鉴 DSH | 无 | 全局 | 新增原则（§8） |
| 预算自动调整（方法 A + C） | 无 | V1.2 / V1.3 | 新增（§7.5） |
| API/模型供应商管理窗口 | 无（仅单 provider 表单） | V1.3 | 新增（§7.6） |
| 主循环编排收敛（单一可复用循环） | 无（固定三角色 DAG） | 决策前移，贯穿 V1.2–V1.4 | 新增（§7.7） |
| Review 反馈闭环 / 预算收敛 / 工具容错 | 无 | V1.1 收尾（0.9） | 新增（§7.8） |

核心变化一句话：**把「并发 + 沙箱 + SQLite」这三个决定产品能走多远的硬能力提前，把「工具生态 + 扩展 + 协作」这些决定产品能做多广的软能力推后；并新增「审计可视化 / 运行中追加输入 / pi 作为 subagent 与 Judge / 思考回传 / API 供应商管理窗口 / 主循环收敛 / Review 反馈闭环」与「前端借鉴 DSH」主基调。** 另据 2026-08 代码核实：V1.1（P0）已基本完成，SQLite 控制面、intent 分级预算、Docker Sandbox 后端三项 V1.2 能力也已提前落地。

---

## 7. 新增需求详解

> 本节为新增需求，不在原三份版本需求文档中，以本节为准。

### 7.1 审计与轨迹可视化（0.6）

**目标**：把已落盘的审计数据（`trace.jsonl`、`task_state.json`、`report.json`、EventSink 事件）做成可交互的时间线/图视图，让用户看到「意图路由 → 模型调用 → 工具执行 → 审批 → Review → 终态」的完整因果链。

**为什么现在做**：审计数据已经齐备（EventSink + JSONL trace + TaskState），缺的只是可视化层；且「可解释、可审计」是 ThreadForge 相对 DSH 的核心差异化，应尽早把这一卖点变成可见产品。

#### 7.1.1 左侧可折叠运行轨迹条（替换现有对话索引）

在会话正文左侧提供一个与当前 Run 同步的可折叠轨迹面板，作为长对话的运行导航。视觉语言借鉴截图的“尺度切换 + Input / Model / Tools 分层泳道 + 事件条”，但整个控件必须**纵向展开**：时间从上到下流动，事件条也沿纵向延伸；不得把横向时间轴塞入左侧。只有折叠状态才收成左侧淡色细轨。该控件直接替换现有 Run Minimap/对话快速索引，不再并存两套索引，避免用户同时理解“消息位置”和“运行事件”两种导航模型。

**布局与交互**：
- 展开态参考分层时间轴：提供纵向排列的 `Duration`、`Turns`、`Calls` 尺度切换；主体使用横向并排的 `Input`、`Model`、`Tools` 泳道列，并可按事件自动增加 `Plan`、`Approval`、`Review`、`System` 列。纵轴是唯一的时间/序号方向，最新事件向下增长。
- `Duration` 按真实开始/结束时间将事件投影为纵向长度，并保留空闲等待间隔；`Turns` 按模型轮次等高排列；`Calls` 按模型/工具调用序号等高排列。切换尺度只改变纵轴投影，不改变事件本身。
- 每个色块对应一个可定位事件：色块是沿纵轴延伸的竖条，而非横条；输入、模型请求/流式响应、工具调用、审批等待、Review、重试、错误和终态使用稳定但可区分的颜色、边框与图标。运行中事件显示进行态，完成后冻结实际高度和耗时。
- 点击或键盘激活色块必须滚动到正文中对应的消息、思考摘要、工具卡片、审批卡片或失败事件，并短暂高亮；正文滚动时，轨迹条同步标出当前视口所在事件。
- 顶部提供本 Run 内搜索与类型/状态筛选；命中结果可逐项跳转。不得把未加载的历史误显示为“无结果”。
- 默认以窄条折叠在会话左边，只显示淡色纵向位置刻度、当前进度和失败标记；展开后向右展开为窄而高的纵向泳道面板，显示列标题、竖向事件条、标签、耗时和搜索。折叠状态按用户保存在本地，不跨用户同步。
- 多 Run 会话只展示当前选中 Run；切换 Run 时保留缩放/折叠偏好并重新定位。尚无 Run 的草稿会话不显示空轨迹。
- SSE 到达后增量追加或更新对应事件，不重建整个控件、不重置滚动位置，也不得出现“1 秒到 4 秒后又归零”的计时循环。刷新/重连后按事件序号恢复同一轨迹。
- 控件与正文共用纵向滚动基准：拖动或滚动控件时滚动到对应正文位置，正文滚动时同步更新纵轴位置；不使用独立的横向时间轴滚动。窄屏下控件默认折叠为侧边按钮，按需覆盖展开；不得压缩正文至不可读宽度。所有色块支持键盘焦点、可读标签和非颜色状态表达。

**事件投影规则**：
- 一条模型流式响应在时间轴中是一个持续事件，不按 token 生成大量色块；重试是新的 attempt，并与原 attempt 关联。
- 一个工具调用从 `tool.requested` 延续到 `tool.completed` / `tool.failed` / `tool.cancelled`；审批等待作为该调用的子区间显示，不伪装成工具执行耗时。
- `talk`、`plan`、`review`、`final` 分别映射到正文节点；后台心跳、租约续期等无用户价值事件默认隐藏，只在 Trace/审计查看器中可见。
- 缺失结束事件时显示 `incomplete`，不得推测为成功；收到 Run 终态后仍未闭合的事件显示为异常并进入审计告警。

#### 7.1.2 Trace 与审计查看器

运行轨迹条提供“查看完整轨迹”入口，打开独立的 Trace/审计查看器。该视图用于调查和验收，不挤进日常对话正文，也不能直接修改运行状态。Trace/审计查看器本身也必须使用 7.1.1 的同一套纵向分层时间轴控件，不能降级成普通事件表、横向 Timeline 或另外设计一套不一致的控件。

**同一控件的完整形态**：
- 页面左侧以完整高度固定展示纵向分层时间轴，保留 `Duration`、`Turns`、`Calls` 切换、搜索、筛选、缩放、纵向滚动和 `Input / Model / Tools / Plan / Approval / Review / System` 泳道列；时间自上而下，事件均为竖向条。它是 Trace/审计页面的主导航，不是附属缩略图。
- 会话左侧形态和 Trace/审计形态复用同一个时间轴组件、事件投影器、颜色/状态语义和选中状态。两者只允许在布局密度、默认筛选和详情深度上不同，不能各自解析事件或产生不同结论。
- 从会话左侧打开 Trace/审计页时，自动携带当前 `run_id`、选中的 `event_id`、尺度、筛选条件和可见时间范围；返回会话后恢复原位置。
- 在完整时间轴选择色块时，下方因果图、事件详情和审计表同步选中同一事件；反向选择图节点或审计行时，时间轴滚动并高亮对应区间。
- 支持在纵轴上拖动时间范围或框选连续竖向区间，下方所有视图只展示该范围；提供“一键查看全部”和“定位当前运行位置”。实时运行时可选择自动跟随最新事件或暂停跟随，暂停只影响视图，不影响事件接收。
- 时间轴可在单 Run 视图中查看全部 attempt，也可按 attempt 分组/折叠；模型流、工具执行、审批等待和重试必须按真实嵌套区间显示，使耗时归因可以直接从控件中读出。

**视图组成**：
- **纵向分层 Trace 时间轴（主控件）**：完整复用截图式 `Duration / Turns / Calls` 的分层语义，但以纵轴展示 `plan.*` / `model.*` / `tool.*` / `approval.*` / `review.*` / `task.*` 的顺序、嵌套关系、真实耗时、等待耗时、重试次数和状态；所有事件以竖向条表现，支持按类型、状态、节点、attempt 和时间范围筛选。
- **因果图**：以 LangGraph 的实际执行事件为数据源，显示意图路由、Planning、Research、Execute、Review、重试和终态之间的有向边；并发节点并列显示，fan-out/fan-in、被跳过节点和中断边必须可辨认。
- **事件详情**：选中节点后显示时间、序号、`trace_id`、父事件、阶段、状态、模型/工具名称、耗时、token/预算摘要、输入输出脱敏摘要、错误码及关联正文位置。模型原始隐藏 CoT 不属于可展示字段。
- **审计表**：按时间列出 actor、owner、device、workspace、session、task、run、来源、版本、意图路由、审批决策、工具风险等级、取消、重试、delegate、终态和 `stop_reason`；支持当前 Run 的筛选和 JSON 导出。
- **诊断摘要**：自动计算总耗时、模型耗时、工具耗时、审批等待、空闲等待、关键路径、调用次数、失败/重试次数和预算使用，帮助定位“慢在哪里”与“为什么结束”。诊断只基于事实事件，不由前端猜测根因。

**统一事件契约**：
- 时间轴、因果图、审计表和正文定位必须消费同一份规范化事件流，至少包含 `event_id`、`parent_event_id`、`sequence`、`trace_id`、`task_id`、`run_id`、`type`、`phase`、`attempt`、`started_at`、`ended_at`、`status`、`summary` 和脱敏后的 `attributes`。
- EventSink 是实时事实源；`trace.jsonl`、`task_state.json` 和 `report.json` 用于恢复与交叉核对。前端不得直接解析多份文件后自行猜测状态，API/Worker 应先完成规范化。
- 同一 `event_id` 的流式更新必须幂等；乱序事件按 `sequence` 合并，断线重放不得产生重复色块或重复审计行。
- 事件与正文节点共享稳定锚点；无法定位时显示“正文节点不可用”，但仍允许查看事件详情。

**安全与权限**：
- 可视化只渲染脱敏后的摘要，绝不回显 API key、设备令牌、Cookie、base URL、隐藏 CoT、绝对路径或未经脱敏的工具输入输出。
- Trace 与审计接口继续执行 owner/device/workspace/session 权限校验；本地正文和文件内容仍由对应 Worker 持有，中央服务只返回允许的索引与摘要。
- 导出仅限当前用户有权访问的当前 Run，默认导出脱敏 JSON；导出动作本身写入审计。V1.1 不提供跨用户、跨 workspace 或跨 Run 全局检索。

**验收标准**：
1. 一个包含 Planning、两次模型调用、三个工具调用、一次审批、一次 Review 和 final 的 Run，能在会话左侧与 Trace/审计页的同一纵向时间轴控件中按三种尺度完整显示；时间自上而下、泳道横向并列、事件均为竖向条，两处事件数量、顺序、状态、耗时和颜色语义一致。
2. 从左侧轨迹点击任一用户可见事件，正文能定位并高亮对应节点；从正文滚动到该节点，轨迹也能反向标记。失败、取消和 incomplete 状态同样可定位。
3. SSE 运行、浏览器刷新和断线重连后，计时、顺序、状态和当前定位保持一致，不重复、不归零、不把运行中事件误判为终态。
4. Trace/审计页的完整时间轴与因果图、事件详情、审计表双向联动；因果图能正确表现串行、并行、重试、跳过与中断，审计表能解释最终 `stop_reason`，并能区分模型耗时、工具耗时和审批等待。
5. 权限测试、脱敏测试和导出测试证明秘密、隐藏 CoT、绝对路径和未授权 workspace 数据不会进入 UI 或导出文件。

**不做什么**：不把 trace 文件作为控制流依据（保持 EventSink 旁路观测的既有原则）；不做跨 Run 的历史搜索（那是 V1.2 SQLite 之后的事）。

### 7.1.3 后端统一事件契约（0.7，前置）

**目标**：把 EventSink 事件规范化为一份可跨端消费的事件契约，让前端时间轴/因果图/审计表消费同一份流，而不是各自解析 `trace.jsonl` 后猜测状态。

**事件字段（至少）**：`event_id` / `parent_event_id` / `sequence` / `trace_id` / `task_id` / `run_id` / `type` / `phase` / `attempt` / `started_at` / `ended_at` / `status` / `summary` / 脱敏 `attributes`。

**后端改动点**：
- `pico` EventSink：模型、工具、审批、Review 事件补 `started_at`（开始）与 `ended_at`（终态），并用 `parent_event_id` 关联父子区间（工具→审批等待、execute→review、attempt 之间）。
- `local-worker` `RemoteExecutionHooks`：`tool.started/tool.completed`、`model.started/model.completed`、`approval.required/resolved` 已天然成对，补 `parent_event_id` 链接即可。
- `api-server` `event_publisher` / `task_events`：把规范化事件作为 SSE 主数据源下发，`task.snapshot` 携带归一化 run 事件；`trace.jsonl` / `task_state` / `report` 降级为交叉核对。

**不变量**：
- 同一 `event_id` 幂等；乱序按 `sequence` 合并；断线重放不产生重复事件。
- 原始隐藏 CoT 不进入 `attributes` / `summary`。
- 事件与正文节点共享稳定锚点（复用现有 `event_id` / `tool_call_id`）。

**为什么是前置**：§7.1.2 的因果图需要 `parent_event_id`，真实耗时区间需要 `started_at`/`ended_at`；没有这一层，前端只能画「瞬时点」，无法表达模型流式响应/工具调用/审批等待的嵌套区间。

### 7.2 运行中追加新请求（1.5）

**目标**：任务运行期间，用户可以向同一会话注入新的请求/上下文，由 agent loop 在下一个获准的模型轮次消费，而非重启任务。

**为什么排 V1.2**：这是 DSH inbox 机制的核心价值（「注入的上下文会留在 inbox 中，直到另一条消息将其唤醒」）；它和「多任务并发」同属运行时交互模型改造，都需要在 agent loop 层面引入队列化输入，故与 1.1 同期统一设计。

**关键设计点**：
- agent loop 引入 inbox/队列：`run()` 不再是单条 `user_message`，而是从队列领取下一条输入。
- 注入语义：新消息在「下一次获准的模型请求」落地，不打断当前已发出的模型调用（与现有取消语义一致）。
- 一致性：注入的消息必须进入会话历史（Worker 本地正文），并与 SSE 事件序号一致；刷新/重连后可恢复。
- 安全：注入消息仍走 owner/device/workspace 校验，不能绕过审批、预算或意图路由；不得借注入改变任务终态。
- 终态约束：任务一旦进入 terminal，注入只创建新 Run（或拒绝），不复活已结束任务。

**与 1.1 并发的关系**：1.1 解决「多个任务并行」，1.5 解决「单任务内追加输入」；二者共享 agent loop 的输入抽象，应在同一版本内统一设计。

### 7.3 pi 作为 subagent + A2A + LLM-as-a-Judge（3.6）

**目标**：把 Pico runtime 从「固定三角色（Research/Execute/Review）的编排内组件」提升为可独立调用的能力：

1. **pi 作为 subagent**：子 agent 的通用机制（薄循环 + `allowed_tools`/`read_only`/预算约束）已并入 §7.7 目标架构，本节不再重复；此处只新增「把 Pico 泛化为可跨进程调用的通用 subagent」这一层。
2. **LLM-as-a-Judge**：把现有 `pico/evaluation/`（evaluator / metrics / verifier）与 Review delegate 提炼为独立 Judge 模块，可对任意「候选答案 vs 验收标准」输出结构化判定（`pass` / `needs_fix` + 理由 + 指标）。
3. **A2A 传输**：通过 Google Agent2Agent 协议（Agent Card + Task / Message / Artifact）把上述 subagent 与 Judge 暴露为可互操作的 agent，使外部 agent / 工具能跨进程调用 pi 的评审能力。

**为什么排 V1.4**：
- subagent 与 Judge 复用现有 `delegate` + `evaluation`，是「已有能力的泛化」，不属于从零建设；
- A2A 是开放协议（类似 MCP），可借力而非自造，与 3.3 只读 MCP 同期接入；
- 它服务于「多 agent 协作」这一 DSH 已有、但本地优先工具近期边际价值有限的领域，故放扩展基础而非核心。

**边界与安全不变量**：
- subagent 不继承宿主全部权限：allowlist、深度/步数预算、只读/写入隔离沿用 §7.7 的执行边界三约束。
- Judge 只能消费明确发布的 plan / talk / evidence / Review 与脱敏 reasoning summary，不得读取或外发原始隐藏 CoT。
- A2A 暴露面默认只读；写入 / 审批 / 发布类操作逐次审批，且 A2A 凭据只存本地（同 MCP 凭据策略）。
- A2A 引入不绕过 `owner/device/workspace` 校验与审计；每个 A2A 任务带 `trace_id` / `source` / `version`。

**不做什么**：不做无预算的 Agent 树（Swarm 仍为 V1.5）；不把 A2A 当作「跨 owner 共享数据」的通道；不自造加密协议（如需跨信任域，沿用 V1.2 的加密 ADR 结论）。

### 7.4 思考回传（reasoning 流式展示，2.5）

**目标**：把模型的「思考过程」作为一等公民流式回传到前端，用户在 Agent 给出正文/工具调用前可看到其推理（借鉴 DSH 的可折叠「思考」区）。

**与「原始 CoT 不落盘」的边界（必须严格遵守）**：
- 只回传**供应商明确提供**的 reasoning 内容（`reasoning_content` / `reasoning_summary` / `reasoning` 字段）。
- 不得从普通输出文本中猜测、拼接或伪造 reasoning；供应商未公开内部推理时，思考区显示「无可用思考摘要」，而不是编造。
- 回传的思考内容经过 secret-shaped 脱敏 + 长度限制 + 来源标记，且**不写入 final_answer、不构成 evidence、不进入完成门禁、不作为 plan/Review 的判定依据**。
- 原始隐藏 CoT 仍不落盘（不进 task_state / trace / report / 会话历史 / 图记忆），不出站（不进日志 / 审计正文）。

**全栈链路**：
1. 模型客户端：从流式响应捕获 `reasoning_content`（与正文 delta 分流），归一到 `reasoning.delta` / `reasoning.completed`；
2. Worker：`RemoteExecutionHooks` 增发脱敏后的 reasoning 事件，与 `assistant.delta` 分流；
3. 控制面：SSE 事件信封新增 `assistant.reasoning.delta` / `assistant.reasoning.completed`；
4. 前端：消息气泡上方渲染可折叠「思考过程」区，默认折叠、可展开、流式增长，完成后标注来源与耗时。

**安全不变量**：
- 思考内容与正文用不同事件类型隔离，前端不得把思考内容当作最终回答展示；
- 刷新/重连后思考内容与正文一样可恢复（进入会话正文的受控投影），但原始 CoT 仍不持久化到中央；
- reasoning 流复用 `model_text_delta` 的增量 redaction 逻辑（secret 后缀保留），保证凭据不因分块泄漏。

**为什么排 V1.3**：干净的 reasoning 捕获需要 provider-neutral 的流式合同（2.2 ModelProviderFactory）；在此之前可先做 OpenAI-compatible 路径的快速版（只捕获 `reasoning_content`），若需要可提前到 V1.2。

### 7.5 预算自动调整（方法 A + 方法 C）

**背景**：当前「继续」续跑直接报 `Coordinator step budget was exhausted`，规划阶段又 `模型服务响应超时`。根因是两套写死的预算：`max_steps=6`（步骤硬上限）与 `DEFAULT_TOTAL_BUDGET=12000` 字符 + 规划器 `timeout=45s`。

#### 方法 A：intent 分级 step_budget（1.6，V1.2）

- 把 `step_budget` 从单一默认值改为按 resolved intent 分级：`conversation=2 / read_only=8 / code_change=16`（作为硬上限）。
- 复用现有「软预算自动扩展」机制（`_budget_failure` / `plan_budget_extended`）：规划器自报 `plan.budgets` 作软预算，用超后自动顶到 intent 硬上限才失败。
- 语义：intent 分级是「floor + 默认」，用户显式传入更大的 `max_steps` 仍被尊重；`code_change` 的修复循环（fix loop）从此有预算。
- 落地要点：在 `intent_router_node` 解析出 intent 后抬高 `state["step_budget"]`（`max(当前, INTENT_STEP_BUDGETS[intent])`）；把规划器 prompt 的 `maximum_budgets.tool_calls` 与执行硬上限解耦，避免规划器被 6 卡住。

> **状态**：方法 A 已实现（`INTENT_STEP_BUDGETS` + `_intent_step_budget` + `_budget_failure`/`plan_budget_extended` 软扩展），§2 任务 1.6 已标 ✅。

#### 方法 C：capability 派生 prompt 预算 + 规划器超时（2.6，V1.3）

- `prompt total_budget`：由模型 context_window 派生（如 `context_window × 0.5 − max_new_tokens − 安全余量`），替换硬编码的 `12000` 字符；`history`（session）section 同步放大。
- 规划器超时：由「模型实测首 token 延迟 + 计划输出 token 预算」估算，替换硬编码的 `min(45, model_timeout)`；推理模型至少给到 `min(120, model_timeout)`。
- 依赖 2.2 `ModelProviderFactory` 的 capability 上报（context_window / 首 token 延迟 / 推理档位）；在此之前可先在 OpenAI-compatible 路径做最小版（放宽超时 + 调大 prompt 预算常量）。

**安全不变量**：预算自动调整只改「上限与默认值」，不改变审批、workspace、owner 校验与失败收敛；硬上限永远存在（如 `code_change ≤ 25`），防止正反馈把预算越顶越高。

### 7.6 API/模型供应商管理窗口（2.7）

**目标**：在 Web Console 里提供一张「模型供应商/API 端点」管理页（界面语义借鉴 ccswitch、Hermes Studio、OpenClaw 的 Providers/API 页），让用户集中增删改查多个供应商（base URL、API key、模型列表、协议/推理档位），一键切换当前生效供应商，并测试连接与查看健康状态；密钥只存本地 Worker，中央不回显。

**为什么现在做**：当前只有「单 provider」配置面——`client/src/features/devices/WorkerDevices.tsx` 的 `base_url / api_key / model` 三字段表单，写入后由 `local-worker` 的 `save_model_env()` 落盘为单一 `PICO_OPENAI_*` 环境；`api-server` 的 `config.py` 也在启动时只冻结一组 `PICO_OPENAI_*`。要「换 provider / 换模型 / 对比多个端点」就必须改表单加重启，无法像 ccswitch 那样一键切换。2.2 已把「多供应商 ModelProviderFactory + 推理协商」列为 V1.3 核心 seam，API 管理窗口正是它的用户可见入口；先有窗口，工厂才有可配置对象。

**参考产品（借鉴交互语义，不照抄品牌/样式）**：
- **ccswitch**：供应商卡片列表 + 一键切换当前生效、分组/标签、密钥本地化、连接自测，把「切换端点」做成零摩擦操作。
- **Hermes Studio**：Providers 设置页的 base URL / key / model 表单 + 测试 + 模型发现（list models）。
- **OpenClaw**：provider 配置 + 凭据 vault，密钥脱敏回显（如 `sk-…xxxx`）。

**功能范围（V1.3 最小可交付）**：
1. 供应商列表：卡片/表格展示 name、协议、base URL、模型、默认/活动标记、状态（active / disabled / error）、最近测试时间。
2. 增删改查：新增/编辑字段 = name、protocol（openai-compatible / anthropic / deepseek / ollama，复用 `pico/providers` 已有客户端类型）、base_url、api_key、model（或模型列表）、推理档位、timeout、并发。
3. 一键切换：标记一个「默认/活动」供应商，任务新 Run 默认路由到它；切换动作写审计（who / device / workspace / from / to / 时间）。
4. 连接测试 + 模型发现：`test` 做最小握手（list models 或 1-token 调用），返回延迟、可用模型、错误码；测试过程不得把 key 写入日志/SSE/审计。
5. 状态与健康：显示每个供应商 last_test_at、last_error、协议版本；运行中切换不得影响已发起的 Run。
6. 作用域：供应商默认归属 device/workspace（延续现有 per-device `save_model_env()` 语义）；owner 可管理，切换只影响该 device 后续 Run。

**数据模型（中央只存非秘密字段）**：

```text
provider_id, owner_id, device_id, name, protocol, base_url, model, models[]
reasoning_tier, timeout, concurrency, state, is_default, last_test_at, last_error, schema_version
```

`api_key` 不属于中央数据库，也不进 `task_state / trace / report / SSE / 审计正文`。

**密钥安全不变量**：
- api_key 只写 device 本地安全存储/本地 env 文件（沿用 `local-worker` `save_model_env()` 的校验与本地落盘），中央 API 只收发 `has_key` 布尔与脱敏尾串。
- 回显、日志、SSE、审计、错误信息一律脱敏；`test` 与 `configure` 消息中的 key 沿用现有 `args_preview` redaction（已有测试覆盖 `api_key` 不回显）。
- 删除/替换 key 时从本地存储与内存同时清除；key 变更不触发旧 Run 复用。

**API 契约**：

```text
GET    /api/v1/providers
POST   /api/v1/providers
GET    /api/v1/providers/{provider_id}
PATCH  /api/v1/providers/{provider_id}
DELETE /api/v1/providers/{provider_id}
POST   /api/v1/providers/{provider_id}/test
POST   /api/v1/providers/{provider_id}/activate
GET    /api/v1/providers/{provider_id}/models
```

**与 2.2 的关系**：2.7 是配置面（UI + 本地密钥 + 切换 + 测试），2.2 是路由面（ModelProviderFactory 按 `provider_id`/协议实例化客户端并协商能力）。配置面（CRUD + 本地密钥 + 连接测试）不依赖工厂，可提前到 V1.2；「一键切换真正生效」依赖 2.2 工厂，仍属 V1.3。两者共享同一 `provider_id` 与 capability 上报，不得维护两套供应商定义。

**不做什么**：不做账单/用量统计、不做密钥轮换代理、不做跨 owner 供应商共享；不做「运行时自动故障切换」（属 2.2 后续）；不回显或导出完整 api_key。

**验收标准**：
1. 新建 openai-compatible 与 deepseek 两个供应商，各自「测试连接」返回可用模型与延迟；仅一个被标记为默认，新 Run 走该供应商。
2. 一键切换默认供应商后，后续 Run 使用新供应商，已运行 Run 不受影响；切换动作出现在审计表。
3. 所有界面、SSE、日志、审计与导出文件中不出现完整 api_key，仅出现 `has_key`/脱敏尾串；删除 key 后本地存储与内存均清除。
4. 未授权 owner/device 不能查看或切换其他 device 的供应商。
5. 刷新/重连后供应商列表、默认标记、状态与最近测试时间保持一致，不与中央落盘冲突。

### 7.7 主循环编排收敛：从固定三角色 DAG 到单一可复用循环（0.8）

**目标**：把当前「intent router → execution → review」的固定三角色 DAG（`graph.py` 的 `build_graph()`），收敛为「一个可复用的单一 ReAct 循环 + 意图预算门 + 可调用的角色/评审能力」。参照系是 pi（[earendil-works/pi](https://github.com/earendil-works/pi)）与 DSH 两个独立、风格迥异的成熟项目——它们不约而同选择了「扁平单循环 + 编排做成能力/工具」，而不是硬编码研究→执行→评审的必经流水线。

**为什么现在做**：当前 `execute_change` / `research` / `review` 各新建一个子 Pico 跑 native AgentLoop，但角色顺序被 `build_graph()` 焊死成必经节点，带来三处结构性成本：① 预算在「图协调器」与「子循环」两层各自计数，需要 `budget counter drift` 断言（`backend.py`）兜底；② 阶段间靠字符串透传（`research_result → executor prompt → review`）丢上下文；③ 每加一个能力（inbox、subagent、原生 tool calling）都要再往图上加节点/边。先做本决策，后续 1.5/2.1/2.2/3.6 才能落到同一循环上，而不是各自开新接口。

**三条可借鉴设计（各自对应一个 roadmap 项）**：

1. **三层 API：`agentLoop / Agent / AgentHarness`**（pi 的 `agent-loop.ts` + `AgentHarness`）。把「循环」做成独立可复用的一层，编排壳（harness）只是循环的消费者，负责给它配 intent、预算、工具与角色。ThreadForge 已有雏形：`AgentLoop`(native 循环) / `Pico`(runtime) / `graph`(编排)，但 `graph` 直接把循环锁死在角色节点里。改造方向 = 把 `AgentLoop` 提升为一等可复用循环，`graph` 退化为「给循环配置 intent/预算/角色的 harness」。这决定 1.5（inbox）与 3.6（pi-as-subagent）的形态——它们应是循环的输入/能力，不是新图节点。

2. **`pi-ai` 统一 provider + 原生 tool calling**（合 2.1 + 2.2）。pi 用一层统一 LLM API 把 OpenAI/Anthropic/Gemini 归一成同一套原生 tool-calling 接口，循环只认这一套。这直接消灭 pi 里 `<tool>/<talk>/<final>` 文本协议、`parse()` 与 `MAX_PROTOCOL_REPAIRS` 纠偏——「模型是否调工具」由 provider 原生 tool-calling 给出，不再靠正则解析文本块。

3. **extensions = hooks + 自定义工具**（对齐 2.1 / 3.3 / 3.4）。pi 把「生命周期钩子（hooks）」和「自定义工具」统一成一个扩展面，而不是 MCP 一套、Skills 另一套。对应 ThreadForge 的 Tool Registry + MCP/Skills：MCP Server 与 Skill 都只应是「注册进 Tool Registry 的工具 + 挂在生命周期事件上的 hook」，避免两套扩展体系各自为政。

**目标架构：循环层 / prompt 组装器 / 工具注册表 三层分离**：

```text
用户请求
   │
   ├─ 协调器 intent_router / planner     ← 只注入 task + 最近消息 [+ 旧 plan + replan_reason]
   ├─ 子 agent（research/review/execute） = 同一个薄循环 + 不同配置
   └─ 薄循环每轮：prompt 组装器(5 段) → model.complete(prompt, tools) → 工具注册表.execute()
```

- **循环层（薄，像 pi）**：`while` 循环直接消费 provider 返回的结构化 `tool_call`，不再 `parse()` 文本协议；循环本身不关心自己是 research 还是 review。
- **prompt 组装器（`context_manager`，5 段策略不变）**：`prefix / memory / relevant_memory / history / current_request` 与预算压缩顺序（`relevant_memory → history → memory → prefix`）保持不变；仅 `prefix` 的「工具清单」段改为遍历注册表渲染。
- **工具注册表（`ToolRegistry`）**：唯一工具来源，循环只通过它执行工具；扩展只发生在注册表，不改变循环和 prompt 组装。

**子 agent = 同一个薄循环 + 配置约束（现状已实现，作为目标基线）**：父 agent 不派生新循环，而是用同一 `AgentLoop` + 一份配置塑造子 agent 能力：

| 约束 | research | review | execute | 生效点 |
|---|---|---|---|---|
| `allowed_tools` | list_files/read_file/search | read_file/search | 除 delegate 外全部 | `tool_executor.execute` → `tool_not_allowed` |
| `read_only=True` | ✅ | ✅ | ❌ | `tool_executor` → `read_only_block` |
| `approval_policy` | never | never | 继承父 | 无审批路径 |
| `max_steps` / `depth` | 3 | 3 | 剩余预算 | 循环预算 / delegate 深度 |

这些是执行层硬约束（不是仅“不让模型看到”），即使模型硬要调写入工具也会在 `tool_executor.execute` 被拒绝并记审计。

**当前缺口——路径级 scope**：`focus_paths`（`REVIEW_PROMPT_TEMPLATE`）目前只是写进 prompt 的软约束，执行层不校验。目标：`read_file`/`search` 调用时校验 `relative_path ∈ allowed_paths`，越界拒绝（`path_not_allowed`），实现「子 agent 只能读 focus 范围内文件」的硬约束（登记为 §7.8.5）。

**工具扩展：builtin / mcp / skill 三层注册**：

- 执行面（注册表）：按 `source=builtin|mcp|skill` 分层，都走同一 workspace / 审批 / 审计边界。
- 模型界面（prompt 看到的）：收敛为 `read(list|read|search)/write/edit/bash` 四个稳定入口，底层映射到注册表工具——模型看到的工具集稳定，扩展都发生在注册表，不改变 prompt 组装。
- 原生 tool calling 落地后，provider 给的就是 schema 校验过的 typed 参数，§7.8.4b 的无损 coerce 压力大幅下降。

**hook 扩展面：从单点调用升级为可注册 + 与 `EventSink` 的职责分界**：

- **现状已有 12 个 hook 点**（`execution_hooks.py` 的 `ExecutionHooks` Protocol）：`before_model / after_model / tool_requested / before_tool / after_tool / commentary / model_retrying / model_protocol_retrying / model_text_delta / begin_answer_candidate / commit_answer_candidate / discard_answer_candidate`。Web 端注入的 `ExecutionBoundary` 已用它做取消检查、审批线性化与 SSE 事件发布——**薄循环不是解锁 hook 的条件，hook 点已存在**。
- **现状是「写死在循环里的单点调用」**：循环直接调用一个注入的 hook 实现，加新能力要改循环或改实现类。
- **目标 = pi/DSH 的「可注册扩展面」**：同一事件可累积多个 handler（MCP/Skill/内置能力可订阅），并区分两类职责：
  - **发布型（`EventSink`）**：只记录/广播，不改变执行（现状已具备）；
  - **拦截型（可改写/拒绝的 hook）**：审批闸门（`tools/pre-execute` deny/ask）、compaction（`agent/pre-step` 改写上下文）、plan mode（拒绝写工具）、沙盒策略——它们能改变或终止当前 step。
- **落地**：审批、沙盒、retry、compaction、plan mode、审计都应作为挂在上述 hook 点上的可注册 handler，而不是散落在循环/图里的 if 分支；薄循环只在固定顺序调用 hook 点，职责分界为「`EventSink` 只发布、拦截 hook 可改写/拒绝」。

**必须保留的差异化（pi/DSH 都没有，不得因扁平化而丢）**：
- intent 分级预算（`conversation/read_only/code_change` 硬上限，§7.5）；
- 逐角色最小权限 allowlist（research/review 只读、execute 可写）；
- 证据账本（evidence ledger）+ 确定性完成门禁（review pass / completion gate）。

**分阶段路线（不一次拆图，先决策、按版本替换）**：
- **V1.2**：与 1.5 inbox 同期，把 execute/research/review 从「必经节点」改为「可调角色」；intent 路由降为入口 + 预算门；循环输入从单条 `user_message` 改为队列化 inbox。
- **V1.3**：第一步先抽 `ToolRegistry`（纯重构，可在 V1.2 提前，见 1.7）；随后与 2.1/2.2 同期上原生 tool calling + 统一 provider，删除文本协议纠偏；review 从「3 步 read delegate」升级为跑测试/verifier 的证据驱动验证。
- **V1.4**：与 3.6 同期，把角色泛化为可 fan-out 的 subagent，评审失败做局部重试而非整条 replan。

**不做什么**：不照抄 pi 的「无 intent router、无强制 review」极简形态；不删除确定性完成门禁、最小权限与审计；不在 1.5/2.1 落地前提前拆掉当前已可用的 LangGraph 生产路径。

**验收标准**：
1. 同一循环能跑 conversation / read_only / code_change 三类任务，且三类仍受各自的 intent 硬预算与最小权限 allowlist 约束。
2. research / execute / review 从「必经节点」变为「按需调用」，纯对话任务不再强制经过 research/review。
3. 预算只在单一循环内计数，不再需要 `budget counter drift` 跨层断言；阶段间上下文不再靠字符串透传丢信息。
4. 原生 tool calling 落地后，`parse()` 文本协议与 `MAX_PROTOCOL_REPAIRS` 纠偏代码可删除，且工具调用语义不退化。

### 7.8 Review 反馈闭环 + 预算收敛 + 工具容错（0.9）

**背景**：2026-08-14 一次真实 `read_only` trace（`run_be2a7b6a64574338abf957c0a323625a`，详见 `issue-log.md`）暴露三个问题：replan 不收敛（第二轮重复第一轮的搜索）、`budget_exhausted` 直接裸 `blocked`（无结论）、工具错误/类型小错误缺乏可恢复性。本节据此定四条改进，均不改变 V1.1 的三类意图门禁与 evidence ledger 语义。

#### 7.8.1 前一轮证据摘要注入 intent router / planner

- **现状**：`_classify_auto_intent` / `build_intent_prompt` 只传静态 workspace `intent_context`；`needs_fix → replan` 时 planner 拿不到「上一轮已搜过什么、已读过哪些文件」，于是重复 `search`/`list_files`。
- **目标**：`AgentState` 增加 `prior_evidence_summary`（由 child evidence 提炼 `tool_name + relative_paths + freshness + review_issues`），replan 时拼进 planning/intent prompt，使第二轮计划能针对「尚未覆盖的证据」展开，而不是重复第一轮。

#### 7.8.2 工具步数预算边界重探

- **现状**：`INTENT_STEP_BUDGETS.read_only = 8`，对「全库搜证/验证型」只读任务偏紧（本次正好 8 步烧光）。
- **目标**：read_only 按「纯读 / 验证搜证」细分，或按 `plan.steps` 派生预算（方法 C 思路）；复用软预算扩展（`_budget_failure` / `plan_budget_extended`）。原则不变：**软预算自动顶、硬上限封顶**，不无脑调大。

#### 7.8.3 失败 / 预算耗尽时的 best-effort 答案

- **现状**：`budget_exhausted` / `review_retry_limit_reached` 直接 `blocked`，`final_result` 为空，用户只看到 `budget_exhausted`。
- **目标**：在上述耗尽点用一次受限模型调用，把已收集 evidence 摘要 + `review_issues` 收敛成「已确认 X / 未能确认 Y / 建议 Z」的可读结论；终态可仍为 `blocked`，但结论必须可读（借鉴 pi「retry 改不对也把答案交给用户」的哲学）。

#### 7.8.4 工具容错（借鉴 pi）

- **4a 工具错误回填 + 可操作提示**：当前 `tool_executor` 已把 `error: ...` 回填给 LLM，但缺「可重试/致命」区分与下一步提示（如 `read_file` 目标不存在 → 提示先 `list_files` 定位真实路径）；瞬时读错误（超时/短暂文件锁）可选自动重试一次再回 LLM。
- **4b schema 无损 coerce**：`validate_tool` 目前对 `"1"`、`"true"` 等小类型错误直接 reject。增加无损、无歧义的自动纠正（纯数字字符串→int、`"true"/"false"`→bool、单元素 list→标量），仍 fail-closed 拒绝路径逃逸、负行号、非法枚举等真实错误。

#### 7.8.5 路径级 scope（子 agent 读范围硬约束）

- **现状**：`focus_paths` 只写进 `REVIEW_PROMPT_TEMPLATE` 作软约束，`read_file`/`search` 执行层不校验路径范围，子 agent 理论上可读 focus 之外的任意工作区文件。
- **目标**：`read_file`/`search` 调用时校验 `relative_path ∈ allowed_paths`，越界返回 `path_not_allowed` 并记审计，实现「research/review 子 agent 只能读 focus 范围内文件」的最小权限。
- **边界**：不影响 execute（写执行器）的路径范围（其授权仍是整个 workspace）；不改变 workspace 根目录之外的既有越界拒绝语义。

**不做什么**：不改 review 的确定性门禁语义；不放宽路径/workspace/审批安全边界；不因容错把占位能力显示为可用。

**验收标准**：
1. 同一 `read_only` 验证任务，replan 后第二轮不再重复第一轮已完成的搜索（`prior_evidence_summary` 生效）。
2. 预算/重试耗尽时返回 best-effort 结论，而不是裸 `budget_exhausted` / 空 `final_result`。
3. 工具参数的小类型错误被自动纠正，路径逃逸、负行号、非法值仍被拒绝。
4. V1.1 的三类意图门禁、evidence ledger 与最小权限 allowlist 无回归。

---

## 8. 前端主基调：主要借鉴 DSH

> 本节确立前端设计与交互的参照方向。不是「复制 DSH」，而是「以 DSH 为第一参照、按需裁剪」，把自研精力留给 ThreadForge 的差异点。

**为什么**：DSH 在「coding agent 交互范式」上已验证了会话事件驱动、可折叠思考、轨迹回放、插件化 UI 节点等模式；ThreadForge 的差异化在多用户、审批、审计与本地 Worker，前端借鉴 DSH 的交互范式即可快速达到可用体验。

**借鉴清单（借鉴 DSH）**：
1. **会话事件驱动 UI**：UI 状态由 append-only 会话事件流派生（而非前端自行拼装），支持回放、刷新恢复、事件序号。
2. **可折叠思考区**：思考（reasoning）作为独立、可折叠、流式增长的元素，与正文/工具调用分离（见 §7.4）。
3. **工具调用卡片**：tool call 的参数/结果预览、状态、耗时、审批入口做成结构化卡片。
4. **左侧可折叠运行轨迹条**：用 `Duration / Turns / Calls` 多尺度泳道替换现有 Run Minimap/对话索引，支持流式进度、搜索、点击定位和正文反向高亮；完整 Trace 与审计从该控件进入（见 §7.1）。
5. **消息/节点统一渲染**：talk、tool、review、final、思考、审批、失败终态用统一的节点渲染器（对齐 DSH 的 ConversationNodeDefinition 思路）。

**不借鉴 / 明确差异化（ThreadForge 自研）**：
- 多用户身份、设备/工作区归属、GitHub OAuth 控制面；
- 逐次审批 + 审计（`approval_id` + `args_digest` 绑定、决策幂等）；
- 本地 Worker 会话正文所有权（中央不落正文）；
- 插件化内核（DSH 的 Cordis 插件树）不照搬，仅在 Tool Registry / UI 节点层面预留扩展点（见 2.1 / 3.3 / 3.4）。

**供应商管理页的例外参照**：API/模型供应商管理窗口（§7.6）借鉴 ccswitch / Hermes Studio / OpenClaw 的 Providers/API 页交互，而非 DSH（DSH 无显式供应商管理页，配置走部署环境）。这不改变「整体以 DSH 为第一参照」的主基调。

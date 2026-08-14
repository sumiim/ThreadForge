# ThreadForge V1.3 扩展生态与高级协作需求

> 文档状态：需求基线（Draft）
> 前置条件：V1.2 核心 Agent 平台、安全边界和 Tool Registry 已稳定
> 目标：在不扩大默认权限的前提下，建设 MCP、Skills、扩展分发和高级跨 Worker 协作能力。

## 1. 版本定位

V1.3 面向“能力生态”，而不是补救核心 Coding Agent。即使本版本全部关闭，V1.2 的读取、修改、Review、Sandbox、资源访问和图记忆仍应完整可用。

本版本包含：

- 受控 MCP Client 和 MCP 工具适配。
- Skills manifest、Registry、评测、发布和回滚。
- 扩展目录/Marketplace 与版本治理。
- 高级浏览器自动化和外部副作用工具。
- Agent Swarm、跨 Worker 子任务和关联工作区。
- 后续移动端验收与扩展管理体验。

## 2. 扩展安全不变量

1. 扩展不能绕过 V1.2 Tool Registry、Sandbox、workspace、审批和审计。
2. 安装扩展不等于授予权限；manifest 只能申请权限。
3. MCP/Skill 默认只读，外部写入、付费、发布和账号操作逐次审批。
4. 第三方凭据只存 Worker 本地安全存储或系统密钥环。
5. 扩展返回内容属于不可信输入，不能修改系统提示、权限或审批。
6. 禁用、撤销或版本不兼容必须立即阻断新调用。
7. 扩展失败不能破坏基础 Agent、会话历史和图记忆。
8. 不得把“planned”页面或空接口展示成已安装能力。
9. MCP、Skills、浏览器和 Swarm 均不得请求、导出或持久化模型原始隐藏 CoT；扩展只能消费明确发布的 plan、talk、evidence、Review 和脱敏 reasoning summary。

## 3. MCP 系统

### 3.1 概念边界

MCP Server 是外部能力提供者；MCP tool 必须适配成带来源和版本的 Tool Registry 条目。Server 不能直接获得宿主 Worker 的所有文件、Shell 或网络权限。

### 3.2 Server 配置

每个 Server 保存：

```text
server_id, name, protocol_version, transport
launch_config references
credential references
tool allowlist
permission/network scopes
timeout, concurrency, rate limits
state, health, last_handshake
schema_version
```

凭据不得出现在中央数据库、Git、SSE、日志、任务结果或图记忆。

### 3.3 生命周期

```text
configured -> testing -> active -> disabled
                    |       |
                    v       v
                   error   revoked
```

- 创建后先测试握手、协议和 schema，再允许启用。
- schema 或权限变化必须重新审核，不能静默扩大能力。
- Server 断线、超时、速率限制和版本错误产生结构化事件。
- 撤销后终止连接并移除模型可见工具。

### 3.4 MCP 调用

- 每次调用经过输入 schema、权限映射、网络策略、审批、超时和输出过滤。
- 记录 server、tool、version、trace、approval 和来源。
- 返回的路径、凭据、网络响应和二进制经过脱敏、大小与内容策略。
- 首期只开放受控只读 MCP；写入能力按工具单独评测和启用。

### 3.5 API 与前端

```text
GET    /api/v1/mcp/servers
POST   /api/v1/mcp/servers
POST   /api/v1/mcp/servers/{id}/test
PATCH  /api/v1/mcp/servers/{id}
DELETE /api/v1/mcp/servers/{id}
GET    /api/v1/mcp/servers/{id}/tools
```

前端显示真实状态、权限、工具列表、健康、版本和最近错误；秘密字段只允许本地 Worker 输入，不回显。

## 4. Skills 系统

### 4.1 Manifest

最小 manifest：

```yaml
id: threadforge.example.skill
version: 0.1.0
description: "..."
entrypoint: package.module:run
required_tools: [read]
required_mcp: []
permissions: [workspace.read]
network_scopes: []
supported_platforms: [windows-x86_64, linux-x86_64]
dependencies: []
checksum: sha256:...
signature: ed25519:...
evaluation: pending
```

### 4.2 生命周期

```text
candidate -> evaluated -> active -> deprecated
             |             |
             +-> rejected  +-> rolled_back
```

- 安装、升级和启用分别校验签名、依赖、平台、协议和权限。
- Skill 只能通过 Tool Registry 和 MCP Adapter 使用能力。
- 每次运行记录 Skill id/version、依赖、权限快照和 trace。
- 禁用后立即阻断新调用，运行中任务按策略取消或完成。
- 卸载只移除 Skill 制品和注册信息，不删除会话和记忆。

### 4.3 评测与自进化

- Skill candidate 必须在隔离环境运行功能、安全和回归评测。
- 评测失败不能进入 active。
- 自进化只生成 candidate、差异和报告，不能自动提高权限或发布。
- active 升级失败必须原子回滚。
- 生产使用量、失败率、拒绝、成本和安全事件可审计。

### 4.4 API

```text
GET    /api/v1/skills
POST   /api/v1/skills/install
POST   /api/v1/skills/{id}/evaluate
POST   /api/v1/skills/{id}/enable
POST   /api/v1/skills/{id}/disable
POST   /api/v1/skills/{id}/rollback
DELETE /api/v1/skills/{id}
```

## 5. 扩展目录与 Marketplace

- Registry 保存扩展元数据、版本、平台、依赖、权限、签名、评测和弃用状态。
- 公共目录只接收可验证签名和可重复构建的制品。
- 安装前展示权限差异、网络目标、工具/MCP 依赖和本地磁盘影响。
- 自动更新默认只允许兼容且不扩大权限的版本；扩大权限必须重新确认。
- 支持发布者撤回、漏洞禁用、版本锁定、回滚和安全公告。
- 排名不能只依据安装量，应显示维护状态、评测和权限风险。
- 私有扩展目录与公共目录使用相同合同，但访问令牌只存本地。

Worker 制品、私有扩展包和公网凭据不得提交公共源码仓库。

## 6. 浏览器自动化与外部副作用

V1.2 的 `web_search/web_fetch` 仅只读。V1.3 可以增加浏览器自动化，但必须按动作分级：

| 动作 | 默认策略 |
| --- | --- |
| 打开公开页面、读取 DOM | 只读策略 |
| 登录、读取账号内容 | 每个站点授权 |
| 填写表单但不提交 | 显式确认 |
| 提交、发布、删除、付费 | 逐次高风险审批 |

要求：

- 使用隔离浏览器 profile，不能继承用户全部 Cookie。
- 域名 allowlist、下载限制、弹窗限制和凭据引用分离。
- 审批绑定站点、动作、目标和参数摘要。
- 页面指令按 prompt injection 处理，不得改变权限。
- 外部副作用形成可审计收据；无法回滚的操作必须在执行前明确提示。

## 7. Agent Swarm 与跨 Worker 执行

Coordinator 可以把计划拆成多个有依赖的 Child Task，但必须满足：

- 每个子任务有最小上下文、工具 allowlist、预算、workspace 和 Worker capability。
- root Task、plan、stage、node、child task、Worker 和 artifact 使用关联 ID。
- 只读子任务可以并发；写入子任务使用独立 worktree/snapshot。
- Coordinator 显式检查冲突、合并和 Review，模型不能自行覆盖主 workspace。
- Worker 离线、租约过期和重复结果幂等收敛。
- Swarm 深度、宽度、token、工具、CPU、网络和费用均有硬上限。
- 必须通过串行/并行消融，证明质量、耗时或可靠性收益后才能默认开启。

## 8. 关联工作区

关联工作区是虚拟上下文视图，不改变底层所有权：

```text
virtual_workspace
  -> member(device_id, workspace_id, read/write policy)
  -> context index
  -> routing policy
```

- 只能关联同一 owner 明确授权的 workspace。
- 每个成员保持独立 freshness、Git 状态和 Worker 在线状态。
- 读取上下文必须标明来源 workspace；写入任务只能选择一个明确目标。
- 跨 workspace 修改必须拆成独立子任务和审批。
- 移除关联不删除底层文件、会话或图记忆。
- 不得用关联工作区绕过路径权限或复制其他用户数据。

## 9. 移动端与远程验收

V1.3 可提供移动端 Web/PWA 或客户端，用于：

- 查看计划、talk、工具、diff、Review 和最终结果。
- 对绑定任务进行批准、拒绝、取消和重试。
- 在多 Worker 和关联工作区之间选择目标。
- 接收任务完成、失败和审批通知。

移动端默认不直接浏览完整本地文件，也不持有 Worker 长期执行密钥。所有操作经过中央身份、任务归属和短期授权校验。

## 10. 扩展可观测性与治理

审计至少记录：

```text
actor, owner, device, workspace
task, run, plan, node
source_type, source_id, source_version
permission_snapshot, approval
decision, duration, cost, result
```

- 每个 MCP/Skill/浏览器/Swarm 能力暴露耗时、失败、拒绝、超时、重试、费用和资源指标。
- 支持管理员按扩展、发布者、版本和权限禁用。
- 安全事件可以定位到制品校验和与调用 trace。
- 日志统一做 secret-shaped 和路径脱敏。
- trace、扩展遥测和第三方诊断包不得包含原始隐藏 CoT；可展示 reasoning summary 必须沿用 V1.2 的来源标记和脱敏规则。

## 11. 实施顺序

1. 冻结 Extension、MCP Server/Tool、Skill manifest 和 Registry 合同。
2. 完成只读 MCP Client、凭据隔离和工具适配。
3. 完成 Skills candidate/evaluate/active/rollback 生命周期。
4. 建设私有扩展目录，再评估公共 Marketplace。
5. 增加浏览器自动化的只读与审批动作分级。
6. 完成 Swarm、跨 Worker 任务和冲突合并评测。
7. 实现关联工作区和移动端远程验收。

## 12. CI 与验收

- `mcp`：握手、schema 变化、断线、超时、凭据和权限映射。
- `skills`：manifest、签名、依赖、平台、评测、升级和回滚。
- `marketplace`：发布、撤回、版本锁定、权限扩大和漏洞禁用。
- `browser`：域名、Cookie、prompt injection、审批绑定和副作用审计。
- `swarm`：预算、租约、重复结果、冲突、合并、取消和性能消融。
- `linked-workspace`：同 owner、来源标记、写入目标和越权拒绝。
- `mobile`：OAuth、审批、取消、通知和敏感内容最小化。
- `security`：恶意扩展、供应链替换、跨 workspace、秘密泄露、权限膨胀和原始 CoT 外泄。

## 13. 明确不做

- 扩展自动获得全部 Worker 权限。
- 未评测的 Skill 自动进入 active。
- Skill 自进化后自动发布或提高权限。
- MCP Server 直接控制宿主 Shell、文件系统或浏览器。
- 无预算 Agent 树和未经冲突检查的多 Agent 合并。
- 跨 owner 关联工作区或共享私有记忆。
- 使用自行设计、未经审计的密码协议保护扩展流量。

## 14. 完成定义

V1.3 只有同时满足以下条件才完成：

1. MCP 和 Skills 通过统一 Registry、权限、Sandbox、审批和审计运行。
2. 凭据始终留在 Worker 本地，撤销和禁用能立即生效。
3. Skills 安装、评测、升级、回滚和卸载不会破坏基础 Agent 数据。
4. 扩展目录具备签名、版本、权限差异和安全撤回机制。
5. 浏览器外部副作用均有精确审批和审计收据。
6. Swarm 和关联工作区不能绕过 owner/workspace 边界，冲突可拒绝和回滚。
7. 移动端能够安全完成远程验收而不直接控制本地 Worker。
8. 扩展与遥测只能访问允许公开的推理产物，无法读取、请求或持久化原始 CoT。
9. 所有 V1.3 CI、安全评测和真实扩展制品验收通过。

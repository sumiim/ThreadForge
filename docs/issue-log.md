# ThreadForge 问题记录

本文记录开发、测试和部署中已经确认的问题。每个条目包含现象、根因、修复、验证和预防措施；不得记录密码、私钥、API Key、Token 或完整敏感日志。

## 2026-08-04：旧开发进程返回前端页面而非 API JSON

### 现象

- 页面样式未正常加载。
- 前端提示 `Unexpected token '<', "<!doctype "... is not valid JSON`。
- `GET /api/v1/config` 返回 `text/html`。

### 根因

本地 Vite 进程在代码合并前启动，仍使用旧的插件和代理配置。请求未被代理到 FastAPI，而是落入 Vite 的 SPA 回退页。

### 修复

停止旧 Vite 进程，在当前仓库的 `client/` 目录重新启动开发服务器。

### 验证

- `/api/v1/config` 返回 `application/json`。
- Tailwind 和 Ant Design 样式正常加载。
- 浏览器控制台无错误。

### 预防

切换分支或修改 `vite.config.ts`、插件、环境文件后必须重启开发服务器，并直接检查 API 响应的状态码和 `Content-Type`。

## 2026-08-04：占位模型配置导致任务显示 RuntimeError

### 现象

- 前端显示模型已经配置。
- 每个任务均在首次模型请求后快速失败，最终消息为 `agent run failed: RuntimeError`。

### 根因

本地验收 API 使用了占位模型 Key，因此配置接口只能判断 Key 非空，却无法保证凭据可用。前端连接的是该临时 API，而不是已经配置真实凭据的生产 API。

### 修复

停止临时本地 API，建立 `127.0.0.1:18000` 到生产服务器 `127.0.0.1:8000` 的 SSH 隧道，并让 Vite 代理到隧道端口。

### 验证

- 隧道的 `/health/live` 和 `/health/ready` 均成功。
- 经 `5173` 前端代理创建的真实测试任务进入 `completed` 状态。
- `stop_reason` 为 `final_answer_returned`。

### 预防

占位 Key 只能用于不发起模型请求的界面测试。端到端验收必须明确记录当前代理目标，并执行一个低风险真实任务。

## 2026-08-04：Vite 未读取 .env.local 中的代理目标

### 现象

`client/.env.local` 已配置 `VITE_API_PROXY_TARGET=http://127.0.0.1:18000`，但开发服务器仍请求默认的 `127.0.0.1:8000`，最终返回 `502 Bad Gateway`。

### 根因

`vite.config.ts` 直接读取 `process.env.VITE_API_PROXY_TARGET`。Vite 在求值配置文件时不会自动把 `.env.local` 注入 `process.env`，配置必须显式调用 `loadEnv`。

### 修复

新增 `resolveApiProxyTarget()`，通过 Vite `loadEnv` 读取环境文件，同时保留 `127.0.0.1:8000` 默认值。

### 验证

- 新增默认值和 `.env.local` 覆盖测试。
- TypeScript、ESLint、6 个前端测试和生产构建通过。
- 开发服务器经 `18000` 隧道成功访问生产 API。

### 预防

Vite 配置层读取 `.env*` 时统一使用 `loadEnv`，并为默认值和本地覆盖分别建立测试。

## 2026-08-04：PowerShell 调用 GitHub API 产生问号和整文件换行差异

### 现象

- PR 中文标题和说明显示为问号。
- `package.json` 在 PR 中显示为整文件删除后重新添加。

### 根因

- Windows PowerShell 5 将 JSON 字符串请求体按非 UTF-8 编码发送，中文在 GitHub API 接收前已丢失。
- 绕过 Git 推送、直接创建 GitHub Blob 时上传了工作区的 CRLF 字节，没有经过 Git 的文本换行规范化。

### 修复

- API 请求体先编码为 UTF-8 字节，再发送到 GitHub。
- 创建 Blob 前把文本规范化为 LF，并以 UTF-8 Base64 上传。

### 验证

- PR 标题和说明可正常显示中文。
- 修改文件只显示实际代码差异，不再出现无意义的整文件换行变化。

### 预防

优先使用标准 Git 推送。必须使用 Git Data API 时，显式控制 UTF-8 请求编码和 LF 换行，并在合并前检查 PR 文件统计。

## 2026-08-04：GitHub 默认解析地址不可达

### 现象

Git 推送反复出现连接超时或 `connection reset`，但 GitHub API 和部分官方入口仍可访问。

### 根因

当前网络把 `github.com` 解析到不可达的边缘地址，TCP 443 连接失败或在传输过程中被重置。

### 修复

- 应急阶段使用可访问的 GitHub API 完成等价的分支和提交创建，没有修改系统 DNS。
- 长期方案创建专用 Ed25519 密钥，在 SSH 配置中把 `github.com` 定向到 `ssh.github.com:443`，并将仓库远端切换为 `git@github.com:sumiim/ThreadForge.git`。

### 验证

- 远端分支和 PR 创建成功，GitHub Actions 正常启动。
- `ssh -T git@github.com` 返回 GitHub 账户认证成功。
- `git fetch origin main` 和 `git ls-remote` 均通过 SSH 成功。

### 预防

推送前先区分 DNS、TCP、认证和 Git 对象问题。网络异常时不得反复创建重复提交；采用替代通道后必须核对远端 SHA 和 PR 文件列表。当前环境统一使用 GitHub SSH 443，不再回退到仓库 HTTPS 远端。

## 2026-08-04：PowerShell 未向 ssh-keygen 传递空口令参数

### 现象

执行 `ssh-keygen ... -N ""` 时，`ssh-keygen` 报告 `option requires an argument -- N`，密钥文件没有创建。

### 根因

Windows PowerShell 5 在调用原生命令时丢弃了空字符串参数，`-N` 因而没有收到必需的空口令值。

### 修复

使用 PowerShell 停止解析标记 `--%`，让 `-N ""` 原样传递给 Windows OpenSSH。随后通过 GitHub 已登录页面添加公钥；现有仓库凭据没有账户 SSH Key 管理权限，不能使用 `/user/keys` API。

### 验证

- 密钥指纹可由 `ssh-keygen -lf` 正常读取。
- GitHub 账户识别该密钥并通过 SSH 认证。
- 私钥 ACL 仅允许当前用户、SYSTEM 和 Administrators 访问。

### 预防

在 Windows PowerShell 5 中调用需要空字符串参数的原生命令时，不依赖普通引号转义；使用 `--%` 或经过验证的参数数组，并在后续步骤前确认目标文件确实存在。

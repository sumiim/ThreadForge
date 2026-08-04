# ThreadForge 本地 Worker V1

## 1. 目标与边界

ThreadForge 使用 GitHub OAuth 识别用户。Web 与 Electron Desktop 使用同一套前端和中央 API；Session、Task、审批、事件及最终结果由服务器统一保存。本地 Worker 在用户电脑上运行完整 Pico Runtime、模型调用、文件工具、Git 与 Shell。

Worker 只主动建立出站 WebSocket 连接，不监听本地公网端口。服务器不会保存本机工作区的真实绝对路径，只保存设备 ID、逻辑工作区 ID、名称和 Git 标记。

```text
Web / Electron
      │ GitHub OAuth + REST/SSE
      ▼
中央 ThreadForge（唯一事实来源）
      │ 设备令牌 + WSS
      ▼
本地 Worker ── Pico / 模型 / 文件 / Git / Shell
```

V1 每台 Worker 同时只执行一个根任务。运行期间断线时，服务器将任务收敛为 `worker_disconnected`；Worker 重连后可以接收新任务，但不会恢复已中断任务。

## 2. 线上控制面前提

多用户不能依赖开发者的 SSH 隧道。生产环境需要一个域名和 HTTPS 反向代理，并把 Web、REST、SSE、OAuth callback 与 Worker WebSocket 放在同一站点。仓库提供 [Caddy 示例](../deploy/Caddyfile.example)。API 和 Web 容器仍只监听宿主机 `127.0.0.1`，公网仅开放 Caddy 的 443。

生产 CD 必须调用仓库内的 [`scripts/deploy-production.sh`](../scripts/deploy-production.sh)。该脚本会同时构建并启动 `api` 与 `web` 服务，并且只有在 API readiness、Web HTTP 200、经 Web 同源代理访问 API 成功以及两个容器均处于 running 状态时才返回成功。服务器上的受限 sudo/forced-command 脚本只应作为固定入口转交给此脚本，避免部署逻辑在服务器和仓库之间漂移。Web Nginx 同时代理 `/api/*` 与 `/health/*` 到 Compose 内的 `api:8000`，因此没有配置公网 Caddy 时，通过 SSH 转发 Web 端口也能使用完整控制台。

生产 `.env` 至少配置：

```dotenv
THREADFORGE_IDENTITY_MODE=github_oauth
THREADFORGE_WEB_ORIGIN=https://threadforge.example.com
THREADFORGE_GITHUB_OAUTH_CALLBACK_URL=https://threadforge.example.com/api/v1/auth/github/callback
THREADFORGE_GITHUB_OAUTH_RETURN_URL=https://threadforge.example.com/
THREADFORGE_AUTH_COOKIE_SECURE=true
THREADFORGE_TRUSTED_HOSTS=["threadforge.example.com","127.0.0.1","localhost"]
```

GitHub OAuth App 的 Authorization callback URL 必须与上面的 callback 完全一致。反向代理必须支持 WebSocket Upgrade，并保留 `Authorization` 请求头。

## 3. Windows 安装

安装脚本从当前仓库安装 Pico 和 Worker，并创建用户级命令，不需要管理员权限：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-worker.ps1
```

重新打开 PowerShell 后，先在已登录网页的“设置 → 本地 Worker”中生成一次性配对码，再执行：

```powershell
threadforge-worker pair `
  --server https://threadforge.example.com `
  --code XXXX-XXXX-XXXX-XXXX `
  --name "我的电脑"

threadforge-worker workspace add "D:\codes\my-project" --name "my-project"
threadforge-worker workspace list
```

模型配置属于本机 Worker，不上传到服务器。启动前在当前用户环境中配置兼容 OpenAI 的模型参数：

```powershell
$env:PICO_OPENAI_API_BASE = "https://api.openai.com/v1"
$env:PICO_OPENAI_API_KEY = "..."
$env:PICO_OPENAI_MODEL = "gpt-5.4"
threadforge-worker run
```

Worker 会在网络中断后指数退避重连。Windows 设备令牌使用当前用户的 DPAPI 加密后写入 `%LOCALAPPDATA%\ThreadForge\Worker\worker.json`；模型密钥不写入该文件。撤销设备后必须重新配对。

## 4. 使用流程

1. 用户通过 GitHub 登录 Web 或 Electron。
2. 用户安装 Worker、输入一次性配对码，并授权一个或多个本地目录。
3. Worker 上线后，本地工作区出现在“新建会话”列表中。
4. 用户创建 Session；服务器将其绑定到 `owner_id + device_id + workspace_id`。
5. Task 由服务器发给指定 Worker。危险工具需要在任一已登录前端审批，服务器再把精确决策转发给 Worker。
6. Worker 回传受限事件、会话补丁和最终结果；服务器持久化后由 Web 与 Desktop 共同读取。

前端不直接连接 Worker。这样页面关闭、刷新、换设备或同时打开 Web/Desktop 时，仍由服务器保证统一状态、审批归属和事件顺序。

## 5. Electron

Electron 开发模式继续使用 `pnpm dev:electron`。生产桌面壳若要使用 GitHub OAuth，应加载与 Web 相同的 HTTPS 站点：

```powershell
$env:THREADFORGE_WEB_URL = "https://threadforge.example.com/"
pnpm build:electron
```

桌面壳只允许 HTTPS 远端站点，或 HTTP loopback 开发地址。未配置 `THREADFORGE_WEB_URL` 时会加载内置静态页面；该模式适合本机开发，不是线上 GitHub OAuth 多用户的推荐方式。

## 6. 协议与安全约束

- `POST /api/v1/devices/pairing-codes`：登录用户创建 10 分钟、一次性、64 bit 配对码。
- `POST /api/v1/workers/pair`：Worker 使用配对码换取设备 ID 和只返回一次的设备令牌。
- `WS /api/v1/workers/connect`：Worker 通过 `Authorization: Bearer <device token>` 主动连接。
- `GET /api/v1/devices`：用户只能查看自己的设备。
- `DELETE /api/v1/devices/{device_id}`：撤销设备，并终止该设备上的活动任务。
- Task 创建、取消、审批和 Worker 回传都校验用户、设备、工作区和 Task 的归属。
- 配对码只保存在服务端内存；设备令牌在服务端只保存 SHA-256 digest。
- 审批 digest 基于脱敏前原始参数计算；持久化 preview 在脱敏后生成。
- Worker 公共事件使用字段白名单；绝对路径和 `..` 路径不会转发给前端。
- 模型请求使用服务端下发超时，且本地客户端关闭内部重试，使取消最坏等待不被多次重试放大。

## 7. 当前限制

- Windows Worker 目前通过脚本和 CLI 安装，尚无签名安装包、托盘程序或自动更新。
- Electron 尚不负责自动安装/升级 Worker；它与 Web 一样只使用中央 API。
- Worker 运行时必须保持命令进程存在；系统服务/开机自启将在后续版本实现。
- 服务器只同步 Agent 会话和受限事件，不提供任意本地文件上传通道。

# ThreadForge

ThreadForge 是一个面向本地代码仓库的 Web Coding Agent 工作台。中央控制面负责身份、设备、工作区归属、任务状态和审批；用户电脑上的本地 Worker 负责访问工作区、调用模型、执行文件/Git/Shell 工具，并保存会话正文。

当前 `main` 已包含 Worker `0.3.97`（协议版本 1）。项目提供 Web Console、Electron 桌面壳、FastAPI 控制面、Windows 自包含 Worker 安装器，以及 Docker Compose 一键部署能力。

## 已完成部分

- React + Vite + TypeScript Web Console，支持 Session、Task、Workspace、Provider、Approval、运行事件和制品查看。
- FastAPI 控制面，提供 REST API、SSE 任务事件、GitHub OAuth、设备配对、工作区和 Worker 管理。
- 本地 Worker Companion：主动建立出站 WebSocket；支持 Windows x86_64 自包含安装器、后台服务、`threadforge://` 协议和开机启动。
- 本地会话正文、模型配置和运行产物保存在 Worker；中央服务保存设备、工作区、会话索引、Task 状态和审批审计。
- Provider/模型能力协商与可用性展示，Provider 错误码和失败原因向前端透传；前端避免重复 Provider 检查。
- Planning、thinking、commentary 等运行阶段事件展示，thinking 单折叠 UI，以及刷新后的 Task 状态恢复。
- Task 流式事件持久化、Worker 安装包 Range 断点续传和下载重试。
- Worker 发行清单使用 Ed25519 签名，安装包同时校验签名、大小和 SHA-256。
- ShellProcess OS 原生资源限制基础和可配置的 `container_sandbox_enabled` 工作区标记；可选 Docker 沙箱后端通过 `sandbox-workers` 镜像接线。
- Linux/macOS 和 Windows 一键 Compose 部署脚本；公网模式使用 Caddy 提供 HTTPS、WebSocket、REST 和 SSE 同源入口。

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web | React 19、TypeScript、Vite、Ant Design、Tailwind CSS、React Markdown、Shiki |
| Desktop | Electron 43、electron-builder，共用 Web 前端 |
| API | Python 3.10+、FastAPI、Uvicorn、Pydantic Settings、AnyIO |
| 实时通信 | Worker WebSocket；浏览器任务事件使用 SSE |
| Agent Runtime | Pico Legacy Runtime（Prompt、Memory、Tool 执行和模型客户端） |
| 编排扩展 | LangGraph，封装在 `agent-orchestrator`，Web V1 主要使用 native Runtime |
| 本地 Worker | Python、websockets、pywin32（Windows）、PyInstaller、NSIS |
| 持久化 | 原子 JSON 文件和文件锁；Worker 使用用户目录下的 JSON、Session 和 Run 文件 |
| 部署 | Docker、Docker Compose、Nginx（Web 容器）、Caddy（公网入口） |

## 整体架构

```text
                         GitHub OAuth / REST / SSE
    Web Console  ───────────────────────────────────┐
    Electron     ───────────────────────────────────┤
                                                   ▼
                                      ThreadForge API Server
                              身份、设备、工作区、Task、Approval
                                      │              │
                                      │ REST/SSE     │ 出站 WSS
                                      │              ▼
                                      │       Local Worker Companion
                                      │       Pico / Model / Tools / Git / Shell
                                      │              │
                                      │              ▼
                                      │       本机工作区和本地历史
                                      ▼
                              JSON data directory / release store
```

### 请求和任务流

1. 用户通过 Web 或 Electron 登录并选择设备、工作区。
2. 前端通过 REST 创建 Session/Task；危险工具需要经过 Approval。
3. API Server 按 `device_id + workspace_id` 将 Task 路由给在线 Worker。
4. Worker 在本地运行 Agent Runtime，通过 WebSocket 回传受限事件和终态。
5. API Server 将任务快照和事件通过 SSE 推送给前端；刷新页面后可重新获取快照并重连事件流。

### 数据边界

- **中央服务**：用户身份、设备令牌摘要、设备/工作区元数据、Session 索引、Task 状态、审批审计。
- **本地 Worker**：会话消息正文、模型 API 地址/模型/API Key、运行记录、工具产物和工作区真实路径。
- 中央服务不会把本机工作区绝对路径作为授权凭证，也不会持久化本地 Session 的消息正文和 API Key。
- V1 是 HTTPS/WSS 链路上的受信控制面，不宣称应用层端到端加密；中央服务在转发当前请求时可以看到运行内容。

## 目录结构

```text
api-server/             FastAPI 控制面、REST/SSE 路由和 JSON 存储
client/                 React/Vite Web Console 与 Electron 壳
local-worker/            本地 Worker Companion、WebSocket 客户端和服务
pico-legacy-runtime/    Pico Agent Runtime、Prompt、Memory、Tool 执行
agent-orchestrator/     LangGraph 编排适配层
sandbox-workers/        可选 Docker 沙箱镜像和运行时
scripts/                Worker 构建、发行、部署和 CI 辅助脚本
deploy/                 生产部署模板和受限部署入口
docs/                   架构、Worker、OAuth、公网部署和设计文档
compose.yaml            本地/内网 Docker Compose
compose.public.yaml     公网 Caddy Compose profile
deploy.sh               Linux/macOS 一键部署入口
deploy.ps1              Windows PowerShell 一键部署入口
```

## 快速启动：Docker Compose

### 前置条件

- Docker Engine 或 Docker Desktop，且 `docker compose` 为 Compose v2。
- 首次使用模型功能时，配置兼容 OpenAI 的 API 地址、API Key 和模型。
- Windows 使用 PowerShell；Linux/macOS 使用 Bash。

### Linux/macOS

```bash
./deploy.sh up
./deploy.sh health
./deploy.sh ps
./deploy.sh logs
```

默认地址：Web Console `http://127.0.0.1:5173`，API Server `http://127.0.0.1:8000`。

### Windows

```powershell
.\deploy.ps1 up
.\deploy.ps1 health
.\deploy.ps1 ps
.\deploy.ps1 logs
```

停止或删除容器：`./deploy.sh stop`、`./deploy.sh down`（Windows 使用 `.\deploy.ps1 stop`、`.\deploy.ps1 down`）。

`up` 会先尝试构建 `threadforge-sandbox:latest`，再构建并启动 `api` 和 `web`。沙箱镜像构建失败不会阻止 OS 原生后端；启用 Docker 沙箱时必须确保 Docker Desktop/Engine 可用。

## 配置

部署脚本在根目录 `.env` 不存在时生成模板；已有 `.env` 不会被覆盖。常用配置：

```dotenv
PICO_OPENAI_API_BASE=https://api.openai.com/v1
PICO_OPENAI_API_KEY=replace-me
PICO_OPENAI_MODEL=gpt-5.4
THREADFORGE_IDENTITY_MODE=single_owner_instance
THREADFORGE_WEB_ORIGIN=http://127.0.0.1:5173
THREADFORGE_SANDBOX_ENABLED=false
THREADFORGE_SANDBOX_BACKEND=os
# THREADFORGE_SANDBOX_BACKEND=docker
```

生产环境应改用 GitHub OAuth，并将 `THREADFORGE_WEB_ORIGIN`、回调地址和可信 Host 设置为 HTTPS 公网主机名。不要把 API Key、OAuth Secret 或 Worker 签名私钥提交到 Git。

## 公网生产部署

公网部署要求一个解析到服务器的主机名。Caddy 负责自动 HTTPS，Web 容器负责将 `/api/*` 和 `/health/*` 代理到 API Server，并支持 Worker WebSocket Upgrade。

服务器 `.env` 至少配置：

```dotenv
THREADFORGE_PUBLIC_HOST=threadforge.example.com
THREADFORGE_WEB_ORIGIN=https://threadforge.example.com
THREADFORGE_TRUSTED_HOSTS=["threadforge.example.com","127.0.0.1","localhost"]
THREADFORGE_IDENTITY_MODE=github_oauth
THREADFORGE_GITHUB_OAUTH_CALLBACK_URL=https://threadforge.example.com/api/v1/auth/github/callback
THREADFORGE_GITHUB_OAUTH_RETURN_URL=https://threadforge.example.com/
THREADFORGE_AUTH_COOKIE_SECURE=true
```

生产部署入口：

```bash
sudo /usr/local/sbin/deploy-threadforge
curl -fsS https://threadforge.example.com/health/ready
docker compose --profile public -f compose.yaml -f compose.public.yaml ps
```

`scripts/deploy-production.sh` 会检查当前分支为 `main`、工作区无未提交修改，然后执行 `git pull --ff-only`、Compose 构建和 readiness 验收。检测到 `THREADFORGE_PUBLIC_HOST` 时会额外启动 `gateway`。公网只应开放 TCP 80/443 和 UDP 443，API 的 8000 及 Web 的 5173 保持 loopback 绑定。

完整 DNS、OAuth、证书和回退说明见 [`docs/public-web-deployment.md`](docs/public-web-deployment.md)。

## 本地 Worker

### Web 安装流程

1. 登录后，在“添加主机”或 Worker 设置中下载 Windows x86_64 安装器。
2. 运行 `threadforge-worker-windows-x86_64.exe`；安装器自带 Python、Pico 和全部依赖。
3. 回到页面点击“安装完成，连接本机”。页面生成短时一次性配对码并打开 `threadforge://worker/pair?...`。
4. Worker 后台服务完成配对并连接 API；之后可在设备卡片添加本地目录。

默认本地数据目录：

```text
%LOCALAPPDATA%\ThreadForge\Worker\
```

其中包含 `worker.json`、`sessions`、`runs`、`providers.json` 和更新状态。重装 Worker 时不要手动删除该目录，否则会丢失本机历史和配对信息。

### Worker CLI

```bash
python -m threadforge_worker status
python -m threadforge_worker pair --server https://threadforge.example.com --code XXXX-XXXX-XXXX-XXXX
python -m threadforge_worker workspace list
python -m threadforge_worker workspace add D:\path\to\repo --name my-repo
python -m threadforge_worker update --check
```

## Worker 发行

修改以下 Worker 相关路径时，必须同步递增版本：`local-worker/pyproject.toml`、`local-worker/src/`、`pico-legacy-runtime/pico/`、`agent-orchestrator/src/`、`scripts/build-worker-installer.ps1`、`scripts/worker-installer.nsi`。版本需同时更新 `project.version` 和 `threadforge_worker.__version__`。

构建 Windows 安装器：

```powershell
.\scripts\build-worker-installer.ps1 -Version 0.3.98
```

发行标签示例：

```bash
git tag -a worker-v0.3.98 -m "Worker release v0.3.98"
git push origin worker-v0.3.98
```

`worker-release.yml` 会校验版本、构建 wheel 和安装器、运行 smoke test、生成签名 manifest，并由 `scripts/publish-worker-release.sh` 发布到服务器私有 release store。Web 端只提供经过签名清单校验的稳定版下载。

## 开发与测试

### Web

```bash
cd client
pnpm install
pnpm dev
pnpm typecheck
pnpm test
pnpm lint
pnpm build
```

开发服务器默认将 `/api` 代理到 `http://127.0.0.1:8000`。需要远端 API 时设置 `VITE_API_PROXY_TARGET`；Electron 生产构建可设置 `THREADFORGE_WEB_URL` 加载同一 HTTPS Web 站点。

### API Server

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ./pico-legacy-runtime
python -m pip install -e ./agent-orchestrator
python -m pip install -e "./api-server[dev]"
python -m threadforge_api
python -m pytest api-server/tests
```

正式启动入口会使用数据目录进程锁，并固定 Uvicorn `workers=1`；开发热重载可使用 `python -m threadforge_api --reload`。

### Worker

```bash
python -m pip install -e ./pico-legacy-runtime
python -m pip install -e ./agent-orchestrator
python -m pip install -e "./local-worker[dev]"
python -m pytest local-worker/tests
```

提交前建议执行对应包的 typecheck/test/lint，并检查 Worker 版本门控和响应字段相关断言。

## 重要限制

- V1 每台 Worker 同时只执行一个根 Task；Worker 断线时活动任务会收敛为 `worker_disconnected`，不会自动恢复已中断任务。
- JSON 存储依赖单进程/单 Uvicorn worker，不支持多个 API 进程共享同一数据目录。
- SSE 事件主要在运行期内存中维护，V1 不承诺跨进程可靠重放。
- OS 原生后端和已批准 Shell 不是对抗性隔离；需要更强隔离时使用已配置并验证过的 Docker 沙箱后端。
- 公网入口、Caddy/Tailscale/代理对 API 请求和 Worker 大文件下载的稳定性会直接影响 Worker 清单、设备状态和安装包下载。

## 相关文档

- [`docs/local-worker-v1.md`](docs/local-worker-v1.md)：Worker、配对、数据边界和协议细节
- [`docs/public-web-deployment.md`](docs/public-web-deployment.md)：公网 HTTPS、DNS、OAuth 和 Caddy 部署
- [`docs/worker-release.md`](docs/worker-release.md)：Worker 签名发行与服务器发布
- [`docs/requirements-v1.md`](docs/requirements-v1.md)：V1 功能边界和 API 基线
- [`AGENTS.md`](AGENTS.md)：仓库 CI、版本门控和合并后的交付规程

## 许可证

第三方和运行时许可说明见 [`NOTICE.md`](NOTICE.md)。

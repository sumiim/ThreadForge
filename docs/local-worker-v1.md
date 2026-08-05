# ThreadForge 本地 Worker V1

## 1. 目标与边界

ThreadForge 使用 GitHub OAuth 识别用户。Web 与 Electron Desktop 使用同一套前端和中央 API；本地 Worker 在用户电脑上运行完整 Pico Runtime、模型调用、文件工具、Git 与 Shell，并保存会话正文、模型配置与运行产物。服务器只保存设备、工作区、会话索引、Task 状态和审批审计，不持久化本地会话的用户消息、模型回答或 API Key。

Worker 只主动建立出站 WebSocket 连接，不监听本地公网端口。服务器不会保存本机工作区的真实绝对路径，只保存设备 ID、逻辑工作区 ID、名称和 Git 标记。

V1 使用 HTTPS/WSS 保护网络链路，但尚未实现应用层端到端加密。中央服务在转发当下可以看到提示词、回答和模型配置明文，只是不把它们持久化或写入日志；因此不能宣称“服务器无法读取正文”。若未来需要零信任控制面，应使用经指纹核验的 Worker 公钥和成熟 sealed-box 实现，而不是自定义密码协议。

```text
Web / Electron
      │ GitHub OAuth + REST/SSE
      ▼
中央 ThreadForge（身份、路由与控制状态）
      │ 设备令牌 + WSS
      ▼
本地 Worker ── 历史 / .env / Pico / 模型 / 文件 / Git / Shell
```

V1 每台 Worker 同时只执行一个根任务。运行期间断线时，服务器将任务收敛为 `worker_disconnected`；Worker 重连后可以接收新任务，但不会恢复已中断任务。

## 2. 线上控制面前提

多用户不能依赖开发者的 SSH 隧道。生产环境需要一个公网主机名和 HTTPS 反向代理，并把 Web、REST、SSE、OAuth callback 与 Worker WebSocket 放在同一站点。仓库提供 [`Caddyfile`](../Caddyfile) 和 [`compose.public.yaml`](../compose.public.yaml)，具体配置见[公网 Web 部署](public-web-deployment.md)。API 和 Web 容器仍只监听宿主机 `127.0.0.1`，公网仅开放 Caddy 的 80/443。

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

首次进入工作台时，前端会检查当前账号是否有在线且协议兼容的 Companion。已绑定但离线时会先尝试唤醒；首次使用、唤醒超时或协议不兼容时，会显示带真实下载进度的安装窗口。下载包由中央服务校验签名清单和制品 SHA-256 后同源转发。

Windows x86_64 下载的是单个 `threadforge-worker-windows-x86_64.exe`。安装程序内含 Python、Pico、Worker 和全部运行依赖，不要求用户预装 Python、pip 或 PowerShell 模块，也不需要解压或管理员权限。用户只需运行一次安装程序；安装器注册当前用户登录自启动和 `threadforge://` 协议，并在后台启动 Companion。后台不显示常驻窗口，只有收到目录授权请求时才临时打开系统目录选择器。

安装后回到网页点击“安装完成，连接本机”。网页此时才创建短时一次性配对码，并打开 `threadforge://worker/pair?server=...&code=...`；安装器注册的协议处理器完成绑定并启动后台服务。配对码不会在下载期间提前生成，避免安装耗时导致过期。浏览器出于安全策略可能要求用户确认打开 ThreadForge Worker，这是唯一无法由网页静默跳过的交互。

此后可以直接在 Web 或 Electron 的设备卡片点击“添加本地目录”；中央服务向对应 Worker 发出一次性请求，本机用户确认目录后才会注册。CLI 的 `pair` 与 `workspace add/list` 只保留作远端设备接入、诊断和兼容入口。

模型配置属于本机 Worker。打开“设置 → 本地 Worker”，在在线设备卡片点击设置图标，填写兼容 OpenAI 的 API 地址、模型和 API Key。配置经当前已认证的 Worker WebSocket 一次性转发，在本机原子写入：

```text
%LOCALAPPDATA%\ThreadForge\Worker\.env
```

Worker 在启动时读取该 `.env`，在线修改后也立即更新当前进程，后续任务使用新配置。Windows 上 `.env` 的 ACL 仅允许当前用户、SYSTEM 和 Administrators 访问；设备令牌则使用当前用户的 DPAPI 加密后写入 `%LOCALAPPDATA%\ThreadForge\Worker\worker.json`。API Key 不写入设备配置、中央数据库、响应或日志。Worker 会在网络中断后指数退避重连，同一用户只允许一个服务实例；撤销设备后必须重新配对。

会话文件位于 `%LOCALAPPDATA%\ThreadForge\Worker\sessions`。历史读取完全独立于模型配置：更换 API 地址、API Key、供应商网关或模型后，可以直接打开和继续旧会话，不需要切回原供应商；只有新发出的消息使用当前 `.env` 配置。

## 4. 使用流程

1. 用户通过 GitHub 登录 Web 或 Electron。
2. 用户运行自包含安装器并在网页点击“连接本机”；一次性配对和后台启动自动完成。
3. 用户在 Web 或 Electron 选择目标设备并点击“添加本地目录”，该设备上的服务弹出原生目录选择器。
4. Worker 上线后，本地工作区出现在“新建会话”列表中。
5. 用户创建 Session；服务器保存不含正文的索引，Worker 在首次运行时创建本地会话文件。
6. Task 由服务器发给指定 Worker。危险工具需要在任一已登录前端审批，服务器再把精确决策转发给 Worker。
7. Worker 实时转发受限事件和最终结果供当前页面显示，但只在本机持久化会话正文；刷新页面时中央 API 向在线 Worker 按需读取历史。

前端不直接连接 Worker。这样页面关闭、刷新、换设备或同时打开 Web/Desktop 时，仍由服务器保证统一状态、审批归属和事件顺序。

中央 API 是身份、归属、任务状态和审批顺序的事实来源；本地 Worker 是会话正文、模型配置和本地运行产物的事实来源。Worker 离线时中央仍可显示会话索引与任务状态，但正文暂时不可读取。Worker 每次连接会分块同步不含正文的本地会话索引，因此重新配对到另一套 ThreadForge API 后，原有本地历史仍能重新出现在前端。

一个 GitHub 账号可以绑定多台 Worker，每台 Worker 可以授权多个工作区。设备握手上报系统、架构、版本与 capabilities；新建会话时用户选择的是具体设备下的具体工作区，后续 Task 固定路由到该 `device_id + workspace_id`，不会因另一台设备上线而漂移。浏览器只能通过自定义协议唤醒自己所在电脑的 Worker；远端设备需要在目标机器安装并使用网页生成的一次性配对码接入。

从早期预览版升级时，服务器可能已有本地 Worker Session/Task 的正文副本。服务器只有在新版 Worker 上报同一 `session_id`、证明本机副本存在后，才清除对应中央 history、memory、checkpoint、Task input 和 final answer；Worker 尚未上报的唯一副本不会被启动迁移直接删除。

`github_oauth` 多用户模式不展示也不接受服务器 `backend_process` 工作区，即使请求复用升级前遗留的服务器 Session 也会被拒绝。这样普通用户无法绕过 Worker 在部署仓库中执行命令，也不会产生新的服务器端会话正文。`single_owner_instance` 仅保留给本机开发和兼容旧版 Runtime。

## 5. Electron

Electron 开发模式继续使用 `pnpm dev:electron`。生产桌面壳若要使用 GitHub OAuth，应加载与 Web 相同的 HTTPS 站点：

```powershell
$env:THREADFORGE_WEB_URL = "https://threadforge.example.com/"
pnpm build:electron
```

桌面壳只允许 HTTPS 远端站点，或 HTTP loopback 开发地址。未配置 `THREADFORGE_WEB_URL` 时会加载内置静态页面；该模式适合本机开发，不是线上 GitHub OAuth 多用户的推荐方式。

Electron 不拥有目录选择、Worker 进程控制或工作区管理的私有 IPC。Web 与 Electron 都调用中央设备 API，由后台 Worker Service 执行本机操作，因此核心能力保持一致。桌面壳只保留窗口、系统外链和通知等展示层能力。

当前桌面安装包仍与 Worker 安装器分开版本化，但两种前端使用相同中央协议和同一个自包含 Worker。后续可以由 Electron 安装器链式安装同一制品，不能维护第二套 Worker Runtime。

## 6. 协议与安全约束

- `POST /api/v1/devices/pairing-codes`：登录用户创建 10 分钟、一次性、64 bit 配对码。
- `POST /api/v1/workers/pair`：Worker 使用配对码换取设备 ID 和只返回一次的设备令牌。
- `WS /api/v1/workers/connect`：Worker 通过 `Authorization: Bearer <device token>` 主动连接。
- `GET /api/v1/devices`：用户只能查看自己的设备。
- `GET /api/v1/worker/releases/latest`：返回经过 Ed25519 验证的稳定版清单；浏览器 Cookie 或有效设备令牌二选一认证。
- `GET /api/v1/worker/releases/download/{platform}`：只读取服务器私有发布目录中的签名制品，并在开始响应前校验清单签名、大小和 SHA-256；二进制不保存到 GitHub。
- `POST /api/v1/devices/{device_id}/workspace-selection-requests`：为属于当前用户、在线且声明 `workspace_selection` 能力的 Worker 创建两分钟一次性请求。
- `GET /api/v1/devices/{device_id}/workspace-selection-requests/{request_id}`：查询请求的 pending/completed/cancelled/failed/expired 状态。
- `PUT /api/v1/devices/{device_id}/model-config`：把模型参数转发给在线 Worker；API Key 不在中央持久化。
- `DELETE /api/v1/devices/{device_id}`：撤销设备，并终止该设备上的活动任务。
- Task 创建、取消、审批和 Worker 回传都校验用户、设备、工作区和 Task 的归属。
- 配对码只保存在服务端内存；设备令牌在服务端只保存 SHA-256 digest。
- 审批 digest 基于脱敏前原始参数计算；持久化 preview 在脱敏后生成。
- Worker 公共事件使用字段白名单；绝对路径和 `..` 路径不会转发给前端。
- 目录选择器在用户会话内按需创建；服务器只收到逻辑工作区 ID、名称和 Git 标记，不接收本机绝对路径。
- `threadforge://worker/start` 只能启动无参数后台服务；`threadforge://worker/pair` 只接受 HTTPS/loopback 服务地址、一次性配对码和可选设备名。协议处理器拒绝重复/未知参数、目录、命令、长期令牌和未知动作。
- Worker 通过 `sessions.updated` 分块同步会话 ID、标题、工作区和消息数，正文不进入该消息；`session.history.get/result` 只在用户打开会话时经当前连接临时传输。
- Worker 握手上报语义版本、协议版本和 capabilities；前端只把在线且协议兼容的设备视为可用。Companion 上线后在后台检查稳定版，空闲时安装经 Ed25519 签名和 SHA-256 绑定的更新并重启。
- 本地 Worker Task 的持久化控制记录不包含用户输入和最终回答，也不在服务器创建 Run report/trace；实时 SSE 和为终态竞态保留的答案缓存仅存在内存中，答案缓存最多保留 10 分钟和 512 项。
- 模型请求使用服务端下发超时，且本地客户端关闭内部重试，使取消最坏等待不被多次重试放大。

## 7. 当前限制

- Windows Worker 已使用 PyInstaller + NSIS 生成自包含、每用户安装器，发布清单已签名；安装器尚未购买 Authenticode 代码签名证书，Windows 仍可能显示未知发布者提示。
- 稳定版自包含安装器目前只覆盖 Windows x86_64。Linux/macOS Worker 的协议与多设备路由可用，但面向普通用户的对应平台安装器、目标系统冒烟测试及 macOS 签名公证尚未完成。
- 浏览器不允许网页静默执行下载的程序；首次安装仍需用户主动运行安装器，并可能确认一次自定义协议唤醒提示。
- Worker 是用户会话内的后台进程而不是 Windows LocalSystem 服务；系统服务运行在 Session 0，无法安全地向当前桌面显示目录选择器。
- 当前只支持 OpenAI-compatible 模型接口；增加原生 Anthropic 等供应商需要扩展本地模型客户端和配置契约。
- 服务器只同步会话索引和受限运行事件，不提供任意本地文件上传通道。

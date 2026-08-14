# ThreadForge 前端 API 与 SSH 接入手册

本文用于把 ThreadForge V1 后端交给前端开发者联调。SSH 密钥只用于建立开发期端口隧道，不能放进前端代码、浏览器、构建产物或 Git。

> V1 后端是单用户系统，没有登录鉴权和独立执行沙盒。服务器 API 只监听 `127.0.0.1:8000`，不得直接暴露公网。浏览器通过前端开发者本机的 SSH 隧道访问后端。

## 1. SSH 密钥与访问开通

### 1.1 双方职责

前端开发者需要：

1. 在自己的电脑生成一对专用 SSH 密钥。
2. 妥善保管私钥，只把 `.pub` 公钥和 SHA256 指纹发给服务器所有者。
3. 收到服务器地址、SSH 端口、专用用户名和服务器 host key 指纹后，核对并建立隧道。
4. 启动前端时把 API 基地址配置为 `http://127.0.0.1:18000`。

服务器所有者需要：

1. 不向前端开发者提供 root 密码、root 私钥或模型 API Key。
2. 为前端开发者创建独立的 SSH 隧道账户。
3. 把对方公钥安装到该账户，并限制它只能转发到服务器的 `127.0.0.1:8000`。
4. 把服务器地址、SSH 端口、专用用户名和服务器 host key 指纹发给前端开发者。
5. 需要撤销访问时删除对应的 `authorized_keys` 行，而不是影响其他人的密钥。

如果前端和服务器均由同一个人管理，也应按这两个角色分别操作，不要复用 root 登录密钥。

### 1.2 前端开发者：创建专用密钥

在前端开发电脑的 PowerShell 执行：

```powershell
$KeyPath = "$env:USERPROFILE\.ssh\threadforge_frontend_ed25519"
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keygen -t ed25519 -a 100 -C "threadforge-frontend" -f $KeyPath
```

建议设置 passphrase。命令生成：

- `threadforge_frontend_ed25519`：私钥，只能留在前端开发者电脑。
- `threadforge_frontend_ed25519.pub`：公钥，发送给服务器所有者。

查看需要发送的公钥和指纹：

```powershell
Get-Content "$KeyPath.pub"
ssh-keygen -lf "$KeyPath.pub" -E sha256
```

发送给服务器所有者的内容只有：

```text
公钥：ssh-ed25519 AAAA... threadforge-frontend
指纹：SHA256:...
```

严禁发送无 `.pub` 后缀的私钥。私钥也不能写入 `.env`、前端源码、CI Secret、聊天记录或网盘。

### 1.3 服务器所有者：创建受限隧道账户

先通过现有管理员通道登录 Debian 服务器。以下命令由服务器所有者执行：

```bash
sudo useradd --create-home --shell /bin/bash threadforge-tunnel
sudo passwd --lock threadforge-tunnel
sudo install -d -m 700 -o threadforge-tunnel -g threadforge-tunnel \
  /home/threadforge-tunnel/.ssh
sudo install -m 600 -o threadforge-tunnel -g threadforge-tunnel \
  /dev/null /home/threadforge-tunnel/.ssh/authorized_keys
```

如果账户已存在，不要重复创建；检查其 home、组和 `.ssh` 权限即可。

先核对前端开发者提供的公钥指纹。把公钥保存到临时文件后可执行：

```bash
ssh-keygen -lf /path/to/frontend-public-key.pub -E sha256
```

确认一致后，在 `/home/threadforge-tunnel/.ssh/authorized_keys` 中添加一整行。必须在公钥前添加限制选项：

```text
restrict,port-forwarding,permitopen="127.0.0.1:8000",command="/usr/sbin/nologin" ssh-ed25519 AAAA... threadforge-frontend
```

可以使用 `sudoedit` 编辑，完成后修正权限：

```bash
sudo chown threadforge-tunnel:threadforge-tunnel \
  /home/threadforge-tunnel/.ssh/authorized_keys
sudo chmod 600 /home/threadforge-tunnel/.ssh/authorized_keys
```

这些 key options 禁止 PTY、agent forwarding、X11 forwarding 等能力，只重新允许 TCP 端口转发，将目标限制为 `127.0.0.1:8000`，并拒绝该密钥发起的远程命令或 Shell。`ssh -N` 不申请远程命令通道，因此仍能建立隧道。不要把前端公钥放进 `/root/.ssh/authorized_keys`。

服务器所有者取得服务器 Ed25519 host key 指纹：

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

通过可信渠道把以下四项发给前端开发者：

```text
SERVER_IP=<服务器地址>
SSH_PORT=<SSH 端口>
SSH_USER=threadforge-tunnel
HOST_KEY_SHA256=<上一条命令输出的 SHA256 指纹>
```

### 1.4 前端开发者：核对并测试隧道

前端开发者在新 PowerShell 窗口执行，先把占位符换成服务器所有者提供的值：

```powershell
$KeyPath = "$env:USERPROFILE\.ssh\threadforge_frontend_ed25519"
ssh -vv -N `
    -i $KeyPath `
    -o IdentitiesOnly=yes `
    -o ExitOnForwardFailure=yes `
    -L 18000:127.0.0.1:8000 `
    -p <SSH_PORT> `
    threadforge-tunnel@<SERVER_IP>
```

首次连接显示 host key 指纹时，必须与服务器所有者提供的 `HOST_KEY_SHA256` 一致。指纹不一致就停止连接并联系服务器所有者，不能直接接受。

`-N` 表示只建立隧道，不打开远程 Shell。连接成功后窗口持续占用且没有新输出是正常现象。保持窗口运行，在另一个 PowerShell 窗口验证：

```powershell
Invoke-RestMethod "http://127.0.0.1:18000/health/live"
Invoke-RestMethod "http://127.0.0.1:18000/health/ready"
```

两者分别应返回：

```json
{"status":"ok"}
```

```json
{"status":"ready"}
```

如果本机 `18000` 已被占用，只修改 `-L` 左侧端口，例如 `18001:127.0.0.1:8000`，并同步修改前端 API 基地址。不能修改右侧的服务器目标地址和端口。

### 1.5 前端开发者：配置 SSH 别名

在前端开发电脑的 `$env:USERPROFILE\.ssh\config` 中添加：

```sshconfig
Host threadforge-api-tunnel
    HostName <SERVER_IP>
    User threadforge-tunnel
    Port <SSH_PORT>
    IdentityFile C:/Users/<WINDOWS_USER>/.ssh/threadforge_frontend_ed25519
    IdentitiesOnly yes
    ExitOnForwardFailure yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    LocalForward 18000 127.0.0.1:8000
```

以后只需运行：

```powershell
ssh -N threadforge-api-tunnel
```

这类受限账号只用于隧道，不用于 VS Code Remote - SSH。前端代码在本机开发即可。

### 1.6 服务器所有者：撤销访问

删除 `/home/threadforge-tunnel/.ssh/authorized_keys` 中对应公钥的完整一行即可撤销该开发者的新连接。不要删除其他人的公钥。

如需立即终止该账户的现有 SSH 连接，在确认目标用户名后执行：

```bash
sudo pkill -u threadforge-tunnel
```

如果多人接入，推荐每人使用独立隧道账户；否则上述命令会同时断开同一账户的所有连接。

## 2. 前端接入方式

### 2.1 API 基地址与 CORS

隧道运行时，前端统一使用：

```text
http://127.0.0.1:18000
```

Vite 项目可以在仅本地使用的 `.env.local` 中配置：

```dotenv
VITE_THREADFORGE_API_BASE_URL=http://127.0.0.1:18000
```

代码中读取：

```ts
const apiBaseUrl = import.meta.env.VITE_THREADFORGE_API_BASE_URL;
```

SSH 私钥与模型 API Key 都不能写入前端环境变量。`VITE_*` 会进入浏览器构建产物，不是秘密存储位置。

服务器当前允许的前端 Origin 是 `http://127.0.0.1:5173`。前端开发服务器应明确监听/打开该地址：

```powershell
npm run dev -- --host 127.0.0.1 --port 5173
```

`http://localhost:5173` 与 `http://127.0.0.1:5173` 是不同 Origin。若使用其他地址，服务器所有者必须明确调整 `THREADFORGE_WEB_ORIGIN` 并重启后端，不能用允许所有 Origin 代替。

### 2.2 前端建议的数据流

1. `GET /api/v1/workspaces`，让用户选择可用 Workspace。
2. `POST /api/v1/sessions` 创建会话，保存返回的 `session_id`。
3. `POST /api/v1/tasks` 创建异步任务，保存 `task_id`、`run_id` 和 `events_url`。
4. 立即连接 `events_url` 的 SSE；首帧 `task.snapshot` 用于恢复当前页面状态。
5. 收到 `waiting_for_approval` 时显示工具名和已脱敏参数，由用户批准或拒绝。
6. 收到 `completed`、`failed` 或 `cancelled` 后关闭 SSE，并重新获取 Task/Session 快照。
7. 页面刷新后通过 Session 详情中的 Task 摘要恢复列表，再查询选中 Task 的完整快照。

V1 同一时刻只允许一个活动根 Task。前端收到 `409 active_task_exists` 时应显示当前活动 Task，而不是自动重试创建。

## 3. API 契约

### 3.1 接口总览

| 方法 | 路径 | 成功状态 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/health/live` | `200` | 进程存活检查 |
| `GET` | `/health/ready` | `200` | 持久化、Workspace 和 Runner 就绪检查 |
| `GET` | `/api/v1/workspaces` | `200` | 查询允许使用的 Workspace |
| `POST` | `/api/v1/sessions` | `201` | 创建 Session |
| `GET` | `/api/v1/sessions?limit=50&offset=0` | `200` | 分页查询 Session |
| `GET` | `/api/v1/sessions/{session_id}?message_limit=100` | `200` | 查询 Session、消息和 Task 摘要 |
| `POST` | `/api/v1/tasks` | `202` | 创建异步 Task |
| `GET` | `/api/v1/tasks/{task_id}` | `200` | 查询 Task 快照 |
| `GET` | `/api/v1/tasks/{task_id}/events` | `200` | 订阅 SSE 事件流 |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | `200/202` | 请求取消 Task |
| `POST` | `/api/v1/tasks/{task_id}/approvals/{approval_id}` | `200` | 批准或拒绝工具调用 |
| `GET` | `/api/v1/runs/{run_id}/artifacts` | `200` | 查询 Run artifacts |
| `GET` | `/api/v1/runs/{run_id}/artifacts/{name}` | `200` | 读取 `task_state`、`trace` 或 `report` |

Task 状态：

```text
queued | running | waiting_for_approval | cancel_requested |
cancelled | completed | failed
```

终态是 `cancelled`、`completed` 和 `failed`。

### 3.2 Workspace 与 Session

查询 Workspace：

```http
GET /api/v1/workspaces
```

```json
{
  "items": [
    {
      "workspace_id": "threadforge",
      "name": "ThreadForge",
      "display_path": "/workspace/threadforge",
      "available": true,
      "is_git": true,
      "execution_environment": "backend_process",
      "container_sandbox_enabled": false
    }
  ]
}
```

创建 Session：

```http
POST /api/v1/sessions
Content-Type: application/json
```

```json
{
  "workspace_id": "threadforge",
  "title": "前端联调"
}
```

`workspace_id` 必填，最大 128 字符；`title` 可选，最大 200 字符。

### 3.3 创建与查询 Task

```http
POST /api/v1/tasks
Content-Type: application/json
```

```json
{
  "session_id": "ses_xxx",
  "input": "分析当前项目结构",
  "max_steps": 6
}
```

`input` 长度为 1 到 100000，不能只包含空白；`max_steps` 可选，范围 1 到 25。

成功返回 `202`：

```json
{
  "task_id": "task_xxx",
  "run_id": "run_xxx",
  "session_id": "ses_xxx",
  "status": "queued",
  "events_url": "/api/v1/tasks/task_xxx/events"
}
```

查询 Task：

```http
GET /api/v1/tasks/task_xxx
```

快照包含 `status`、`final_answer`、`stop_reason`、`attempts`、`tool_steps` 和 `pending_approval`。前端必须允许这些可空字段为 `null`。

### 3.4 SSE

浏览器连接：

```ts
const source = new EventSource(`${apiBaseUrl}${task.events_url}`);

source.addEventListener("task.snapshot", (event) => {
  const envelope = JSON.parse((event as MessageEvent).data);
  renderTask(envelope.data);
});

for (const eventType of ["task.completed", "task.failed", "task.cancelled"]) {
  source.addEventListener(eventType, (event) => {
    const envelope = JSON.parse((event as MessageEvent).data);
    renderTerminal(envelope);
    source.close();
  });
}

source.onerror = () => {
  // EventSource 会自动重连；同时用 GET task 快照校准页面状态。
};
```

SSE 首帧固定为 `task.snapshot`。事件 envelope：

```json
{
  "event_id": "evt_xxx",
  "sequence": 1,
  "type": "task.running",
  "task_id": "task_xxx",
  "run_id": "run_xxx",
  "timestamp": "2026-08-03T00:00:00Z",
  "data": {}
}
```

连接空闲时服务端发送 `: ping` heartbeat，浏览器 `EventSource` 会自动忽略。断开 SSE 不等于取消 Task。

### 3.5 审批与取消

当 Task 为 `waiting_for_approval` 时，从 `pending_approval.approval_id` 取得 ID：

```http
POST /api/v1/tasks/task_xxx/approvals/apr_xxx
Content-Type: application/json
```

```json
{"decision":"approved"}
```

也可以发送 `{"decision":"rejected"}`。重复、过期、陈旧或不属于该 Task 的审批会返回 `404` 或 `409`，前端应重新获取 Task 快照。

取消：

```http
POST /api/v1/tasks/task_xxx/cancel
```

`202` 表示取消仍在收敛；继续接收 SSE 或查询快照。`200` 表示请求返回时 Task 已是终态。

### 3.6 Artifact

```http
GET /api/v1/runs/run_xxx/artifacts
GET /api/v1/runs/run_xxx/artifacts/report
GET /api/v1/runs/run_xxx/artifacts/task_state
GET /api/v1/runs/run_xxx/artifacts/trace
```

`trace` 的媒体类型为 `application/x-ndjson`，每行是独立 JSON 对象，不能用普通单对象 `response.json()` 解析。其他 artifact 返回 JSON。

### 3.7 错误处理

统一错误响应：

```json
{
  "error": {
    "code": "task_not_found",
    "message": "...",
    "details": {},
    "request_id": "..."
  }
}
```

前端至少需要专门处理：

| HTTP 状态 | 常见 code | 前端行为 |
| --- | --- | --- |
| `404` | `session_not_found`、`task_not_found` | 返回列表并刷新本地状态 |
| `409` | `active_task_exists` | 跳转或提示当前活动 Task |
| `409` | `approval_stale`、`approval_expired` | 重新获取 Task 快照 |
| `422` | `validation_error`、`input_too_long` | 在输入控件旁显示错误 |
| `503` | `not_ready`、`model_not_configured`、`task_runner_unavailable` | 禁止继续提交并显示服务状态 |

报告故障时提供响应体中的 `request_id` 或响应头 `X-Request-ID`，不要记录 SSH 私钥、模型 Key 或完整敏感输入。

## 4. 前端联调验收

交付前至少验证：

- 未开启隧道时，公网地址无法直接访问 API。
- 开启隧道后，`live`、`ready` 和 Workspace 查询成功。
- 能创建 Session 和 Task，并通过 SSE 到达终态。
- 刷新页面后能从 Session/Task API 恢复状态。
- 能正确显示审批，并能批准、拒绝和取消。
- 同时提交第二个根 Task 时能处理 `409 active_task_exists`。
- SSH 私钥、服务器 root 凭据和模型 API Key 均未进入前端源码、浏览器存储或构建产物。

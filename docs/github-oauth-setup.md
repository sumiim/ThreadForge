# ThreadForge GitHub OAuth 配置

## 安全边界

GitHub OAuth 只负责确认用户身份。ThreadForge 使用服务端白名单决定谁能登录，并用 `owner_id` 隔离 Session、Task、Approval、SSE 和运行产物。GitHub Access Token 只用于登录期间读取当前用户资料，不写入磁盘。

当前版本仍是单进程、单 Worker、全局单活动任务。多个用户可以拥有独立数据，但不能同时执行多个根任务；API 仍只监听服务器 `127.0.0.1:8000`，前端继续通过 SSH 隧道访问。

## 创建 GitHub OAuth App

在 GitHub 打开 **Settings -> Developer settings -> OAuth Apps -> New OAuth App**，填写：

| 字段 | 值 |
|---|---|
| Application name | `ThreadForge` |
| Homepage URL | `http://127.0.0.1:5173` |
| Authorization callback URL | `http://127.0.0.1:18000/api/v1/auth/github/callback` |

创建后生成一个 Client Secret。Client ID 可以公开，但 Client Secret 只能写入服务器 `.env`，不得写入前端、Git、GitHub Actions 日志或聊天记录。

## 配置服务器

编辑服务器 `/root/ThreadForge/.env`：

```dotenv
THREADFORGE_IDENTITY_MODE=github_oauth
THREADFORGE_GITHUB_OAUTH_CLIENT_ID=替换为_Client_ID
THREADFORGE_GITHUB_OAUTH_CLIENT_SECRET=替换为_Client_Secret
THREADFORGE_GITHUB_OAUTH_CALLBACK_URL=http://127.0.0.1:18000/api/v1/auth/github/callback
THREADFORGE_GITHUB_OAUTH_RETURN_URL=http://127.0.0.1:5173/
THREADFORGE_GITHUB_OWNER_LOGIN=sumiim
THREADFORGE_GITHUB_ALLOWED_LOGINS='["sumiim"]'
THREADFORGE_AUTH_SESSION_TTL_SECONDS=604800
THREADFORGE_AUTH_COOKIE_SECURE=false
```

`THREADFORGE_GITHUB_OWNER_LOGIN` 必须是现有数据所有者的 GitHub 登录名。该用户会继承升级前的 Session、Task 和运行产物。增加用户时修改 JSON 数组，例如：

```dotenv
THREADFORGE_GITHUB_ALLOWED_LOGINS='["sumiim","another-developer"]'
```

登录名不区分大小写。删除某个登录名后，该用户的现有 Cookie 会在下一次请求时失效。`THREADFORGE_GITHUB_OWNER_LOGIN` 首次成功登录后会与现有实例所有者绑定，不得改成另一个 GitHub 账户；不要删除 `instance-owner.json` 或 `users` 目录来绕过绑定。

## 重建与验证

在服务器执行现有受控部署流程，或者由下一次主分支 CD 自动重建。配置完成后验证：

```bash
curl -sS http://127.0.0.1:8000/health/live
curl -sS http://127.0.0.1:8000/health/ready
curl -sS http://127.0.0.1:8000/api/v1/auth/status
```

认证状态应显示：

```json
{
  "identity_mode": "github_oauth",
  "multi_user_enabled": true,
  "authentication_required": true,
  "authenticated": false,
  "user": null
}
```

本地启动 SSH 隧道和前端后，打开 `http://127.0.0.1:5173`，页面应显示 GitHub 登录按钮。完成登录后，`/api/v1/auth/status` 的 `authenticated` 应为 `true`，且只返回当前用户自己的会话。

## 回退

认证配置异常时，将服务器 `.env` 中的模式改回：

```dotenv
THREADFORGE_IDENTITY_MODE=single_owner_instance
```

重新部署后会恢复原来的单所有者行为。用户映射和认证会话文件会保留，但不会参与请求身份解析。

# ThreadForge 公网 Web 部署

## 是否必须购买域名

技术上可以直接用公网 IP 提供 HTTP，但 ThreadForge 的 GitHub OAuth 多用户模式要求远程地址使用 HTTPS，浏览器 Cookie 和 Worker WebSocket 也应使用 HTTPS/WSS。Let’s Encrypt 已提供短期 IP 地址证书，不过 Caddy 的默认自动 HTTPS 路径仍可能为 IP 使用只在本地受信任的 CA；本项目的生产模板因此明确使用一个解析到服务器的主机名。

不一定要购买域名：可以使用自己已有域名的子域名，或者可信的免费 DNS 子域名。长期公开使用建议购买并持有自己的域名，便于控制 OAuth 回调、证书和迁移。裸 IP 和临时隧道只适合验收。

## DNS 与防火墙

1. 为主机名创建 `A` 记录，指向服务器公网 IPv4，例如 `threadforge.example.com -> 203.0.113.10`（文档保留地址）。
2. 在云平台安全组和服务器防火墙中仅开放 TCP `80`、TCP `443`、UDP `443`；SSH 继续使用现有受限端口。
3. 等待 DNS 生效，确认 `nslookup threadforge.example.com` 返回该服务器 IP。

API 的 `8000` 和 Web 容器的 `5173` 仍只绑定 `127.0.0.1`。公网请求只能通过 Caddy 进入，不能直接开放这两个端口。

## 服务器 `.env`

以下示例假设公网地址为 `https://threadforge.example.com`：

```dotenv
THREADFORGE_PUBLIC_HOST=threadforge.example.com
THREADFORGE_WEB_ORIGIN=https://threadforge.example.com
THREADFORGE_TRUSTED_HOSTS='["threadforge.example.com","127.0.0.1","localhost"]'
THREADFORGE_GITHUB_OAUTH_CALLBACK_URL=https://threadforge.example.com/api/v1/auth/github/callback
THREADFORGE_GITHUB_OAUTH_RETURN_URL=https://threadforge.example.com/
THREADFORGE_AUTH_COOKIE_SECURE=true
THREADFORGE_GITHUB_ACCESS_POLICY=all_authenticated
```

`all_authenticated` 允许任意完成 GitHub OAuth 的用户进入，但设备、工作区、会话、任务与审批仍按内部 `owner_id` 隔离。只面向受邀用户时保留默认的 `allowlist`，并在 `THREADFORGE_GITHUB_ALLOWED_LOGINS` 中列出账号；不要把空 allowlist 误认为开放注册。

同时把 GitHub OAuth App 配置改为：

| 字段 | 值 |
|---|---|
| Homepage URL | `https://threadforge.example.com` |
| Authorization callback URL | `https://threadforge.example.com/api/v1/auth/github/callback` |

GitHub OAuth App 只有一个 Authorization callback URL。保留 loopback 开发登录时，应另建一个开发用 OAuth App，不要在生产地址和 `127.0.0.1` 之间反复改同一个 Client ID。

## 部署与验收

`scripts/deploy-production.sh` 检测到 `THREADFORGE_PUBLIC_HOST` 后，会自动组合 `compose.yaml` 与 `compose.public.yaml`，启动 `api`、`web` 和 `gateway`。Caddy 负责申请和续期证书。

```bash
sudo /usr/local/sbin/deploy-threadforge
curl -fsS https://threadforge.example.com/health/ready
curl -I https://threadforge.example.com/api/v1/auth/github/start
docker compose --profile public -f compose.yaml -f compose.public.yaml ps
```

验收时还应在公网电脑完成 GitHub 登录，并确认浏览器开发者工具中的 Worker WebSocket 使用 `wss://`。

## 回退

从 `.env` 删除 `THREADFORGE_PUBLIC_HOST` 后重新部署，脚本会回到仅启动 `api` 与 `web` 的 loopback 模式。Caddy 数据卷保留，重新启用时可继续使用原证书状态。

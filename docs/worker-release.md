# Worker 签名发布与自动更新

## 信任链

Worker 发布使用独立 Ed25519 密钥：私钥只保存在 GitHub Actions Secret `WORKER_RELEASE_SIGNING_KEY_B64`，公钥固定在 API 与 Worker 代码中。发布清单签名后绑定版本、协议版本、平台、下载 URL、文件名、大小和 SHA-256。

下载流程会执行两次验证：中央 API 验证清单签名并完整下载、校验制品后才向浏览器或 Worker 返回；Worker 自动更新时再次验证清单签名、文件大小和 SHA-256。ZIP 解压还限制成员路径、数量、总大小和文件类型。

这套签名保护 ThreadForge 自动更新信任链，但不等于 Windows Authenticode。当前 PowerShell 安装脚本仍可能显示未知发布者；面向非开发者正式发行前，还应使用代码签名证书签署 MSIX/NSIS 安装器。

## 一次性 Secret 配置

当前公钥已经进入代码。对应私钥必须作为仓库 Actions Secret 保存，不能提交文件、粘贴到 Issue/PR 或输出到 Actions 日志。

1. 打开仓库 `Settings -> Secrets and variables -> Actions`。
2. 新建 Repository secret，名称为 `WORKER_RELEASE_SIGNING_KEY_B64`。
3. 值填写 Ed25519 PEM 私钥文件的 Base64 单行内容。
4. 保存后删除普通临时副本，只保留受 ACL 保护的离线备份。

如果 Secret 丢失，不能重新生成私钥后直接沿用当前公钥。密钥轮换必须先发布一个同时信任旧、新公钥的过渡版本，再用旧私钥签署该版本；确认客户端升级后才能改用新私钥。

## 发布稳定版

1. 同时更新 `local-worker/pyproject.toml` 和 `threadforge_worker.__version__`。
2. 通过主分支 CI，并确认版本对应代码已经合并到 `main`。
3. 在该提交创建严格匹配版本的标签：

```bash
git tag worker-v0.2.0
git push origin worker-v0.2.0
```

`Worker Release` 工作流会重新执行 Worker Lint/测试，构建 Pico 与 Worker wheels，解析 Windows Python 3.12 的第三方 wheels，生成可离线安装的 ZIP，签署 `worker-manifest.json`，最后创建 GitHub Release。

发布后验证：

```bash
curl -fsS https://github.com/sumiim/ThreadForge/releases/latest/download/worker-manifest.json
curl -fsS https://threadforge.example.com/api/v1/worker/releases/latest
```

第二个接口需要已登录浏览器 Cookie或有效 Worker 设备令牌。现有 Companion 在连接成功后空闲检查更新；发现更高版本时验证并安装本地 wheels，然后自动重启。协议不兼容的旧 Worker 会在前端被标记为必须更新。

# Worker 签名发布与自动更新

## 信任链

Worker 发布使用独立 Ed25519 密钥：私钥只保存在 GitHub Actions Secret `WORKER_RELEASE_SIGNING_KEY_B64`，公钥固定在 API 与 Worker 代码中。发布清单签名后绑定版本、协议版本、平台、下载 URL、文件名、大小和 SHA-256。

下载流程会执行两次验证：中央 API 验证清单签名并完整下载、校验制品后才向浏览器或 Worker 返回；Worker 自动更新时再次验证清单签名、固定文件名、文件大小和 SHA-256，再静默启动用户级安装器。

这套签名保护 ThreadForge 应用内下载和自动更新信任链，但不等于 Windows Authenticode。当前 NSIS 安装器仍可能显示未知发布者；面向非开发者正式发行前，还应使用代码签名证书签署安装器和内含的 Worker EXE。

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
git tag worker-v0.2.1
git push origin worker-v0.2.1
```

`Worker Release` 工作流在 Windows Runner 上重新执行 Worker Lint/测试，使用 PyInstaller 封装 Python 与全部运行依赖，再用 NSIS 生成每用户安装器。流水线会静默安装最终 EXE、实际执行 `--help` 和 `status`，通过后才签署 `worker-manifest.json` 并创建 GitHub Release。这条目标系统冒烟测试是发布门禁，不能用“依赖下载成功”代替。

发布后验证：

```bash
curl -fsS https://github.com/sumiim/ThreadForge/releases/latest/download/worker-manifest.json
curl -fsS https://threadforge.example.com/api/v1/worker/releases/latest
```

第二个接口需要已登录浏览器 Cookie 或有效 Worker 设备令牌。现有 Companion 在连接成功后空闲检查更新；发现更高版本时验证并启动静默安装，新安装器停止旧进程、替换程序并启动新服务。协议不兼容的旧 Worker 会在前端被标记为必须更新。

当前稳定清单只发布 `windows-x86_64`。控制面和设备协议允许同一账号绑定任意数量、不同平台的 Worker，并记录每台设备的系统、架构和独立工作区；新增 Linux/macOS 自包含制品时必须分别在对应 Runner 构建和安装验收。macOS 对外发布还必须补 Developer ID 签名与公证，不能把未公证二进制标成稳定版。

# Worker 私有发布与自动更新

## 发布边界

GitHub 仓库只保存源码、构建脚本和版本标签，不保存 Worker 安装包、发布清单、Actions Artifact 或 GitHub Release 附件。Windows Runner 只在任务临时目录中构建和测试安装包；验收通过后，流水线通过受限 SSH 将安装包与签名清单直接传到 ThreadForge 服务器。

服务器将制品保存到 `.env` 中 `THREADFORGE_WORKER_RELEASE_DIR` 指定的宿主机目录。API 容器以只读方式挂载该目录，登录用户或已配对设备只能通过以下认证接口获取制品：

- `GET /api/v1/worker/releases/latest`
- `GET /api/v1/worker/releases/download/{platform}`

公网域名、服务器 IP 和 SSH 端口只存在于服务器 `.env` 与 GitHub Actions Secrets，不写入源码或文档。

## 信任链

Worker 发布使用独立 Ed25519 密钥。私钥仅保存为 GitHub Actions Secret `WORKER_RELEASE_SIGNING_KEY_B64` 和受权限保护的离线备份；公钥固定在 API 与 Worker 源码中。

发布清单的签名绑定版本、协议版本、平台、文件名、大小和 SHA-256。API 在返回安装包前验证清单签名，并从与清单版本一致的服务器目录读取文件，再次校验大小和 SHA-256。Worker 下载后还会重复验证清单签名、文件名、大小和 SHA-256。

这套签名不等同于 Windows Authenticode。面向普通用户正式发布前，仍应使用代码签名证书签署安装器和内部 EXE，避免 Windows 显示未知发布者。

## 一次性服务器引导

生产服务器需要安装源码中的受限命令分发器，并允许部署账户仅执行该分发器：

```bash
install -o root -g root -m 0755 scripts/threadforge-ci-dispatch.sh /usr/local/sbin/threadforge-ci-dispatch
install -d -o root -g root -m 0755 /var/lib/threadforge/worker-releases
```

部署账户的 `authorized_keys` 固定命令应指向：

```text
restrict,command="sudo -n --preserve-env=SSH_ORIGINAL_COMMAND /usr/local/sbin/threadforge-ci-dispatch" ssh-ed25519 <PUBLIC_KEY> threadforge-cd
```

`sudoers` 只允许无密码执行该分发器；分发器只接受空命令（正常 CD）和严格版本格式的 `publish-worker worker-vX.Y.Z`，其他命令全部拒绝。

## 发布稳定版

1. 同时更新 `local-worker/pyproject.toml` 和 `threadforge_worker.__version__`。
2. 合并到 `main` 并等待 CI/CD 成功。
3. 在该合并提交创建匹配版本标签：

```bash
git tag worker-v0.2.2
git push origin worker-v0.2.2
```

`Worker Release` 工作流会在 Windows Runner 上重新执行 Ruff、测试、PyInstaller/NSIS 构建和真实静默安装冒烟测试。随后生成签名清单，把两个文件压入临时 ZIP，以 Base64 流经 SSH 直接发送到服务器，最后删除 Runner 上的临时载荷。

发布脚本拒绝路径穿越、符号链接、额外文件、版本不一致、大小不一致、摘要不一致，以及用不同字节覆盖已有版本。新清单以原子替换方式生效，旧版本目录保留，避免正在下载的请求读到半个文件。

## 验收

先通过正常登录获取 Cookie，或使用已配对 Worker 的设备令牌，再检查：

```bash
curl -fsS -H "Authorization: Bearer <DEVICE_TOKEN>" \
  https://threadforge.example.com/api/v1/worker/releases/latest
```

服务器还应确认：

```bash
find /var/lib/threadforge/worker-releases -maxdepth 2 -type f -printf '%m %p\n'
```

当前稳定清单只包含 `windows-x86_64`。新增 Linux/macOS 制品时，必须分别在对应目标系统构建和安装验收；macOS 对外发布还需要 Developer ID 签名与公证。

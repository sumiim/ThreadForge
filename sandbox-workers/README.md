# Sandbox Workers

ThreadForge 的不可信工具执行层：Shell 在独立 Docker 容器中执行，限制挂载、网络、CPU、内存、进程数、超时与环境变量。

## 状态

- **后端已实现**：`src/threadforge_sandbox/sandbox.py` 的 `DockerSandboxBackend` 已接线到 `local-worker/runtime.py` 与 `api-server/.../native_runtime.py`（fail-closed，`sandbox_enabled` 默认关闭）。
- **镜像 Dockerfile 已就绪**：本目录 `Dockerfile`（非 root、只读 rootfs、预装 git/python/pytest/node 等工具）。

## 构建与分发镜像

```powershell
# 构建（镜像名须与 SandboxConfig 默认一致）
docker build -t threadforge-sandbox:latest .

# 推送到 Worker 可达的 registry（按实际 registry 改写）
docker tag threadforge-sandbox:latest <registry>/threadforge-sandbox:latest
docker push <registry>/threadforge-sandbox:latest
```

每台 Worker 机器需满足：

1. 安装 Docker（daemon 可达）；
2. 能 `docker pull` 到该镜像（本地 build 或从 registry 拉取）；
3. 镜像名与 Worker 配置的 `sandbox_image` 一致（默认 `threadforge-sandbox:latest`）。

## 默认开启

镜像分发到位后，把 `api-server/config.py` 的 `sandbox_enabled` 默认值从 `False` 改为 `True` 即可。注意：未装 Docker / 未拉镜像的 Worker 上，所有 `run_shell` 会 fail-closed 报错，因此**默认开启前必须先完成镜像分发与 Worker Docker 环境**。

## 安全边界（fail-closed）

- 非 root 用户（`65534:65534`）、只读 rootfs、`--cap-drop ALL` + `no-new-privileges`、无网络、CPU/内存/pids 上限、/tmp tmpfs。
- 工作区以 bind mount 挂 `/workspace`（rw），其余文件系统只读。
- 任何不安全配置 / Docker 缺失都直接报 `SandboxError`，绝不回退到宿主机 shell。

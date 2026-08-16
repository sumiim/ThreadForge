# ThreadForge 沙盒镜像：非 root、只读 rootfs 的编码工具容器。
#
# 后端（DockerSandboxBackend）以 --read-only + --user 65534:65534 + --network none
# 运行本镜像，工作区以 bind mount 挂到 /workspace（rw），/tmp 走 tmpfs。
# 因此所有工具/依赖必须在此镜像里预装好，容器内无法 pip/npm install。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# 编码工具：shell 自带，补 git / curl / 编译工具链 / node
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# 常用 Python 测试/代码质量工具（只读 rootfs 下预装）
RUN pip install --no-cache-dir \
        pytest \
        ruff \
        mypy

# 非 root 用户（与 SandboxConfig 默认 user=65534:65534 一致）
RUN mkdir -p /workspace && chmod 1777 /tmp
USER 65534:65534
WORKDIR /workspace

CMD ["/bin/sh"]

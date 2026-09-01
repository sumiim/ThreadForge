#!/usr/bin/env bash
# ThreadForge 一键部署脚本（Linux/macOS）
set -euo pipefail
cd "$(dirname "$0")"

ACTION="${1:-up}"

assert_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: 未找到 docker。请先安装 docker engine 并确保 'docker' 在 PATH 中。" >&2
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose (v2) 不可用。请升级 docker。" >&2
    exit 1
  fi
}

ensure_env_file() {
  if [ -f .env ]; then
    echo ".env 已存在，保留现有配置 (.env)"
    return
  fi
  cat > .env <<'EOF'
# ThreadForge compose 运行配置(可选;缺省用 compose 内的默认值)
# PICO_OPENAI_API_BASE=https://api.openai.com/v1
# PICO_OPENAI_API_KEY=
# PICO_OPENAI_MODEL=gpt-5.4
# THREADFORGE_WEB_ORIGIN=http://127.0.0.1:5173
# THREADFORGE_DESKTOP_ORIGIN_ENABLED=false
# THREADFORGE_IDENTITY_MODE=single_owner_instance
# THREADFORGE_GITHUB_OAUTH_CLIENT_ID=
# THREADFORGE_GITHUB_OAUTH_CLIENT_SECRET=
# THREADFORGE_GITHUB_OAUTH_CALLBACK_URL=http://127.0.0.1:18000/api/v1/auth/github/callback
# THREADFORGE_GITHUB_OAUTH_RETURN_URL=http://127.0.0.1:5173/
# THREADFORGE_GITHUB_OWNER_LOGIN=
# THREADFORGE_GITHUB_ACCESS_POLICY=allowlist
# THREADFORGE_GITHUB_ALLOWED_LOGINS=[]
# THREADFORGE_SANDBOX_ENABLED=false
# THREADFORGE_SANDBOX_BACKEND=os
# THREADFORGE_WORKER_RELEASE_DIR=./worker-releases
EOF
  echo "已生成默认 .env。如需启用 GitHub OAuth / 沙箱，编辑后重新 up。"
}

invoke_up() {
  ensure_env_file
  echo "Building the sandbox image (for the optional 'docker' sandbox backend)..."
  docker build -t threadforge-sandbox:latest ./sandbox-workers || \
    echo "WARNING: sandbox image build failed (non-fatal; 'os' backend needs no image)."
  echo "Building and starting ThreadForge(api + web)..."
  docker compose up -d --build
  echo ""
  echo "Started."
  echo "  api  (control plane) : http://127.0.0.1:8000"
  echo "  web  (frontend)      : http://127.0.0.1:5173"
  echo "  health check         : ./deploy.sh health"
  echo "  logs                 : ./deploy.sh logs"
  echo "  stop                 : ./deploy.sh stop"
}

invoke_health() {
  local url="http://127.0.0.1:8000/api/v1/config"
  if curl -sf -o /dev/null --max-time 10 "$url"; then
    echo "健康检查 OK: $url"
  else
    echo "健康检查未通过: $url（可能仍在启动;稍后重试或 ./deploy.sh logs）" >&2
  fi
}

assert_compose
case "$ACTION" in
  up)     invoke_up ;;
  health) invoke_health ;;
  ps)     docker compose ps ;;
  logs)   docker compose logs -f ;;
  stop)   docker compose stop ;;
  down)   docker compose down ;;
  *)      echo "未知动作: $ACTION；可用: up | stop | logs | down | health | ps" >&2; exit 1 ;;
esac

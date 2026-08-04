#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_DIR="${THREADFORGE_REPO_DIR:-/root/ThreadForge}"
readonly API_HEALTH_URL="${THREADFORGE_API_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
readonly WEB_HEALTH_URL="${THREADFORGE_WEB_HEALTH_URL:-http://127.0.0.1:5173/}"
readonly WEB_API_HEALTH_URL="${THREADFORGE_WEB_API_HEALTH_URL:-http://127.0.0.1:5173/health/ready}"
readonly LOCK_FILE="${THREADFORGE_DEPLOY_LOCK_FILE:-/run/lock/threadforge-deploy.lock}"
readonly HEALTH_ATTEMPTS="${THREADFORGE_DEPLOY_HEALTH_ATTEMPTS:-30}"
readonly HEALTH_INTERVAL="${THREADFORGE_DEPLOY_HEALTH_INTERVAL:-2}"

deployment_diagnostics() {
    docker compose ps >&2 || true
    docker compose logs --tail=100 api web >&2 || true
}

trap 'echo "Deployment failed at line ${LINENO}." >&2; deployment_diagnostics' ERR

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Another ThreadForge deployment is already running." >&2
    exit 75
fi

cd "${REPO_DIR}"

if [[ "$(git branch --show-current)" != "main" ]]; then
    echo "Refusing to deploy: ${REPO_DIR} is not on branch main." >&2
    exit 1
fi

if [[ -n "$(git status --porcelain=v1)" ]]; then
    echo "Refusing to deploy: ${REPO_DIR} has uncommitted changes." >&2
    git status --short >&2
    exit 1
fi

git pull --ff-only origin main
docker compose up -d --build --remove-orphans api web

for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    api_ready=false
    web_ready=false
    web_api_ready=false

    if api_response=$(curl --fail --silent --show-error --max-time 5 "${API_HEALTH_URL}" 2>/dev/null); then
        api_ready=true
    fi

    if [[ "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 5 "${WEB_HEALTH_URL}" 2>/dev/null || true)" == "200" ]]; then
        web_ready=true
    fi

    if [[ "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 5 "${WEB_API_HEALTH_URL}" 2>/dev/null || true)" == "200" ]]; then
        web_api_ready=true
    fi

    running_services="$(docker compose ps --status running --services)"
    if [[ "${api_ready}" == true ]] &&
       [[ "${web_ready}" == true ]] &&
       [[ "${web_api_ready}" == true ]] &&
       grep -qx 'api' <<<"${running_services}" &&
       grep -qx 'web' <<<"${running_services}"; then
        printf '%s\n' "${api_response}"
        echo "ThreadForge API and Web are ready."
        exit 0
    fi

    sleep "${HEALTH_INTERVAL}"
done

echo "ThreadForge readiness check failed after ${HEALTH_ATTEMPTS} attempts." >&2
deployment_diagnostics
exit 1

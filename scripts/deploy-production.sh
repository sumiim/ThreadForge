#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_DIR="${THREADFORGE_REPO_DIR:-/root/ThreadForge}"
readonly API_HEALTH_URL="${THREADFORGE_API_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
readonly WEB_HEALTH_URL="${THREADFORGE_WEB_HEALTH_URL:-http://127.0.0.1:5173/}"
readonly WEB_API_HEALTH_URL="${THREADFORGE_WEB_API_HEALTH_URL:-http://127.0.0.1:5173/health/ready}"
readonly LOCK_FILE="${THREADFORGE_DEPLOY_LOCK_FILE:-/run/lock/threadforge-deploy.lock}"
readonly HEALTH_ATTEMPTS="${THREADFORGE_DEPLOY_HEALTH_ATTEMPTS:-60}"
readonly HEALTH_INTERVAL="${THREADFORGE_DEPLOY_HEALTH_INTERVAL:-2}"

cd "${REPO_DIR}"

public_host="${THREADFORGE_PUBLIC_HOST:-}"
if [[ -z "${public_host}" && -f .env ]]; then
    public_host="$(sed -n 's/^THREADFORGE_PUBLIC_HOST=//p' .env | tail -n 1)"
    public_host="${public_host%\"}"
    public_host="${public_host#\"}"
    public_host="${public_host%\'}"
    public_host="${public_host#\'}"
fi
compose=(docker compose)
services=(api web)
if [[ -n "${public_host}" ]]; then
    if [[ ! "${public_host}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]]; then
        echo "THREADFORGE_PUBLIC_HOST must be a hostname without a URL scheme or path." >&2
        exit 1
    fi
    compose+=(--profile public -f compose.yaml -f compose.public.yaml)
    services+=(gateway)
fi

deployment_diagnostics() {
    "${compose[@]}" ps >&2 || true
    "${compose[@]}" logs --tail=100 "${services[@]}" >&2 || true
}

trap 'echo "Deployment failed at line ${LINENO}." >&2; deployment_diagnostics' ERR

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Another ThreadForge deployment is already running." >&2
    exit 75
fi

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
"${compose[@]}" config --quiet
if ! docker builder prune --all --force >/dev/null; then
    echo "Warning: failed to prune Docker build cache before deploy." >&2
fi
"${compose[@]}" up -d --build --remove-orphans "${services[@]}"

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

    public_ready=true
    if [[ -n "${public_host}" ]]; then
        public_ready=false
        if [[ "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 10 "https://${public_host}/health/ready" 2>/dev/null || true)" == "200" ]]; then
            public_ready=true
        fi
    fi

    running_services="$("${compose[@]}" ps --status running --services)"
    if [[ "${api_ready}" == true ]] &&
       [[ "${web_ready}" == true ]] &&
       [[ "${web_api_ready}" == true ]] &&
       [[ "${public_ready}" == true ]] &&
       grep -qx 'api' <<<"${running_services}" &&
       grep -qx 'web' <<<"${running_services}" &&
       { [[ -z "${public_host}" ]] || grep -qx 'gateway' <<<"${running_services}"; }; then
        printf '%s\n' "${api_response}"
        echo "ThreadForge API and Web are ready${public_host:+ at https://${public_host}}."
        exit 0
    fi

    sleep "${HEALTH_INTERVAL}"
done

echo "ThreadForge readiness check failed after ${HEALTH_ATTEMPTS} attempts." >&2
deployment_diagnostics
exit 1

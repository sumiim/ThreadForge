#!/usr/bin/env bash
set -Eeuo pipefail

readonly command_name="${SSH_ORIGINAL_COMMAND:-}"

if [[ -z "${command_name}" ]]; then
    exec /usr/local/sbin/deploy-threadforge
fi

if [[ "${command_name}" =~ ^publish-worker\ (worker-v[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    exec /root/ThreadForge/scripts/publish-worker-release.sh "${BASH_REMATCH[1]}"
fi

echo "Unsupported CI command." >&2
exit 64

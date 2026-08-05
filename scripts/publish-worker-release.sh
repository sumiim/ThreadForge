#!/usr/bin/env bash
set -Eeuo pipefail

readonly TAG="${1:-}"
readonly RELEASE_ROOT="${THREADFORGE_WORKER_RELEASE_DIR:-/var/lib/threadforge/worker-releases}"
readonly MAX_ARCHIVE_BYTES=$((180 * 1024 * 1024))

if [[ ! "${TAG}" =~ ^worker-v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    echo "Invalid Worker release tag." >&2
    exit 64
fi
readonly VERSION="${BASH_REMATCH[1]}"
readonly TEMP_DIR="$(mktemp -d /tmp/threadforge-worker-release.XXXXXX)"
trap 'rm -rf -- "${TEMP_DIR}"' EXIT

base64 --decode --ignore-garbage | head -c "$((MAX_ARCHIVE_BYTES + 1))" >"${TEMP_DIR}/payload.zip"
archive_size="$(stat -c '%s' "${TEMP_DIR}/payload.zip")"
if (( archive_size <= 0 || archive_size > MAX_ARCHIVE_BYTES )); then
    echo "Worker release payload has an invalid size." >&2
    exit 65
fi

python3 - "${TEMP_DIR}/payload.zip" "${TEMP_DIR}/verified" "${VERSION}" <<'PY'
import hashlib
import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path

archive_path, output_path, version = map(Path, sys.argv[1:])
version = str(version)
expected_installer = "threadforge-worker-windows-x86_64.exe"
expected_names = {expected_installer, "worker-manifest.json"}

with zipfile.ZipFile(archive_path) as archive:
    entries = archive.infolist()
    if {entry.filename for entry in entries} != expected_names or len(entries) != 2:
        raise SystemExit("Worker release payload contains unexpected files.")
    if sum(entry.file_size for entry in entries) > 180 * 1024 * 1024:
        raise SystemExit("Worker release payload expands beyond its size limit.")
    for entry in entries:
        if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
            raise SystemExit("Worker release payload contains an unsupported entry.")
    output_path.mkdir(mode=0o700)
    for entry in entries:
        with archive.open(entry) as source, (output_path / entry.filename).open("wb") as target:
            shutil.copyfileobj(source, target)

manifest_path = output_path / "worker-manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("version") != version:
    raise SystemExit("Worker manifest version does not match its tag.")
artifact = manifest.get("platforms", {}).get("windows-x86_64", {})
if artifact.get("filename") != expected_installer:
    raise SystemExit("Worker manifest has an unexpected installer filename.")
installer_path = output_path / expected_installer
if artifact.get("size") != installer_path.stat().st_size:
    raise SystemExit("Worker installer size does not match its manifest.")
if artifact.get("sha256") != hashlib.sha256(installer_path.read_bytes()).hexdigest():
    raise SystemExit("Worker installer digest does not match its manifest.")
PY

install -d -m 0755 "${RELEASE_ROOT}" "${RELEASE_ROOT}/${VERSION}"
target_installer="${RELEASE_ROOT}/${VERSION}/threadforge-worker-windows-x86_64.exe"
target_manifest="${RELEASE_ROOT}/${VERSION}/worker-manifest.json"
if [[ -e "${target_installer}" ]]; then
    if ! cmp -s "${TEMP_DIR}/verified/threadforge-worker-windows-x86_64.exe" "${target_installer}"; then
        echo "Refusing to replace an existing Worker version with different bytes." >&2
        exit 65
    fi
else
    install -m 0644 "${TEMP_DIR}/verified/threadforge-worker-windows-x86_64.exe" "${target_installer}"
fi
if [[ -e "${target_manifest}" ]]; then
    if ! cmp -s "${TEMP_DIR}/verified/worker-manifest.json" "${target_manifest}"; then
        echo "Refusing to replace an existing Worker version with a different manifest." >&2
        exit 65
    fi
else
    install -m 0644 "${TEMP_DIR}/verified/worker-manifest.json" "${target_manifest}"
fi
install -m 0644 "${TEMP_DIR}/verified/worker-manifest.json" "${RELEASE_ROOT}/worker-manifest.json.next"
mv -f "${RELEASE_ROOT}/worker-manifest.json.next" "${RELEASE_ROOT}/worker-manifest.json"

echo "Published ThreadForge Worker ${VERSION} to the private server release store."

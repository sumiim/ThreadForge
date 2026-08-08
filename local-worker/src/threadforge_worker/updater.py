"""Signed startup update for the per-user Worker runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__
from .config import ConfigStore

WORKER_RELEASE_PUBLIC_KEY_B64 = "fcq2gihsuHOcqMw1Kjzlq21OHcZ79aaqqlpu5fsokps="
WORKER_PROTOCOL_VERSION = 1
_MAX_BUNDLE_BYTES = 128 * 1024 * 1024
_WINDOWS_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
    subprocess, "DETACHED_PROCESS", 0
)
_DOWNLOAD_RANGE_BYTES = 2 * 1024 * 1024
_DOWNLOAD_READ_BYTES = 64 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30
_DOWNLOAD_RETRY_LIMIT = 5
_DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
_STALL_CHECK_SECONDS = 30.0
_MIN_DOWNLOAD_BYTES_PER_SECOND = 8 * 1024
_PROGRESS_STEP_BYTES = 256 * 1024
_UPDATE_STATUS_NAME = "update-status.json"
LOGGER = logging.getLogger(__name__)

UpdateStatusCallback = Callable[[dict], None]


class UpdateAlreadyRunningError(RuntimeError):
    pass


def update_available(store: ConfigStore) -> tuple[bool, dict]:
    config = store.load()
    manifest = _get_json(
        config.server_url.rstrip("/") + "/api/v1/worker/releases/latest",
        config.device_token,
    )
    _verify_manifest(manifest)
    if manifest["protocol_version"] != WORKER_PROTOCOL_VERSION:
        raise RuntimeError("available Worker release uses an incompatible protocol")
    return _version_tuple(manifest["version"]) > _version_tuple(__version__), manifest


def apply_update(
    store: ConfigStore,
    status_callback: UpdateStatusCallback | None = None,
) -> bool:
    with _update_lock(store.root):
        return _apply_update_locked(store, status_callback)


def _apply_update_locked(
    store: ConfigStore,
    status_callback: UpdateStatusCallback | None,
) -> bool:
    target_version = ""
    target_size = 0
    try:
        _publish_status(store, status_callback, status="checking")
        available, manifest = update_available(store)
        target_version = str(manifest["version"])
        if not available:
            _publish_status(
                store,
                status_callback,
                status="current",
                target_version=target_version,
            )
            return False
        if sys.platform != "win32":
            _publish_status(
                store,
                status_callback,
                status="unsupported",
                target_version=target_version,
            )
            return False

        artifact = manifest["platforms"]["windows-x86_64"]
        target_size = int(artifact["size"])
        installer_path = _download_installer(
            store,
            artifact,
            target_version=target_version,
            status_callback=status_callback,
        )
        _publish_status(
            store,
            status_callback,
            status="installing",
            target_version=target_version,
            downloaded_bytes=artifact["size"],
            total_bytes=artifact["size"],
        )
        subprocess.Popen(
            [str(installer_path), "/S"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_WINDOWS_CREATION_FLAGS,
            env={
                **os.environ,
                # The NSIS installer starts the replacement frozen Worker. Without
                # this flag it inherits the old process's soon-to-be-deleted _MEI
                # directory and can fail while loading python312.dll.
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            },
        )
        return True
    except Exception as exc:
        partial_size = _partial_download_size(store)
        _publish_status(
            store,
            status_callback,
            status="failed",
            target_version=target_version,
            downloaded_bytes=partial_size,
            total_bytes=target_size,
            error=str(exc)[:500],
        )
        raise


@contextmanager
def _update_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "update.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UpdateAlreadyRunningError("Worker update is already running") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UpdateAlreadyRunningError("Worker update is already running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def load_update_status(store: ConfigStore) -> dict:
    path = store.root / _UPDATE_STATUS_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        return {
            "status": str(payload.get("status", ""))[:32],
            "current_version": str(payload.get("current_version", ""))[:32],
            "target_version": str(payload.get("target_version", ""))[:32],
            "downloaded_bytes": max(0, int(payload.get("downloaded_bytes", 0))),
            "total_bytes": max(0, int(payload.get("total_bytes", 0))),
            "bytes_per_second": max(0, int(payload.get("bytes_per_second", 0))),
            "retry_count": max(0, int(payload.get("retry_count", 0))),
            "error": str(payload.get("error", ""))[:500],
            "updated_at": str(payload.get("updated_at", ""))[:40],
        }
    except (TypeError, ValueError):
        return {}


def _download_installer(
    store: ConfigStore,
    artifact: dict,
    *,
    target_version: str,
    status_callback: UpdateStatusCallback | None,
) -> Path:
    expected_size = int(artifact["size"])
    expected_digest = str(artifact["sha256"])
    update_root = store.root / "updates"
    update_root.mkdir(parents=True, exist_ok=True)
    installer_path = update_root / artifact["filename"]
    download_path = installer_path.with_suffix(installer_path.suffix + ".download")

    if _file_matches(installer_path, expected_size, expected_digest):
        return installer_path
    if installer_path.exists():
        installer_path.unlink()

    existing_size = download_path.stat().st_size if download_path.is_file() else 0
    if existing_size > expected_size:
        download_path.unlink()
        existing_size = 0
    if existing_size == expected_size:
        if _file_matches(download_path, expected_size, expected_digest):
            download_path.replace(installer_path)
            return installer_path
        download_path.unlink()
        existing_size = 0

    config = store.load()
    base_headers = {
        "Authorization": f"Bearer {config.device_token}",
        "User-Agent": f"ThreadForge-Worker/{__version__}",
    }
    download_url = (
        config.server_url.rstrip("/")
        + "/api/v1/worker/releases/download/windows-x86_64"
    )
    digest = hashlib.sha256()
    if existing_size:
        _hash_file(download_path, digest)
    total = existing_size
    transfer_started = time.monotonic()
    transfer_start_bytes = total
    last_reported = total // _PROGRESS_STEP_BYTES
    consecutive_failures = 0
    _publish_status(
        store,
        status_callback,
        status="downloading",
        target_version=target_version,
        downloaded_bytes=total,
        total_bytes=expected_size,
    )

    while total < expected_size:
        range_start = total
        range_end = min(expected_size - 1, range_start + _DOWNLOAD_RANGE_BYTES - 1)
        request = urllib.request.Request(
            download_url,
            headers={
                **base_headers,
                "Range": f"bytes={range_start}-{range_end}",
            },
        )
        attempt_started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request,
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                _validate_range_response(response, range_start, range_end, expected_size)
                with download_path.open("ab") as output:
                    while total <= range_end:
                        chunk = response.read(
                            min(_DOWNLOAD_READ_BYTES, range_end - total + 1)
                        )
                        if not chunk:
                            raise OSError("Worker update connection ended early")
                        total += len(chunk)
                        if total > _MAX_BUNDLE_BYTES or total > expected_size:
                            download_path.unlink(missing_ok=True)
                            raise RuntimeError("Worker update exceeded its signed size")
                        digest.update(chunk)
                        output.write(chunk)
                        now = time.monotonic()
                        attempt_elapsed = now - attempt_started
                        attempt_bytes = total - range_start
                        if (
                            attempt_elapsed >= _STALL_CHECK_SECONDS
                            and attempt_bytes / attempt_elapsed
                            < _MIN_DOWNLOAD_BYTES_PER_SECOND
                        ):
                            raise TimeoutError("Worker update connection stalled")
                        progress_step = total // _PROGRESS_STEP_BYTES
                        if progress_step > last_reported or total == expected_size:
                            last_reported = progress_step
                            _publish_status(
                                store,
                                status_callback,
                                status="downloading",
                                target_version=target_version,
                                downloaded_bytes=total,
                                total_bytes=expected_size,
                                bytes_per_second=_transfer_speed(
                                    total,
                                    transfer_start_bytes,
                                    transfer_started,
                                ),
                            )
            consecutive_failures = 0
        except Exception as exc:
            if total > range_start:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            if consecutive_failures >= _DOWNLOAD_RETRY_LIMIT:
                raise RuntimeError(
                    f"Worker update download failed after {_DOWNLOAD_RETRY_LIMIT} retries: {exc}"
                ) from exc
            retry_number = max(1, consecutive_failures)
            _publish_status(
                store,
                status_callback,
                status="retrying",
                target_version=target_version,
                downloaded_bytes=total,
                total_bytes=expected_size,
                bytes_per_second=_transfer_speed(
                    total,
                    transfer_start_bytes,
                    transfer_started,
                ),
                retry_count=retry_number,
                error=str(exc)[:500],
            )
            time.sleep(_DOWNLOAD_RETRY_DELAY_SECONDS * retry_number)
            _publish_status(
                store,
                status_callback,
                status="downloading",
                target_version=target_version,
                downloaded_bytes=total,
                total_bytes=expected_size,
                bytes_per_second=_transfer_speed(
                    total,
                    transfer_start_bytes,
                    transfer_started,
                ),
            )
    if total != expected_size:
        raise RuntimeError("Worker update ended before its signed size")
    if digest.hexdigest() != expected_digest:
        download_path.unlink(missing_ok=True)
        raise RuntimeError("Worker update installer failed checksum verification")
    download_path.replace(installer_path)
    return installer_path


def _validate_range_response(response, start: int, end: int, total_size: int) -> None:
    status = int(getattr(response, "status", 200) or 200)
    if status == 200 and start == 0 and end == total_size - 1:
        return
    if status != 206:
        raise RuntimeError("Worker update server does not support resumable downloads")
    expected = f"bytes {start}-{end}/{total_size}"
    if str(response.headers.get("Content-Range", "")) != expected:
        raise RuntimeError("Worker update server returned an invalid byte range")


def _transfer_speed(total: int, start_bytes: int, started: float) -> int:
    return int(max(0, total - start_bytes) / max(0.001, time.monotonic() - started))


def _hash_file(path: Path, digest) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)


def _file_matches(path: Path, expected_size: int, expected_digest: str) -> bool:
    try:
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        _hash_file(path, digest)
        return digest.hexdigest() == expected_digest
    except OSError:
        return False


def _partial_download_size(store: ConfigStore) -> int:
    update_root = store.root / "updates"
    try:
        return max(
            (path.stat().st_size for path in update_root.glob("*.download")),
            default=0,
        )
    except OSError:
        return 0


def _publish_status(
    store: ConfigStore,
    callback: UpdateStatusCallback | None,
    *,
    status: str,
    target_version: str = "",
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    bytes_per_second: int = 0,
    retry_count: int = 0,
    error: str = "",
) -> None:
    payload = {
        "status": status,
        "current_version": __version__,
        "target_version": target_version,
        "downloaded_bytes": max(0, int(downloaded_bytes)),
        "total_bytes": max(0, int(total_bytes)),
        "bytes_per_second": max(0, int(bytes_per_second)),
        "retry_count": max(0, int(retry_count)),
        "error": error,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    store.root.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=store.root,
            prefix=f"{_UPDATE_STATUS_NAME}.",
            suffix=".tmp",
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_path, store.root / _UPDATE_STATUS_NAME)
        temporary_path = None
    except OSError:
        LOGGER.warning("Unable to persist Worker update status", exc_info=True)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if status == "failed":
        LOGGER.warning("Worker update failed: %s", error)
    elif status != "downloading" or downloaded_bytes in {0, total_bytes}:
        LOGGER.info(
            "Worker update status=%s current=%s target=%s downloaded=%s total=%s",
            status,
            __version__,
            target_version or "-",
            downloaded_bytes,
            total_bytes,
        )
    if callback is not None:
        try:
            callback(dict(payload))
        except Exception:
            LOGGER.debug("Unable to report Worker update status", exc_info=True)


def _get_json(url: str, token: str) -> dict:
    if not token:
        raise RuntimeError("Worker must be paired before checking for updates")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"ThreadForge-Worker/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise RuntimeError("Worker release manifest is too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError("Worker release manifest is invalid")
    return result


def _verify_manifest(
    manifest: dict, public_key_b64: str = WORKER_RELEASE_PUBLIC_KEY_B64
) -> None:
    signature = manifest.get("signature")
    if manifest.get("schema_version") != 1 or not isinstance(signature, dict):
        raise RuntimeError("Worker release manifest is unsupported")
    if signature.get("algorithm") != "ed25519":
        raise RuntimeError("Worker release manifest is unsigned")
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(public_key_b64, validate=True)
    )
    try:
        public_key.verify(
            base64.b64decode(str(signature.get("value", "")), validate=True), canonical
        )
    except (InvalidSignature, ValueError) as exc:
        raise RuntimeError("Worker release manifest signature is invalid") from exc
    _version_tuple(str(manifest.get("version", "")))
    platforms = manifest.get("platforms")
    artifact = platforms.get("windows-x86_64") if isinstance(platforms, dict) else None
    if not isinstance(artifact, dict):
        raise TypeError("Worker release has no Windows artifact")
    expected_filename = "threadforge-worker-windows-x86_64.exe"
    if artifact.get("filename") != expected_filename:
        raise RuntimeError("Worker release artifact filename is invalid")
    if (
        not isinstance(artifact.get("size"), int)
        or not 0 < artifact["size"] <= _MAX_BUNDLE_BYTES
        or not re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("sha256", "")))
    ):
        raise RuntimeError("Worker release artifact metadata is invalid")


def _version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise RuntimeError("Worker release version is invalid")
    return tuple(int(part) for part in value.split("."))

from __future__ import annotations

import base64
import hashlib
import json
from email.message import Message
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threadforge_worker.config import ConfigStore, WorkerConfig
from threadforge_worker.updater import (
    UpdateAlreadyRunningError,
    _update_lock,
    _verify_manifest,
    _version_tuple,
    apply_update,
)


class _Response:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        result = self._body[self._offset : self._offset + size]
        self._offset += len(result)
        return result


class _InterruptedResponse(_Response):
    def __init__(self, body: bytes, *, fail_after: int, headers: dict[str, str]):
        super().__init__(body, status=206, headers=headers)
        self._fail_after = fail_after

    def read(self, size: int = -1) -> bytes:
        if self._offset >= self._fail_after:
            raise OSError("simulated connection reset")
        return super().read(min(size, self._fail_after - self._offset))


def _manifest(private_key) -> dict:
    manifest = {
        "schema_version": 1,
        "channel": "stable",
        "version": "0.2.0",
        "protocol_version": 1,
        "minimum_server_protocol": 1,
        "published_at": "2026-08-05T00:00:00Z",
        "platforms": {
            "windows-x86_64": {
                "filename": "threadforge-worker-windows-x86_64.exe",
                "size": 10,
                "sha256": hashlib.sha256(b"0123456789").hexdigest(),
            }
        },
    }
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    manifest["signature"] = {
        "algorithm": "ed25519",
        "value": base64.b64encode(private_key.sign(canonical)).decode(),
    }
    return manifest


def test_release_signature_and_version_validation():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    manifest = _manifest(private_key)
    _verify_manifest(manifest, base64.b64encode(public_key).decode())
    assert _version_tuple("1.12.3") > _version_tuple("1.2.9")

    manifest["version"] = "9.9.9"
    with pytest.raises(RuntimeError, match="signature"):
        _verify_manifest(manifest, base64.b64encode(public_key).decode())


def test_release_rejects_unexpected_installer_filename():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    manifest = _manifest(private_key)
    manifest["platforms"]["windows-x86_64"]["filename"] = "other.exe"
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    manifest["signature"]["value"] = base64.b64encode(private_key.sign(canonical)).decode()

    with pytest.raises(RuntimeError, match="filename"):
        _verify_manifest(manifest, base64.b64encode(public_key).decode())


def test_apply_update_downloads_verified_installer_and_launches_it(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(WorkerConfig(server_url="https://threadforge.example", device_token="token"))
    artifact = b"0123456789"
    manifest = {
        "version": "99.0.0",
        "platforms": {
            "windows-x86_64": {
                "filename": "threadforge-worker-windows-x86_64.exe",
                "size": len(artifact),
                "sha256": hashlib.sha256(artifact).hexdigest(),
            }
        },
    }
    launched = Mock()
    monkeypatch.setattr("threadforge_worker.updater.update_available", lambda _store: (True, manifest))
    monkeypatch.setattr("threadforge_worker.updater.sys.platform", "win32")
    monkeypatch.setattr(
        "threadforge_worker.updater.urllib.request.urlopen", lambda *_args, **_kwargs: _Response(artifact)
    )
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\stale-mei")
    monkeypatch.setattr("threadforge_worker.updater.subprocess.Popen", launched)

    assert apply_update(store) is True
    installer = tmp_path / "updates" / "threadforge-worker-windows-x86_64.exe"
    assert installer.read_bytes() == artifact
    assert launched.call_args.args[0] == [str(installer), "/S"]
    child_env = launched.call_args.kwargs["env"]
    assert child_env["_PYI_APPLICATION_HOME_DIR"] == r"C:\Temp\stale-mei"
    assert child_env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    status = json.loads((tmp_path / "update-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "installing"
    assert status["downloaded_bytes"] == len(artifact)


def test_apply_update_resumes_a_partial_download(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(WorkerConfig(server_url="https://threadforge.example", device_token="token"))
    artifact = b"0123456789"
    manifest = {
        "version": "99.0.0",
        "platforms": {
            "windows-x86_64": {
                "filename": "threadforge-worker-windows-x86_64.exe",
                "size": len(artifact),
                "sha256": hashlib.sha256(artifact).hexdigest(),
            }
        },
    }
    update_root = tmp_path / "updates"
    update_root.mkdir()
    partial = update_root / "threadforge-worker-windows-x86_64.exe.download"
    partial.write_bytes(artifact[:4])
    requests = []

    def open_range(request, **_kwargs):
        requests.append(request)
        return _Response(
            artifact[4:],
            status=206,
            headers={"Content-Range": "bytes 4-9/10"},
        )

    monkeypatch.setattr("threadforge_worker.updater.update_available", lambda _store: (True, manifest))
    monkeypatch.setattr("threadforge_worker.updater.sys.platform", "win32")
    monkeypatch.setattr("threadforge_worker.updater.urllib.request.urlopen", open_range)
    monkeypatch.setattr("threadforge_worker.updater.subprocess.Popen", Mock())

    assert apply_update(store) is True
    assert requests[0].get_header("Range") == "bytes=4-9"
    assert (update_root / "threadforge-worker-windows-x86_64.exe").read_bytes() == artifact


def test_apply_update_reconnects_after_a_partial_range_response(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(WorkerConfig(server_url="https://threadforge.example", device_token="token"))
    artifact = b"0123456789"
    manifest = {
        "version": "99.0.0",
        "platforms": {
            "windows-x86_64": {
                "filename": "threadforge-worker-windows-x86_64.exe",
                "size": len(artifact),
                "sha256": hashlib.sha256(artifact).hexdigest(),
            }
        },
    }
    requests = []
    statuses = []

    def open_range(request, **_kwargs):
        requests.append(request)
        start = int(request.get_header("Range").split("=")[1].split("-")[0])
        if len(requests) == 1:
            return _InterruptedResponse(
                artifact,
                fail_after=4,
                headers={"Content-Range": "bytes 0-9/10"},
            )
        return _Response(
            artifact[start:],
            status=206,
            headers={"Content-Range": f"bytes {start}-9/10"},
        )

    monkeypatch.setattr("threadforge_worker.updater.update_available", lambda _store: (True, manifest))
    monkeypatch.setattr("threadforge_worker.updater.sys.platform", "win32")
    monkeypatch.setattr("threadforge_worker.updater.urllib.request.urlopen", open_range)
    monkeypatch.setattr("threadforge_worker.updater.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("threadforge_worker.updater.subprocess.Popen", Mock())

    assert apply_update(store, statuses.append) is True
    assert [request.get_header("Range") for request in requests] == ["bytes=0-9", "bytes=4-9"]
    assert any(status["status"] == "retrying" for status in statuses)
    assert (tmp_path / "updates" / "threadforge-worker-windows-x86_64.exe").read_bytes() == artifact


def test_apply_update_reports_failed_after_repeated_connection_errors(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(WorkerConfig(server_url="https://threadforge.example", device_token="token"))
    artifact = b"0123456789"
    manifest = {
        "version": "99.0.0",
        "platforms": {
            "windows-x86_64": {
                "filename": "threadforge-worker-windows-x86_64.exe",
                "size": len(artifact),
                "sha256": hashlib.sha256(artifact).hexdigest(),
            }
        },
    }
    statuses = []

    def fail_open(*_args, **_kwargs):
        raise OSError("simulated connection reset")

    monkeypatch.setattr("threadforge_worker.updater.update_available", lambda _store: (True, manifest))
    monkeypatch.setattr("threadforge_worker.updater.sys.platform", "win32")
    monkeypatch.setattr("threadforge_worker.updater.urllib.request.urlopen", fail_open)
    monkeypatch.setattr("threadforge_worker.updater.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="after 5 retries"):
        apply_update(store, statuses.append)
    assert statuses[-1]["status"] == "failed"
    assert "after 5 retries" in statuses[-1]["error"]


def test_update_lock_rejects_a_second_updater(tmp_path):
    with (
        _update_lock(tmp_path),
        pytest.raises(UpdateAlreadyRunningError),
        _update_lock(tmp_path),
    ):
        pass

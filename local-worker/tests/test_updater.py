from __future__ import annotations

import base64
import hashlib
import io
import json
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threadforge_worker.config import ConfigStore, WorkerConfig
from threadforge_worker.updater import _verify_manifest, _version_tuple, apply_update


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
                "url": "https://github.com/sumiim/ThreadForge/releases/download/worker-v0.2.0/threadforge-worker-windows-x86_64.exe",
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
        "threadforge_worker.updater.urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(artifact)
    )
    monkeypatch.setattr("threadforge_worker.updater.subprocess.Popen", launched)

    assert apply_update(store) is True
    installer = tmp_path / "updates" / "threadforge-worker-windows-x86_64.exe"
    assert installer.read_bytes() == artifact
    assert launched.call_args.args[0] == [str(installer), "/S"]

from __future__ import annotations

import base64
import hashlib
import json
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threadforge_worker.updater import _extract_bundle, _verify_manifest, _version_tuple


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
                "filename": "threadforge-worker-windows-x86_64.zip",
                "url": "https://github.com/sumiim/ThreadForge/releases/download/worker-v0.2.0/threadforge-worker-windows-x86_64.zip",
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


def test_update_archive_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "worker.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.whl", b"bad")

    with pytest.raises(RuntimeError, match="unsafe path"):
        _extract_bundle(archive_path, tmp_path / "output")
    assert not (tmp_path / "outside.whl").exists()

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threadforge_api.domain.errors import WorkerReleaseUnavailableError
from threadforge_api.infrastructure.worker_releases import WorkerReleaseService


def _signed_manifest(private_key, artifact: bytes) -> dict:
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
                "size": len(artifact),
                "sha256": hashlib.sha256(artifact).hexdigest(),
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


def test_signed_manifest_and_artifact_are_verified(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    artifact = b"signed worker bundle"
    manifest = _signed_manifest(private_key, artifact)
    (tmp_path / "worker-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    version_dir = tmp_path / "0.2.0"
    version_dir.mkdir()
    (version_dir / "threadforge-worker-windows-x86_64.exe").write_bytes(artifact)
    releases = WorkerReleaseService(
        tmp_path,
        1024 * 1024,
        public_key_b64=base64.b64encode(public_key).decode(),
    )

    assert releases.latest()["version"] == "0.2.0"
    handle, metadata = releases.open_verified_artifact("windows-x86_64")
    try:
        assert handle.read() == artifact
        assert metadata["size"] == len(artifact)
    finally:
        handle.close()


def test_tampered_manifest_is_rejected(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    manifest = _signed_manifest(private_key, b"bundle")
    manifest["version"] = "9.9.9"
    (tmp_path / "worker-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    releases = WorkerReleaseService(
        tmp_path,
        1024 * 1024,
        public_key_b64=base64.b64encode(public_key).decode(),
    )

    with pytest.raises(WorkerReleaseUnavailableError):
        releases.latest()


def test_artifact_must_exist_in_signed_version_directory(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    artifact = b"bundle"
    manifest = _signed_manifest(private_key, artifact)
    (tmp_path / "worker-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "threadforge-worker-windows-x86_64.exe").write_bytes(artifact)
    releases = WorkerReleaseService(
        tmp_path,
        1024 * 1024,
        public_key_b64=base64.b64encode(public_key).decode(),
    )

    with pytest.raises(WorkerReleaseUnavailableError):
        releases.open_verified_artifact("windows-x86_64")

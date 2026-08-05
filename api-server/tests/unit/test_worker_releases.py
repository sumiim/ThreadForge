from __future__ import annotations

import base64
import hashlib
import io
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threadforge_api.domain.errors import WorkerReleaseUnavailableError
from threadforge_api.infrastructure.worker_releases import WorkerReleaseService


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


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
                "url": "https://github.com/sumiim/ThreadForge/releases/download/worker-v0.2.0/threadforge-worker-windows-x86_64.exe",
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


def test_signed_manifest_and_artifact_are_verified(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    artifact = b"signed worker bundle"
    manifest = _signed_manifest(private_key, artifact)
    responses = [json.dumps(manifest).encode(), artifact]
    monkeypatch.setattr(
        "threadforge_api.infrastructure.worker_releases.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(responses.pop(0)),
    )
    releases = WorkerReleaseService(
        "https://example.test/worker-manifest.json",
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


def test_tampered_manifest_is_rejected(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    manifest = _signed_manifest(private_key, b"bundle")
    manifest["version"] = "9.9.9"
    monkeypatch.setattr(
        "threadforge_api.infrastructure.worker_releases.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(json.dumps(manifest).encode()),
    )
    releases = WorkerReleaseService(
        "https://example.test/worker-manifest.json",
        1024 * 1024,
        public_key_b64=base64.b64encode(public_key).decode(),
    )

    with pytest.raises(WorkerReleaseUnavailableError):
        releases.latest()

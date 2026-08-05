"""Signed Worker release discovery and bounded artifact proxying."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..domain.errors import WorkerReleaseUnavailableError

WORKER_RELEASE_PUBLIC_KEY_B64 = "fcq2gihsuHOcqMw1Kjzlq21OHcZ79aaqqlpu5fsokps="
_ALLOWED_ARTIFACT_PREFIX = "https://github.com/sumiim/ThreadForge/releases/download/"
_PLATFORMS = {"windows-x86_64"}


class WorkerReleaseService:
    def __init__(
        self,
        manifest_url: str,
        max_bytes: int,
        cache_seconds: int = 300,
        public_key_b64: str = WORKER_RELEASE_PUBLIC_KEY_B64,
    ):
        self.manifest_url = manifest_url
        self.max_bytes = max_bytes
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()
        self._public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True)
        )

    def latest(self, *, refresh: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            if not refresh and self._cache and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]
        try:
            request = urllib.request.Request(
                self.manifest_url,
                headers={"Accept": "application/json", "User-Agent": "ThreadForge-Control-Plane"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError("release manifest is too large")
            manifest = json.loads(raw)
            self._validate_manifest(manifest)
        except WorkerReleaseUnavailableError:
            raise
        except Exception as exc:
            raise WorkerReleaseUnavailableError("Worker release manifest is unavailable") from exc
        with self._lock:
            self._cache = (now, manifest)
        return manifest

    def open_verified_artifact(self, platform_name: str):
        manifest = self.latest()
        artifact = manifest["platforms"].get(platform_name)
        if not isinstance(artifact, dict):
            raise WorkerReleaseUnavailableError("Worker release is unavailable for this platform")
        expected_size = int(artifact["size"])
        expected_digest = str(artifact["sha256"])
        if expected_size > self.max_bytes:
            raise WorkerReleaseUnavailableError("Worker release exceeds the configured size limit")
        # Ownership is transferred to the StreamingResponse background task.
        temporary = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # noqa: SIM115
        digest = hashlib.sha256()
        total = 0
        try:
            request = urllib.request.Request(
                artifact["url"], headers={"User-Agent": "ThreadForge-Control-Plane"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > self.max_bytes or total > expected_size:
                        raise ValueError("release artifact exceeded its signed size")
                    digest.update(chunk)
                    temporary.write(chunk)
            if total != expected_size or digest.hexdigest() != expected_digest:
                raise ValueError("release artifact does not match its signed manifest")
            temporary.seek(0)
            return temporary, artifact
        except Exception as exc:
            temporary.close()
            raise WorkerReleaseUnavailableError("Worker release download failed verification") from exc

    def _validate_manifest(self, manifest) -> None:
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("unsupported release manifest")
        signature = manifest.get("signature")
        if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
            raise ValueError("release manifest is unsigned")
        unsigned = {key: value for key, value in manifest.items() if key != "signature"}
        canonical = json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        try:
            self._public_key.verify(
                base64.b64decode(str(signature.get("value", "")), validate=True), canonical
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("release manifest signature is invalid") from exc
        if not _semantic_version(str(manifest.get("version", ""))):
            raise ValueError("invalid Worker release version")
        if manifest.get("protocol_version") != 1:
            raise ValueError("release protocol is incompatible")
        platforms = manifest.get("platforms")
        if not isinstance(platforms, dict) or not platforms:
            raise ValueError("release manifest has no artifacts")
        for name, artifact in platforms.items():
            if name not in _PLATFORMS or not isinstance(artifact, dict):
                raise ValueError("unsupported release platform")
            url = str(artifact.get("url", ""))
            parsed = urllib.parse.urlsplit(url)
            if not url.startswith(_ALLOWED_ARTIFACT_PREFIX) or parsed.query or parsed.fragment:
                raise ValueError("release artifact URL is not trusted")
            expected_filename = f"threadforge-worker-{name}.exe"
            if artifact.get("filename") != expected_filename or not parsed.path.endswith(
                "/" + expected_filename
            ):
                raise ValueError("release artifact filename is invalid")
            if not isinstance(artifact.get("size"), int) or artifact["size"] <= 0:
                raise ValueError("release artifact size is invalid")
            digest = str(artifact.get("sha256", ""))
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("release artifact digest is invalid")


def _semantic_version(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)

"""Signed startup update for the per-user Worker runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__
from .config import ConfigStore

WORKER_RELEASE_PUBLIC_KEY_B64 = "fcq2gihsuHOcqMw1Kjzlq21OHcZ79aaqqlpu5fsokps="
WORKER_PROTOCOL_VERSION = 1
_MAX_BUNDLE_BYTES = 128 * 1024 * 1024


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


def apply_update(store: ConfigStore) -> bool:
    available, manifest = update_available(store)
    if not available:
        return False
    if sys.platform != "win32":
        return False
    artifact = manifest["platforms"]["windows-x86_64"]
    config = store.load()
    request = urllib.request.Request(
        config.server_url.rstrip("/")
        + "/api/v1/worker/releases/download/windows-x86_64",
        headers={
            "Authorization": f"Bearer {config.device_token}",
            "User-Agent": f"ThreadForge-Worker/{__version__}",
        },
    )
    store.root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="update-", dir=store.root) as temp_dir:
        bundle_path = Path(temp_dir) / artifact["filename"]
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(request, timeout=60) as response, bundle_path.open("wb") as output:
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > _MAX_BUNDLE_BYTES or total > artifact["size"]:
                    raise RuntimeError("Worker update exceeded its signed size")
                digest.update(chunk)
                output.write(chunk)
        if total != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
            raise RuntimeError("Worker update bundle failed checksum verification")
        extract_root = Path(temp_dir) / "bundle"
        _extract_bundle(bundle_path, extract_root)
        wheels = sorted((extract_root / "packages").glob("*.whl"))
        pico = next((path for path in wheels if path.name.startswith("pico-")), None)
        worker = next(
            (path for path in wheels if path.name.startswith("threadforge_worker-")), None
        )
        if pico is None or worker is None:
            raise RuntimeError("Worker update bundle is incomplete")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--upgrade",
                "--no-index",
                "--find-links",
                str(extract_root / "packages"),
                str(pico),
                str(worker),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
        )
    return True


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


def _extract_bundle(bundle_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(bundle_path) as archive:
        members = archive.infolist()
        if len(members) > 32 or sum(item.file_size for item in members) > 256 * 1024 * 1024:
            raise RuntimeError("Worker update archive is unsafe")
        destination.mkdir()
        for member in members:
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError("Worker update archive contains an unsafe path")
            if member.is_dir():
                continue
            if not (
                member_path.parts[:1] == ("packages",) and member_path.suffix == ".whl"
            ) and member_path.name != "install-worker.ps1":
                raise RuntimeError("Worker update archive contains an unexpected file")
            target = destination / member_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                while chunk := source.read(64 * 1024):
                    output.write(chunk)

"""User-scoped Worker configuration without server or provider secrets in Git."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "ThreadForge" / "Worker"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "threadforge-worker"


@dataclass
class LocalWorkspace:
    workspace_id: str
    name: str
    path: str

    def public_dict(self) -> dict:
        root = Path(self.path)
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "is_git": (root / ".git").exists(),
        }


@dataclass
class WorkerConfig:
    server_url: str = "http://127.0.0.1:18000"
    device_id: str = ""
    device_token: str = ""
    device_name: str = ""
    workspaces: list[LocalWorkspace] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "server_url": self.server_url,
            "device_id": self.device_id,
            "device_token": _protect_token(self.device_token),
            "device_name": self.device_name,
            "workspaces": [workspace.__dict__ for workspace in self.workspaces],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> WorkerConfig:
        return cls(
            server_url=str(payload.get("server_url", "http://127.0.0.1:18000")).rstrip("/"),
            device_id=str(payload.get("device_id", "")),
            device_token=_unprotect_token(str(payload.get("device_token", ""))),
            device_name=str(payload.get("device_name", "")),
            workspaces=[
                LocalWorkspace(
                    workspace_id=str(item["workspace_id"]),
                    name=str(item["name"]),
                    path=str(item["path"]),
                )
                for item in payload.get("workspaces", [])
                if isinstance(item, dict)
            ],
        )


class ConfigStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or default_data_dir()).resolve()
        self.path = self.root / "worker.json"

    def load(self) -> WorkerConfig:
        if not self.path.is_file():
            return WorkerConfig()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Worker config must be a JSON object")
        return WorkerConfig.from_dict(payload)

    def save(self, config: WorkerConfig) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=self.root,
                prefix="worker.json.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)
            self.path.chmod(0o600)

    def add_workspace(self, config: WorkerConfig, path: str, name: str | None = None) -> LocalWorkspace:
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace path must be a directory")
        for existing in config.workspaces:
            if Path(existing.path) == root:
                return existing
        workspace = LocalWorkspace(
            workspace_id="ws_" + uuid.uuid4().hex,
            name=(name or root.name).strip() or root.name,
            path=str(root),
        )
        config.workspaces.append(workspace)
        self.save(config)
        return workspace


def _protect_token(token: str) -> str:
    """Encrypt the device credential with the current Windows user profile."""
    if not token or sys.platform != "win32" or token.startswith("dpapi:"):
        return token
    import win32crypt

    protected = win32crypt.CryptProtectData(
        token.encode("utf-8"),
        "ThreadForge local Worker device token",
        None,
        None,
        None,
        0,
    )
    return "dpapi:" + urlsafe_b64encode(protected).decode("ascii")


def _unprotect_token(token: str) -> str:
    if not token.startswith("dpapi:"):
        return token
    if sys.platform != "win32":
        raise RuntimeError("a Windows-protected Worker token cannot be read on this platform")
    import win32crypt

    try:
        _, plaintext = win32crypt.CryptUnprotectData(
            urlsafe_b64decode(token[6:].encode("ascii")),
            None,
            None,
            None,
            0,
        )
    except Exception as exc:
        raise RuntimeError("Worker device token cannot be decrypted by the current Windows user") from exc
    return plaintext.decode("utf-8")

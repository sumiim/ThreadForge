"""Persistent local-Worker device identities and short-lived pairing codes."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.entities import utc_now
from ..domain.errors import (
    AuthorizationDeniedError,
    DeviceNotFoundError,
    PairingCodeInvalidError,
)
from ..domain.identity import canonical_owner_id
from .jsonutil import read_json, secure_directory, write_json_atomic

_DEVICE_ID = re.compile(r"^dev_[a-f0-9]{32}$")


@dataclass(frozen=True)
class WorkerWorkspace:
    workspace_id: str
    name: str
    is_git: bool = False

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "display_name": self.name,
            "display_name_source": "user",
            "is_git": self.is_git,
        }


@dataclass
class Device:
    device_id: str
    owner_id: str
    name: str
    token_digest: str
    created_at: str = field(default_factory=utc_now)
    last_seen_at: str = ""
    model: str = ""
    model_configured: bool = False
    version: str = ""
    protocol_version: int = 0
    platform: str = ""
    architecture: str = ""
    capabilities: list[str] = field(default_factory=list)
    orchestration_backend: str = ""
    model_capabilities: dict = field(default_factory=dict)
    workspaces: list[WorkerWorkspace] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "device_id": self.device_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "token_digest": self.token_digest,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "model": self.model,
            "model_configured": self.model_configured,
            "version": self.version,
            "protocol_version": self.protocol_version,
            "platform": self.platform,
            "architecture": self.architecture,
            "capabilities": list(self.capabilities),
            "orchestration_backend": self.orchestration_backend,
            "model_capabilities": dict(self.model_capabilities),
            "workspaces": [workspace.to_dict() for workspace in self.workspaces],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Device:
        return cls(
            device_id=str(payload["device_id"]),
            owner_id=canonical_owner_id(payload["owner_id"]),
            name=str(payload["name"]),
            token_digest=str(payload["token_digest"]),
            created_at=str(payload.get("created_at", "")),
            last_seen_at=str(payload.get("last_seen_at", "")),
            model=str(payload.get("model", "")),
            model_configured=bool(payload.get("model_configured", False)),
            version=str(payload.get("version", ""))[:32],
            protocol_version=int(payload.get("protocol_version", 0)),
            platform=str(payload.get("platform", ""))[:32],
            architecture=str(payload.get("architecture", ""))[:32],
            capabilities=[
                str(item)
                for item in payload.get("capabilities", [])
                if isinstance(item, str)
            ],
            orchestration_backend=str(payload.get("orchestration_backend", ""))[:64],
            model_capabilities=(
                dict(payload.get("model_capabilities", {}))
                if isinstance(payload.get("model_capabilities", {}), dict)
                else {}
            ),
            workspaces=[
                WorkerWorkspace(
                    workspace_id=str(item["workspace_id"]),
                    name=str(item["name"]),
                    is_git=bool(item.get("is_git", False)),
                )
                for item in payload.get("workspaces", [])
                if isinstance(item, dict)
            ],
        )


class PairingCodeStore:
    def __init__(self, ttl_seconds: int = 600, limit: int = 256):
        self._ttl_seconds = ttl_seconds
        self._limit = limit
        self._codes: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, owner_id: str) -> tuple[str, int]:
        code = "-".join(
            [
                secrets.token_hex(2).upper(),
                secrets.token_hex(2).upper(),
                secrets.token_hex(2).upper(),
                secrets.token_hex(2).upper(),
            ]
        )
        expires_at = time.time() + self._ttl_seconds
        with self._lock:
            self._purge()
            while len(self._codes) >= self._limit:
                self._codes.pop(next(iter(self._codes)))
            self._codes[code] = (canonical_owner_id(owner_id), expires_at)
        return code, self._ttl_seconds

    def consume(self, code: str) -> str:
        normalized = str(code).strip().upper()
        with self._lock:
            self._purge()
            item = self._codes.pop(normalized, None)
        if item is None:
            raise PairingCodeInvalidError("pairing code is invalid or expired")
        return item[0]

    def _purge(self) -> None:
        now = time.time()
        for code in [code for code, (_, expiry) in self._codes.items() if expiry <= now]:
            self._codes.pop(code, None)


class DeviceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        secure_directory(self.root)
        self._lock = threading.RLock()

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def create(self, owner_id: str, name: str) -> tuple[Device, str]:
        owner_id = canonical_owner_id(owner_id)
        device_id = "dev_" + secrets.token_hex(16)
        token = secrets.token_urlsafe(48)
        device = Device(
            device_id=device_id,
            owner_id=owner_id,
            name=name.strip(),
            token_digest=self._token_digest(token),
        )
        with self._lock:
            write_json_atomic(self.root / f"{device_id}.json", device.to_dict())
        return device, token

    def get(self, device_id: str) -> Device:
        if not _DEVICE_ID.fullmatch(str(device_id)):
            raise DeviceNotFoundError(str(device_id))
        path = self.root / f"{device_id}.json"
        try:
            return Device.from_dict(read_json(path))
        except FileNotFoundError:
            raise DeviceNotFoundError(device_id) from None

    def get_for_owner(self, device_id: str, owner_id: str) -> Device:
        device = self.get(device_id)
        if device.owner_id != canonical_owner_id(owner_id):
            raise DeviceNotFoundError(device_id)
        return device

    def authenticate(self, token: str) -> Device:
        if not token or len(token) > 512:
            raise AuthorizationDeniedError("invalid Worker device token")
        digest = self._token_digest(token)
        with self._lock:
            for path in self.root.glob("dev_*.json"):
                device = Device.from_dict(read_json(path))
                if hmac.compare_digest(device.token_digest, digest):
                    return device
        raise AuthorizationDeniedError("invalid Worker device token")

    def list_for_owner(self, owner_id: str) -> list[Device]:
        owner_id = canonical_owner_id(owner_id)
        devices = []
        with self._lock:
            for path in self.root.glob("dev_*.json"):
                device = Device.from_dict(read_json(path))
                if device.owner_id == owner_id:
                    devices.append(device)
        return sorted(devices, key=lambda device: (device.name.lower(), device.device_id))

    def update_presence(
        self,
        device_id: str,
        *,
        model: str,
        model_configured: bool,
        version: str,
        protocol_version: int,
        platform: str,
        architecture: str,
        capabilities: list[str],
        workspaces: list[WorkerWorkspace],
        orchestration_backend: str | None = None,
        model_capabilities: dict | None = None,
    ) -> Device:
        if len(workspaces) > 100:
            raise ValueError("a Worker may register at most 100 workspaces")
        if len({workspace.workspace_id for workspace in workspaces}) != len(workspaces):
            raise ValueError("workspace ids must be unique per Worker")
        with self._lock:
            device = self.get(device_id)
            device.last_seen_at = utc_now()
            device.model = model[:200]
            device.model_configured = bool(model_configured)
            device.version = version[:32]
            device.protocol_version = protocol_version
            device.platform = platform[:32]
            device.architecture = architecture[:32]
            device.capabilities = list(capabilities)
            if orchestration_backend is not None:
                device.orchestration_backend = str(orchestration_backend)[:64]
            if model_capabilities is not None:
                device.model_capabilities = dict(model_capabilities)
            device.workspaces = list(workspaces)
            write_json_atomic(self.root / f"{device_id}.json", device.to_dict())
            return device

    def find_workspace(
        self,
        owner_id: str,
        workspace_id: str,
        *,
        device_id: str | None = None,
    ) -> tuple[Device, WorkerWorkspace] | None:
        devices = (
            [self.get_for_owner(device_id, owner_id)]
            if device_id
            else self.list_for_owner(owner_id)
        )
        for device in devices:
            for workspace in device.workspaces:
                if workspace.workspace_id == workspace_id:
                    return device, workspace
        return None

    def rename(self, device_id: str, owner_id: str, display_name: str) -> Device:
        display_name = str(display_name).strip()
        if not display_name or len(display_name) > 200:
            raise ValueError("invalid device display name")
        with self._lock:
            device = self.get_for_owner(device_id, owner_id)
            device.name = display_name
            write_json_atomic(self.root / f"{device_id}.json", device.to_dict())
            return device

    def revoke(self, device_id: str, owner_id: str) -> None:
        self.get_for_owner(device_id, owner_id)
        with self._lock:
            (self.root / f"{device_id}.json").unlink(missing_ok=True)

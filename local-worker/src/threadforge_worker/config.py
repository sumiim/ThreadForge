"""User-scoped Worker configuration without server or provider secrets in Git."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field
from pathlib import Path


class WorkspacePathError(ValueError):
    """The directory selected by the user cannot be resolved locally."""


class WorkspaceConfigWriteError(RuntimeError):
    """The local Worker configuration could not be persisted."""


_CONFIG_REPLACE_ATTEMPTS = 5
_CONFIG_REPLACE_DELAY_SECONDS = 0.05


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
    machine_fingerprint: str = ""
    workspaces: list[LocalWorkspace] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "server_url": self.server_url,
            "device_id": self.device_id,
            "device_token": _protect_token(self.device_token),
            "device_name": self.device_name,
            "machine_fingerprint": self.machine_fingerprint,
            "workspaces": [workspace.__dict__ for workspace in self.workspaces],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> WorkerConfig:
        return cls(
            server_url=str(payload.get("server_url", "http://127.0.0.1:18000")).rstrip("/"),
            device_id=str(payload.get("device_id", "")),
            device_token=_unprotect_token(str(payload.get("device_token", ""))),
            device_name=str(payload.get("device_name", "")),
            machine_fingerprint=str(payload.get("machine_fingerprint", "")),
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
        # Keep mutable workspace authorization separate from the paired device
        # record. Windows scanners and installers may briefly lock worker.json;
        # selecting a directory must not depend on replacing that file.
        self.workspaces_path = self.root / "workspaces.json"

    def load(self) -> WorkerConfig:
        if not self.path.is_file():
            return WorkerConfig()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Worker config must be a JSON object")
        config = WorkerConfig.from_dict(payload)
        if self.workspaces_path.is_file():
            workspace_payload = json.loads(self.workspaces_path.read_text(encoding="utf-8"))
            if not isinstance(workspace_payload, list):
                raise TypeError("Worker workspace config must be a JSON list")
            config.workspaces = WorkerConfig.from_dict({"workspaces": workspace_payload}).workspaces
        return config

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
            _replace_with_retry(temp_path, self.path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)
            self.path.chmod(0o600)

    def add_workspace(self, config: WorkerConfig, path: str, name: str | None = None) -> LocalWorkspace:
        try:
            root = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError("selected workspace path is unavailable") from exc
        if not root.is_dir():
            raise WorkspacePathError("workspace path must be a directory")
        for existing in config.workspaces:
            if Path(existing.path) == root:
                return existing
        default_name = root.name or root.anchor.rstrip("\\/") or "Workspace"
        workspace = LocalWorkspace(
            workspace_id="ws_" + uuid.uuid4().hex,
            name=(name or default_name).strip() or default_name,
            path=str(root),
        )
        config.workspaces.append(workspace)
        try:
            self.save_workspaces(config)
        except Exception as exc:
            config.workspaces.remove(workspace)
            raise WorkspaceConfigWriteError("failed to save Worker workspace") from exc
        return workspace

    def remove_workspace(self, config: WorkerConfig, workspace_id: str) -> bool:
        remaining = [item for item in config.workspaces if item.workspace_id != workspace_id]
        if len(remaining) == len(config.workspaces):
            return False
        config.workspaces = remaining
        self.save_workspaces(config)
        return True

    def save_workspaces(self, config: WorkerConfig) -> None:
        """Persist only workspace metadata, avoiding the paired device file."""
        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=self.root,
                prefix="workspaces.json.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump([workspace.__dict__ for workspace in config.workspaces], handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_path, self.workspaces_path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)
            self.workspaces_path.chmod(0o600)

    @property
    def model_env_path(self) -> Path:
        return self.root / ".env"

    @property
    def providers_path(self) -> Path:
        return self.root / "providers.json"

    def _read_providers(self) -> dict:
        if not self.providers_path.is_file():
            return {}
        payload = json.loads(self.providers_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("provider config must be a JSON object")
        return payload

    def _write_providers(self, providers: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=self.root,
                prefix="providers.json.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(providers, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_path, self.providers_path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            self.providers_path.chmod(0o600)

    def save_provider(
        self,
        provider_id: str,
        *,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str,
        reasoning_efforts: tuple[str, ...] = (),
        model_efforts: dict[str, list[str]] | None = None,
        max_output_tokens: int = 0,
        context_window: int = 0,
    ) -> None:
        # base_url / api_key 允许为空（编辑时留空表示沿用 Worker 本地已保存值，
        # 或新建时尚未填写）；仅拒超长/换行。model 同样允许为空（尚未发现模型）。
        # 真正跑任务建客户端时，这些字段会被重新校验/回填。
        base_url = _validate_model_value("base_url", base_url, 2048, allow_empty=True)
        api_key = _validate_model_value("api_key", api_key, 8192, allow_empty=True)
        model = _validate_model_value("model", model, 200, allow_empty=True)
        protocol = _validate_provider_protocol(protocol)
        providers = self._read_providers()
        # §2.2 模型×档位矩阵：可选，按模型记各自支持的 reasoning_efforts，
        # 缺省回退到 provider 级 reasoning_efforts（向后兼容）。
        effort_map = model_efforts if isinstance(model_efforts, dict) else {}
        providers[str(provider_id)] = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "protocol": protocol,
            "reasoning_efforts": [str(item).strip() for item in reasoning_efforts if str(item).strip()],
            "model_efforts": {
                str(k): [str(item).strip() for item in (v or []) if str(item).strip()]
                for k, v in effort_map.items()
            },
            "max_output_tokens": int(max_output_tokens or 0),
            "context_window": int(context_window or 0),
        }
        self._write_providers(providers)

    def load_provider(self, provider_id: str) -> dict | None:
        provider = self._read_providers().get(str(provider_id))
        return dict(provider) if isinstance(provider, dict) else None

    def list_providers(self) -> list[dict]:
        return [dict(item) for item in self._read_providers().values()]

    def delete_provider(self, provider_id: str) -> None:
        providers = self._read_providers()
        if str(provider_id) in providers:
            del providers[str(provider_id)]
            self._write_providers(providers)

    def save_model_env(self, *, base_url: str, api_key: str, model: str, model_provider: str = "") -> None:
        base_url = _validate_model_value("base_url", base_url, 2048)
        api_key = _validate_model_value("api_key", api_key, 8192)
        model = _validate_model_value("model", model, 200)
        model_provider = _validate_model_provider(model_provider)
        parsed = urllib.parse.urlsplit(base_url)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or (parsed.scheme == "http" and not loopback)
        ):
            raise ValueError(
                "model API base URL must use HTTPS, except for loopback development"
            )
        payload = (
            "# Managed by ThreadForge Worker Companion. Do not commit this file.\n"
            f"PICO_MODEL_PROVIDER={model_provider}\n"
            f"PICO_OPENAI_API_BASE={base_url}\n"
            f"PICO_OPENAI_API_KEY={api_key}\n"
            f"PICO_OPENAI_MODEL={model}\n"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=self.root,
                prefix=".env.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _secure_secret_file(temp_path)
            temp_path.replace(self.model_env_path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        _secure_secret_file(self.model_env_path)
        self.load_model_env()

    def load_model_env(self) -> dict[str, str]:
        from pico.config import load_project_env

        if not self.model_env_path.is_file():
            return {}
        return load_project_env(self.root, override=True)


def _validate_model_value(name: str, value: str, max_length: int, *, allow_empty: bool = False) -> str:
    value = str(value).strip()
    if (not allow_empty and not value) or len(value) > max_length or "\n" in value or "\r" in value:
        raise ValueError(f"invalid model {name}")
    return value


_ALLOWED_MODEL_PROVIDERS = frozenset({"", "openai", "chat_completions", "anthropic"})


def _validate_model_provider(value: str) -> str:
    value = str(value).strip().lower()
    if value not in _ALLOWED_MODEL_PROVIDERS:
        raise ValueError(f"invalid model_provider: {value!r}")
    return value


_ALLOWED_PROVIDER_PROTOCOLS = frozenset({"openai_compatible", "anthropic", "deepseek", "ollama"})


def _validate_provider_protocol(value: str) -> str:
    value = str(value).strip().lower()
    if value not in _ALLOWED_PROVIDER_PROTOCOLS:
        raise ValueError(f"invalid provider protocol: {value!r}")
    return value


def _replace_with_retry(source: Path, target: Path) -> None:
    """Tolerate short-lived Windows scanner/installer locks on worker.json."""
    for attempt in range(_CONFIG_REPLACE_ATTEMPTS):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == _CONFIG_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_CONFIG_REPLACE_DELAY_SECONDS * (2**attempt))


def _secure_secret_file(path: Path) -> None:
    if sys.platform != "win32":
        path.parent.chmod(0o700)
        path.chmod(0o600)
        return
    import ntsecuritycon
    import win32api
    import win32security

    current_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    admins_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid, None
    )
    dacl = win32security.ACL()
    for sid in (current_sid, system_sid, admins_sid):
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    security = win32security.SECURITY_DESCRIPTOR()
    security.SetSecurityDescriptorDacl(True, dacl, False)
    win32security.SetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION, security
    )


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

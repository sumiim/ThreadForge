"""Server-side configuration (frozen once at startup).

All ``THREADFORGE_*`` settings are validated by pydantic-settings. Provider
configuration is read from ``PICO_OPENAI_*`` environment variables exactly once
at startup; Workspace ``.env`` files are never consulted by the Web path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="THREADFORGE_", extra="ignore", protected_namespaces=())

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path
    workspaces_file: Path
    web_origin: str = "http://127.0.0.1:5173"
    desktop_origin_enabled: bool = False
    approval_timeout_seconds: int = 1800
    approval_preview_max_chars: int = 4000
    model_timeout_seconds: int = 120
    shell_cleanup_grace_seconds: int = 5
    shell_output_max_bytes: int = 1048576
    sse_heartbeat_seconds: int = 15
    sse_queue_size: int = 256
    max_steps: int = 6
    # §7.8.9 阶段 4 收尾（2026-08-18）：DeepSeek 思考模式下 reasoning + 正文
    # 共用 max_tokens 预算。512 太少（effort=max 时思考吃光预算 → 收尾轮空
    # 响应）；2048 在 max 档下仍可能偏紧，取 4096 留足正文余量。
    # 合法范围 64-8192（field_validator 校验）。
    max_new_tokens: int = 4096
    model_temperature: float = 0.2
    task_input_max_chars: int = 20000
    artifact_max_bytes: int = 10485760
    openapi_enabled: bool = True
    log_level: str = "INFO"
    trusted_hosts: list[str] = ["127.0.0.1", "::1", "localhost"]
    instance_owner_id: UUID | None = None
    identity_mode: Literal["single_owner_instance", "github_oauth"] = "single_owner_instance"
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_callback_url: str = "http://127.0.0.1:18000/api/v1/auth/github/callback"
    github_oauth_return_url: str = "http://127.0.0.1:5173/"
    github_owner_login: str = ""
    github_access_policy: Literal["allowlist", "all_authenticated"] = "allowlist"
    github_allowed_logins: list[str] = Field(default_factory=list)
    auth_session_ttl_seconds: int = 604800
    auth_cookie_secure: bool = False
    worker_pairing_ttl_seconds: int = 600
    worker_message_max_bytes: int = 2 * 1024 * 1024
    worker_max_concurrent_tasks: int = 2
    worker_release_dir: Path = Path("worker-releases")
    worker_release_max_bytes: int = 128 * 1024 * 1024
    sandbox_enabled: bool = False
    # Sandbox backend: "os" (OS-native Job Object / setrlimit, no Docker needed)
    # or "docker" (per-command container). Default "os" removes the Docker
    # daemon requirement on the Worker machine.
    sandbox_backend: str = "os"
    # Memory convergence: allow Worker/native to write durable memory into the
    # workspace .pico/memory/ dir and {session}.json. Off by default to keep the
    # "web path never mutates the user workspace" invariant; enable explicitly per
    # deployment when cross-session durable memory is needed.
    durable_memory_enabled: bool = False
    sandbox_image: str = "threadforge-sandbox:latest"
    sandbox_user: str = "65534:65534"
    sandbox_cpu_limit: float = 1.0
    sandbox_memory_limit: str = "512m"
    sandbox_pids_limit: int = 64
    sandbox_network: str = "none"

    @field_validator("trusted_hosts")
    @classmethod
    def _validate_trusted_hosts(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("V1 does not allow wildcard trusted hosts")
        return value

    # Provider config, frozen from PICO_* at startup. model_provider selects
    # which provider the server uses; the other two groups stay dormant.
    model_provider: Literal["openai", "anthropic", "chat_completions"] = "openai"
    pico_openai_api_base: str = "https://www.right.codes/codex/v1"
    pico_openai_api_key: str = ""
    pico_openai_model: str = "gpt-5.4"
    pico_anthropic_api_base: str = "https://api.deepseek.com/anthropic"
    pico_anthropic_api_key: str = ""
    pico_anthropic_model: str = "deepseek-chat"
    pico_chat_completions_api_base: str = "https://api.siliconflow.cn/v1"
    pico_chat_completions_api_key: str = ""
    pico_chat_completions_model: str = "deepseek-ai/DeepSeek-V3.2"

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if value == "0.0.0.0":
            import os

            if os.environ.get("THREADFORGE_DOCKER") != "1":
                raise ValueError("0.0.0.0 is only allowed with THREADFORGE_DOCKER=1")
            return value
        if value not in LOOPBACK_HOSTS:
            raise ValueError("V1 only binds a loopback host")
        return value

    @field_validator("web_origin")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        if value == "*":
            raise ValueError("V1 does not allow wildcard CORS origin")
        return _validated_web_url(value, allow_path=False, label="web_origin")

    @field_validator("github_oauth_callback_url", "github_oauth_return_url")
    @classmethod
    def _validate_oauth_url(cls, value: str) -> str:
        return _validated_web_url(value, allow_path=True, label="OAuth URL")

    @field_validator("github_allowed_logins")
    @classmethod
    def _normalize_allowed_logins(cls, value: list[str]) -> list[str]:
        normalized = []
        for login in value:
            login = login.strip().lower()
            if login and login not in normalized:
                normalized.append(login)
        return normalized

    @field_validator("github_owner_login")
    @classmethod
    def _normalize_owner_login(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("auth_session_ttl_seconds")
    @classmethod
    def _validate_auth_session_ttl(cls, value: int) -> int:
        if not 300 <= value <= 31 * 24 * 60 * 60:
            raise ValueError("auth_session_ttl_seconds must be between 5 minutes and 31 days")
        return value

    @field_validator("worker_pairing_ttl_seconds")
    @classmethod
    def _validate_worker_pairing_ttl(cls, value: int) -> int:
        if not 60 <= value <= 3600:
            raise ValueError("worker_pairing_ttl_seconds must be between 1 minute and 1 hour")
        return value

    @field_validator("worker_message_max_bytes")
    @classmethod
    def _validate_worker_message_max_bytes(cls, value: int) -> int:
        if not 64 * 1024 <= value <= 16 * 1024 * 1024:
            raise ValueError("worker_message_max_bytes must be in 64 KiB - 16 MiB")
        return value

    @field_validator("worker_release_max_bytes")
    @classmethod
    def _validate_worker_release_max_bytes(cls, value: int) -> int:
        if not 1024 * 1024 <= value <= 512 * 1024 * 1024:
            raise ValueError("worker_release_max_bytes must be in 1-512 MiB")
        return value

    @field_validator("worker_max_concurrent_tasks")
    @classmethod
    def _validate_worker_max_concurrent_tasks(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("worker_max_concurrent_tasks must be in 1-64")
        return value

    @model_validator(mode="after")
    def _validate_github_oauth(self) -> Settings:
        if self.identity_mode != "github_oauth":
            return self
        missing = [
            name
            for name, value in {
                "github_oauth_client_id": self.github_oauth_client_id,
                "github_oauth_client_secret": self.github_oauth_client_secret,
                "github_owner_login": self.github_owner_login,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ValueError("github_oauth requires: " + ", ".join(missing))
        if self.github_owner_login not in self.github_allowed_logins:
            self.github_allowed_logins = [self.github_owner_login, *self.github_allowed_logins]
        if (
            self.github_oauth_callback_url.startswith("https://")
            or self.github_oauth_return_url.startswith("https://")
        ) and not self.auth_cookie_secure:
            raise ValueError("HTTPS GitHub OAuth requires THREADFORGE_AUTH_COOKIE_SECURE=true")
        return self

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be in 1-65535")
        return value

    @field_validator("approval_timeout_seconds")
    @classmethod
    def _validate_approval_timeout(cls, value: int) -> int:
        if not 30 <= value <= 86400:
            raise ValueError("approval_timeout_seconds must be in 30-86400")
        return value

    @field_validator("approval_preview_max_chars")
    @classmethod
    def _validate_preview_chars(cls, value: int) -> int:
        if not 256 <= value <= 10000:
            raise ValueError("approval_preview_max_chars must be in 256-10000")
        return value

    @field_validator("model_timeout_seconds")
    @classmethod
    def _validate_model_timeout(cls, value: int) -> int:
        if not 5 <= value <= 300:
            raise ValueError("model_timeout_seconds must be in 5-300")
        return value

    @field_validator("shell_cleanup_grace_seconds")
    @classmethod
    def _validate_shell_grace(cls, value: int) -> int:
        if not 1 <= value <= 30:
            raise ValueError("shell_cleanup_grace_seconds must be in 1-30")
        return value

    @field_validator("shell_output_max_bytes")
    @classmethod
    def _validate_shell_output(cls, value: int) -> int:
        if not 64 * 1024 <= value <= 16 * 1024 * 1024:
            raise ValueError("shell_output_max_bytes must be in 64 KiB - 16 MiB")
        return value

    @field_validator("sse_heartbeat_seconds")
    @classmethod
    def _validate_heartbeat(cls, value: int) -> int:
        if not 5 <= value <= 60:
            raise ValueError("sse_heartbeat_seconds must be in 5-60")
        return value

    @field_validator("sse_queue_size")
    @classmethod
    def _validate_queue_size(cls, value: int) -> int:
        if not 16 <= value <= 4096:
            raise ValueError("sse_queue_size must be in 16-4096")
        return value

    @field_validator("max_steps")
    @classmethod
    def _validate_max_steps(cls, value: int) -> int:
        if not 1 <= value <= 25:
            raise ValueError("max_steps must be in 1-25")
        return value

    @field_validator("max_new_tokens")
    @classmethod
    def _validate_max_tokens(cls, value: int) -> int:
        if not 64 <= value <= 8192:
            raise ValueError("max_new_tokens must be in 64-8192")
        return value

    @field_validator("model_temperature")
    @classmethod
    def _validate_temperature(cls, value: float) -> float:
        if not 0 <= value <= 2:
            raise ValueError("model_temperature must be in 0-2")
        return value

    @field_validator("task_input_max_chars")
    @classmethod
    def _validate_input_chars(cls, value: int) -> int:
        if not 1000 <= value <= 100000:
            raise ValueError("task_input_max_chars must be in 1000-100000")
        return value

    @field_validator("artifact_max_bytes")
    @classmethod
    def _validate_artifact_bytes(cls, value: int) -> int:
        if not 1 <= value <= 100 * 1024 * 1024:
            raise ValueError("artifact_max_bytes must be in 1-100 MiB")
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(LOG_LEVELS)}")
        return value

    def freeze_provider_env(self) -> Settings:
        """Read PICO_* once per provider group. Not persisted into THREADFORGE_*."""
        self.pico_openai_api_base = os.environ.get("PICO_OPENAI_API_BASE", self.pico_openai_api_base).rstrip("/") or self.pico_openai_api_base
        self.pico_openai_api_key = os.environ.get("PICO_OPENAI_API_KEY", self.pico_openai_api_key) or self.pico_openai_api_key
        self.pico_openai_model = os.environ.get("PICO_OPENAI_MODEL", self.pico_openai_model) or self.pico_openai_model
        self.pico_anthropic_api_base = os.environ.get("PICO_ANTHROPIC_API_BASE", self.pico_anthropic_api_base).rstrip("/") or self.pico_anthropic_api_base
        self.pico_anthropic_api_key = os.environ.get("PICO_ANTHROPIC_API_KEY", self.pico_anthropic_api_key) or self.pico_anthropic_api_key
        self.pico_anthropic_model = os.environ.get("PICO_ANTHROPIC_MODEL", self.pico_anthropic_model) or self.pico_anthropic_model
        self.pico_chat_completions_api_base = os.environ.get("PICO_CHAT_COMPLETIONS_API_BASE", self.pico_chat_completions_api_base).rstrip("/") or self.pico_chat_completions_api_base
        self.pico_chat_completions_api_key = os.environ.get("PICO_CHAT_COMPLETIONS_API_KEY", self.pico_chat_completions_api_key) or self.pico_chat_completions_api_key
        self.pico_chat_completions_model = os.environ.get("PICO_CHAT_COMPLETIONS_MODEL", self.pico_chat_completions_model) or self.pico_chat_completions_model
        return self

    def model_configured(self) -> bool:
        if self.model_provider == "anthropic":
            return bool(self.pico_anthropic_api_key)
        if self.model_provider == "chat_completions":
            return bool(self.pico_chat_completions_api_key)
        return bool(self.pico_openai_api_key)

    def provider_model(self) -> str:
        if self.model_provider == "anthropic":
            return self.pico_anthropic_model
        if self.model_provider == "chat_completions":
            return self.pico_chat_completions_model
        return self.pico_openai_model


def _validated_web_url(value: str, *, allow_path: bool, label: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError(f"{label} is invalid")
    if not allow_path and (parsed.path not in {"", "/"} or parsed.query):
        raise ValueError(f"{label} must be an origin without path or query")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"{label} must use HTTPS unless it is loopback")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{label} must use HTTP or HTTPS")
    return value.rstrip("/") if not allow_path else value

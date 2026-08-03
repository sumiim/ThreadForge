"""Server-side configuration (frozen once at startup).

All ``THREADFORGE_*`` settings are validated by pydantic-settings. Provider
configuration is read from ``PICO_OPENAI_*`` environment variables exactly once
at startup; Workspace ``.env`` files are never consulted by the Web path.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
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
    max_new_tokens: int = 512
    model_temperature: float = 0.2
    task_input_max_chars: int = 20000
    artifact_max_bytes: int = 10485760
    openapi_enabled: bool = True
    log_level: str = "INFO"
    trusted_hosts: list[str] = ["127.0.0.1", "::1", "localhost"]

    @field_validator("trusted_hosts")
    @classmethod
    def _validate_trusted_hosts(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("V1 does not allow wildcard trusted hosts")
        return value

    # Provider config, frozen from PICO_OPENAI_* at startup.
    pico_openai_api_base: str = "https://www.right.codes/codex/v1"
    pico_openai_api_key: str = ""
    pico_openai_model: str = "gpt-5.4"

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
        import re as _re

        if value == "*":
            raise ValueError("V1 does not allow wildcard CORS origin")
        if not _re.fullmatch(r"https?://(127\.0\.0\.1|\[::1\]|localhost)(:\d+)?", value):
            raise ValueError("V1 only allows loopback CORS origins")
        return value

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
        """Read PICO_OPENAI_* once. Not persisted into THREADFORGE_*."""
        self.pico_openai_api_base = os.environ.get("PICO_OPENAI_API_BASE", self.pico_openai_api_base).rstrip("/") or self.pico_openai_api_base
        self.pico_openai_api_key = os.environ.get("PICO_OPENAI_API_KEY", self.pico_openai_api_key) or self.pico_openai_api_key
        self.pico_openai_model = os.environ.get("PICO_OPENAI_MODEL", self.pico_openai_model) or self.pico_openai_model
        return self

    def model_configured(self) -> bool:
        return bool(self.pico_openai_api_key)

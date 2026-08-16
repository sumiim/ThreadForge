"""Provider 实体与校验（2.7 供应商管理窗口的数据模型地基）。

§7.6：中央只存非秘密字段；``api_key`` 只写 device 本地，中央只收发 ``has_key``
布尔与脱敏尾串。协议复用 ``pico/providers`` 已有客户端类型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ProviderProtocol(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"


class ProviderState(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


_PROVIDER_ID = re.compile(r"^prv_[a-f0-9]{32}$")
_HTTP_URL = re.compile(r"^https?://")


@dataclass
class Provider:
    provider_id: str
    owner_id: str
    device_id: str
    name: str
    protocol: str
    base_url: str
    model: str = ""
    models: list[str] = field(default_factory=list)
    reasoning_tier: str = "none"
    timeout: int = 45
    concurrency: int = 1
    state: str = ProviderState.ACTIVE.value
    is_default: bool = False
    last_test_at: str = ""
    last_error: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "models": list(self.models),
            "reasoning_tier": self.reasoning_tier,
            "timeout": int(self.timeout),
            "concurrency": int(self.concurrency),
            "state": self.state,
            "is_default": bool(self.is_default),
            "last_test_at": self.last_test_at,
            "last_error": self.last_error,
            "schema_version": int(self.schema_version),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Provider:
        known = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


def validate_provider_payload(payload: dict) -> dict:
    """校验并规范化一份 Provider 载荷；非法时抛 ValueError。"""
    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 200:
        raise ValueError("name must be a non-empty string of at most 200 chars")

    protocol = str(payload.get("protocol", "")).strip()
    if protocol not in {item.value for item in ProviderProtocol}:
        raise ValueError("protocol must be one of openai_compatible/anthropic/deepseek/ollama")

    base_url = str(payload.get("base_url", "")).strip()
    if not _HTTP_URL.match(base_url):
        raise ValueError("base_url must be an http(s) URL")

    timeout = int(payload.get("timeout", 45))
    if not 5 <= timeout <= 600:
        raise ValueError("timeout must be in [5, 600]")

    concurrency = int(payload.get("concurrency", 1))
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be in [1, 16]")

    state = str(payload.get("state", ProviderState.ACTIVE.value)).strip()
    if state not in {item.value for item in ProviderState}:
        raise ValueError("state must be active/disabled/error")

    return {
        "name": name,
        "protocol": protocol,
        "base_url": base_url,
        "model": str(payload.get("model", "")).strip(),
        "models": [str(item).strip() for item in payload.get("models", []) if str(item).strip()],
        "reasoning_tier": str(payload.get("reasoning_tier", "none")).strip(),
        "timeout": timeout,
        "concurrency": concurrency,
        "state": state,
        "is_default": bool(payload.get("is_default", False)),
    }

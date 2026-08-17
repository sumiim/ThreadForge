"""Provider CRUD service（2.7 供应商窗口配置面）。

中央只存非秘密字段；``api_key`` 只在请求里出现用于 ``has_key`` 判断，不落中央。
连接测试 / 模型发现依赖 2.2 的 ModelProviderFactory，暂不在本服务内实现。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from ..domain.entities import utc_now
from ..domain.identity import canonical_owner_id
from ..domain.providers import Provider, validate_provider_payload
from ..infrastructure.sqlite_repositories import SqliteProviderRepository


def _set_default(provider: Provider, value: bool) -> Provider:
    return replace(provider, is_default=value)


class ProviderService:
    def __init__(self, repo: SqliteProviderRepository):
        self._repo = repo

    def list_providers(self, owner_id: str, device_id: str = "") -> list[dict]:
        return [
            p.to_dict()
            for p in self._repo.list(canonical_owner_id(owner_id), device_id)
        ]

    def create_provider(self, owner_id: str, device_id: str, payload: dict) -> dict:
        owner_id = canonical_owner_id(owner_id)
        normalized = validate_provider_payload(payload)
        provider = Provider(
            provider_id="prv_" + uuid.uuid4().hex,
            owner_id=owner_id,
            device_id=device_id,
            **normalized,
        )
        return self._repo.create(provider).to_dict()

    def get_provider(self, provider_id: str, owner_id: str) -> dict:
        return self._repo.get(provider_id, canonical_owner_id(owner_id)).to_dict()

    def get_active_provider(self, owner_id: str, device_id: str = "") -> dict | None:
        owner_id = canonical_owner_id(owner_id)
        for item in self._repo.list(owner_id, device_id):
            if item.is_default:
                return item.to_dict()
        return None

    def update_provider(self, provider_id: str, owner_id: str, patch: dict) -> dict:
        owner_id = canonical_owner_id(owner_id)
        updatable = {
            "name", "protocol", "base_url", "model", "models",
            "reasoning_efforts", "timeout", "concurrency", "state",
        }

        def _apply(provider: Provider) -> Provider:
            for key, value in patch.items():
                if key in updatable and value is not None:
                    setattr(provider, key, value)
            return provider

        return self._repo.update(provider_id, owner_id, _apply).to_dict()

    def delete_provider(self, provider_id: str, owner_id: str) -> None:
        self._repo.delete(provider_id, canonical_owner_id(owner_id))

    def activate_provider(self, provider_id: str, owner_id: str, device_id: str = "") -> dict:
        owner_id = canonical_owner_id(owner_id)
        self._repo.get(provider_id, owner_id)  # 404 if absent
        # 激活只在该设备（或跨全部设备，device_id 为空时）的 Provider 之间切换默认，
        # 不能把别的设备的默认 Provider 一并清掉。
        for item in self._repo.list(owner_id, device_id):
            if item.provider_id != provider_id and item.is_default:
                self._repo.update(item.provider_id, owner_id, lambda p: _set_default(p, False))
        return self._repo.update(
            provider_id, owner_id, lambda p: _set_default(p, True)
        ).to_dict()

    def record_models(
        self, provider_id: str, owner_id: str, models: list[str], *, error: str = ""
    ) -> dict:
        owner_id = canonical_owner_id(owner_id)

        def _apply(provider: Provider) -> Provider:
            provider.models = [str(item).strip() for item in models if str(item).strip()]
            provider.last_test_at = utc_now()
            provider.last_error = str(error or "")
            return provider

        return self._repo.update(provider_id, owner_id, _apply).to_dict()

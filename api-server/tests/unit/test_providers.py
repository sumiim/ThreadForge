import uuid
from dataclasses import replace

import pytest

from threadforge_api.application.provider_service import ProviderService
from threadforge_api.domain.errors import ProviderNotFoundError
from threadforge_api.domain.providers import (
    Provider,
    ProviderProtocol,
    validate_provider_payload,
)
from threadforge_api.infrastructure.sqlite_repositories import SqliteProviderRepository
from threadforge_api.infrastructure.sqlite_store import SqliteStore


def test_provider_round_trips_through_dict():
    provider = Provider(
        provider_id="prv_" + "a" * 32,
        owner_id=str(uuid.uuid4()),
        device_id="dev_" + "b" * 32,
        name="DeepSeek",
        protocol=ProviderProtocol.OPENAI_COMPATIBLE.value,
        base_url="https://api.example.com/v1",
        model="deepseek-chat",
    )
    d = provider.to_dict()
    assert d["name"] == "DeepSeek"
    assert Provider.from_dict(d).base_url == "https://api.example.com/v1"


def test_validate_provider_payload_normalizes_and_rejects():
    ok = validate_provider_payload(
        {
            "name": "  DeepSeek  ",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "timeout": 30,
            "concurrency": 2,
        }
    )
    assert ok["name"] == "DeepSeek"
    assert ok["timeout"] == 30

    with pytest.raises(ValueError, match="protocol"):
        validate_provider_payload(
            {"name": "x", "protocol": "bogus", "base_url": "https://api.example.com"}
        )
    with pytest.raises(ValueError, match="base_url"):
        validate_provider_payload(
            {"name": "x", "protocol": "anthropic", "base_url": "not-a-url"}
        )
    with pytest.raises(ValueError, match="timeout"):
        validate_provider_payload(
            {"name": "x", "protocol": "ollama", "base_url": "https://api.example.com", "timeout": 1}
        )


def test_provider_repository_crud(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    repo = SqliteProviderRepository(store, json_root=tmp_path / "providers")

    provider = Provider(
        provider_id="prv_" + "c" * 32,
        owner_id=str(uuid.uuid4()),
        device_id="dev_" + "d" * 32,
        name="DeepSeek",
        protocol=ProviderProtocol.OPENAI_COMPATIBLE.value,
        base_url="https://api.example.com/v1",
    )
    repo.create(provider)
    assert repo.get(provider.provider_id, provider.owner_id).name == "DeepSeek"
    assert [p.name for p in repo.list(provider.owner_id)] == ["DeepSeek"]

    repo.update(
        provider.provider_id,
        provider.owner_id,
        lambda p: replace(p, name="Renamed"),
    )
    assert repo.get(provider.provider_id, provider.owner_id).name == "Renamed"

    repo.delete(provider.provider_id, provider.owner_id)
    with pytest.raises(ProviderNotFoundError):
        repo.get(provider.provider_id, provider.owner_id)


def test_provider_service_crud_and_activate(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    repo = SqliteProviderRepository(store, json_root=tmp_path / "providers")
    service = ProviderService(repo)
    owner = str(uuid.uuid4())

    created = service.create_provider(owner, "", {
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-secret-not-stored",
    })
    provider_id = created["provider_id"]
    assert created["name"] == "DeepSeek"
    assert "api_key" not in created  # 中央不落密钥

    assert [p["name"] for p in service.list_providers(owner)] == ["DeepSeek"]

    updated = service.update_provider(provider_id, owner, {"name": "Renamed"})
    assert updated["name"] == "Renamed"

    service.activate_provider(provider_id, owner)
    assert service.get_provider(provider_id, owner)["is_default"] is True

    service.delete_provider(provider_id, owner)
    assert service.list_providers(owner) == []


def test_provider_service_create_binds_device_and_activate_scopes_to_device(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    repo = SqliteProviderRepository(store, json_root=tmp_path / "providers")
    service = ProviderService(repo)
    owner = str(uuid.uuid4())
    device_a = "dev_" + "a" * 32
    device_b = "dev_" + "b" * 32

    base = {
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.com/v1",
    }
    created_a = service.create_provider(owner, device_a, base)
    created_b = service.create_provider(owner, device_b, base)

    # 创建时绑定 device_id：get_active_provider 只能按对应 device 查到。
    assert created_a["device_id"] == device_a
    assert created_b["device_id"] == device_b
    assert service.get_active_provider(owner, device_a) is None
    assert service.get_active_provider(owner, "") is None

    # 激活设备 A 的 Provider 时，设备 B 的默认状态不受影响。
    service.activate_provider(created_a["provider_id"], owner, device_a)
    assert service.get_active_provider(owner, device_a)["provider_id"] == created_a["provider_id"]
    assert service.get_active_provider(owner, device_b) is None

    # 激活设备 B 的 Provider 后，A 的默认仍保留。
    service.activate_provider(created_b["provider_id"], owner, device_b)
    assert service.get_active_provider(owner, device_a)["provider_id"] == created_a["provider_id"]
    assert service.get_active_provider(owner, device_b)["provider_id"] == created_b["provider_id"]


def test_provider_service_bind_device_fixes_unbound_legacy_provider(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    repo = SqliteProviderRepository(store, json_root=tmp_path / "providers")
    service = ProviderService(repo)
    owner = str(uuid.uuid4())
    device = "dev_" + "a" * 32

    # 旧版本创建的 provider：device_id 为空（未绑定设备）。
    created = service.create_provider(owner, "", {
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.com/v1",
    })
    provider_id = created["provider_id"]
    assert created["device_id"] == ""
    assert service.get_active_provider(owner, device) is None
    assert [p["provider_id"] for p in service.list_unbound(owner)] == [provider_id]

    # bind_device 后：列 + payload 同步；激活后按设备可查到默认 provider。
    bound = service.bind_device(provider_id, owner, device)
    assert bound["device_id"] == device
    assert service.list_unbound(owner) == []
    service.activate_provider(provider_id, owner, device)
    assert service.get_active_provider(owner, device)["provider_id"] == provider_id

    # 重新加载后绑定仍生效（持久化列）。
    reloaded = service.get_provider(provider_id, owner)
    assert reloaded["device_id"] == device

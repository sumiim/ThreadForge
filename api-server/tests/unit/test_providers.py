import uuid

import pytest

from threadforge_api.domain.providers import (
    Provider,
    ProviderProtocol,
    validate_provider_payload,
)


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

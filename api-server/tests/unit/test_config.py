"""Settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from threadforge_api.config import Settings


def _base(**overrides):
    return {
        "data_dir": "/tmp/d",
        "workspaces_file": "/tmp/w.json",
        **overrides,
    }


def test_defaults_are_valid():
    settings = Settings(**_base())
    assert settings.host == "127.0.0.1"
    assert settings.max_steps == 6
    assert settings.sse_heartbeat_seconds == 15
    assert settings.desktop_origin_enabled is False


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_host_rejected(host):
    with pytest.raises(ValidationError):
        Settings(**_base(host=host))


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 0),
        ("port", 70000),
        ("approval_timeout_seconds", 5),
        ("approval_timeout_seconds", 90000),
        ("approval_preview_max_chars", 10),
        ("model_timeout_seconds", 400),
        ("shell_cleanup_grace_seconds", 0),
        ("shell_output_max_bytes", 1000),
        ("sse_heartbeat_seconds", 100),
        ("sse_queue_size", 1),
        ("max_steps", 0),
        ("max_steps", 100),
        ("max_new_tokens", 10),
        ("model_temperature", 5),
        ("task_input_max_chars", 10),
        ("artifact_max_bytes", 0),
        ("log_level", "verbose"),
    ],
)
def test_out_of_range_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**_base(**{field: value}))


def test_model_config_freezes_from_env(monkeypatch):
    monkeypatch.setenv("PICO_OPENAI_API_BASE", "https://example.test/v1")
    monkeypatch.setenv("PICO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PICO_OPENAI_MODEL", "gpt-test")
    settings = Settings(**_base()).freeze_provider_env()
    assert settings.pico_openai_api_base == "https://example.test/v1"
    assert settings.pico_openai_api_key == "sk-test"
    assert settings.pico_openai_model == "gpt-test"
    assert settings.model_configured() is True


def test_model_not_configured_when_key_missing(monkeypatch):
    monkeypatch.delenv("PICO_OPENAI_API_KEY", raising=False)
    settings = Settings(**_base(pico_openai_api_key="")).freeze_provider_env()
    assert settings.model_configured() is False

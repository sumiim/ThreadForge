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


def test_github_oauth_requires_server_credentials():
    with pytest.raises(ValidationError, match="github_oauth requires"):
        Settings(**_base(identity_mode="github_oauth"))


def test_github_oauth_normalizes_allowlist_and_includes_owner():
    settings = Settings(
        **_base(
            identity_mode="github_oauth",
            github_oauth_client_id="client-id",
            github_oauth_client_secret="client-secret",
            github_owner_login=" Sumiim ",
            github_allowed_logins=["Guest", "guest"],
        )
    )
    assert settings.github_owner_login == "sumiim"
    assert settings.github_allowed_logins == ["sumiim", "guest"]


def test_github_oauth_urls_allow_https_and_reject_remote_http():
    settings = Settings(
        **_base(
            identity_mode="github_oauth",
            github_oauth_client_id="client-id",
            github_oauth_client_secret="client-secret",
            github_owner_login="sumiim",
            github_oauth_callback_url="https://threadforge.example/api/v1/auth/github/callback",
            github_oauth_return_url="https://threadforge.example/",
            web_origin="https://threadforge.example",
            auth_cookie_secure=True,
        )
    )
    assert settings.web_origin == "https://threadforge.example"
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(**_base(github_oauth_return_url="http://example.com/"))


def test_https_github_oauth_requires_secure_cookie():
    with pytest.raises(ValidationError, match="AUTH_COOKIE_SECURE"):
        Settings(
            **_base(
                identity_mode="github_oauth",
                github_oauth_client_id="client-id",
                github_oauth_client_secret="client-secret",
                github_owner_login="sumiim",
                github_oauth_callback_url="https://threadforge.example/api/v1/auth/github/callback",
                github_oauth_return_url="https://threadforge.example/",
            )
        )


def test_github_allowlist_loads_from_json_environment(monkeypatch):
    monkeypatch.setenv("THREADFORGE_IDENTITY_MODE", "github_oauth")
    monkeypatch.setenv("THREADFORGE_GITHUB_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("THREADFORGE_GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("THREADFORGE_GITHUB_OWNER_LOGIN", "sumiim")
    monkeypatch.setenv("THREADFORGE_GITHUB_ALLOWED_LOGINS", '["sumiim","guest"]')
    settings = Settings(**_base())
    assert settings.github_allowed_logins == ["sumiim", "guest"]

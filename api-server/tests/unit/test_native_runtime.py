"""Public classification of Runtime failures."""

from __future__ import annotations

import urllib.error

import pytest
from pico.providers.clients import ModelProviderError
from pico.task_state import STOP_REASON_MODEL_ERROR, STOP_REASON_RUNTIME_ERROR

from threadforge_api.infrastructure.native_runtime import _classify_public_error


def test_http_provider_error_exposes_only_status_code():
    cause = urllib.error.HTTPError(
        "https://provider.example/v1/responses",
        401,
        "unauthorized secret response",
        {},
        None,
    )
    error = RuntimeError("provider body must not be public")
    error.__cause__ = cause

    assert _classify_public_error(error) == ("model_http_401", STOP_REASON_MODEL_ERROR)


def test_model_provider_error_code_wins_over_http_cause():
    cause = urllib.error.HTTPError(
        "https://provider.example/v1/responses",
        401,
        "unauthorized",
        {},
        None,
    )
    error = ModelProviderError("model_auth_error", retryable=False, attempts=1)
    error.__cause__ = cause

    assert _classify_public_error(error) == ("model_auth_error", STOP_REASON_MODEL_ERROR)


@pytest.mark.parametrize(
    "code",
    [
        "model_rate_limited",
        "model_timeout",
        "model_server_error",
        "model_auth_error",
        "model_request_rejected",
        "model_connection_error",
        "model_response_invalid",
        "model_provider_error",
    ],
)
def test_model_provider_error_raised_directly_without_cause(code):
    assert _classify_public_error(ModelProviderError(code)) == (
        code,
        STOP_REASON_MODEL_ERROR,
    )


def test_unlisted_model_provider_error_falls_back_to_http_cause():
    cause = urllib.error.HTTPError(
        "https://provider.example/v1/responses",
        429,
        "rate limited",
        {},
        None,
    )
    error = ModelProviderError("model_custom_code", retryable=True, attempts=1)
    error.__cause__ = cause

    assert _classify_public_error(error) == ("model_http_429", STOP_REASON_MODEL_ERROR)


def test_unknown_error_remains_runtime_error():
    assert _classify_public_error(ValueError("bad state")) == (
        "ValueError",
        STOP_REASON_RUNTIME_ERROR,
    )

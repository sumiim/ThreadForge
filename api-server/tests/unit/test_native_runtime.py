"""Public classification of Runtime failures."""

from __future__ import annotations

import urllib.error

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


def test_unknown_error_remains_runtime_error():
    assert _classify_public_error(ValueError("bad state")) == (
        "ValueError",
        STOP_REASON_RUNTIME_ERROR,
    )

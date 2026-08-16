import pytest
from pydantic import ValidationError

from threadforge_api.api.models import AppendMessageRequest


def test_append_message_request_defaults_wake_true():
    req = AppendMessageRequest(content="please also check tests/")
    assert req.wake is True


def test_append_message_request_accepts_wake_false():
    req = AppendMessageRequest(content="inject this", wake=False)
    assert req.wake is False


def test_append_message_request_rejects_blank_content():
    with pytest.raises(ValidationError):
        AppendMessageRequest(content="   ")

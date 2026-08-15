"""Public request/response Pydantic models."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=200)


class PairWorkerRequest(BaseModel):
    code: str = Field(min_length=8, max_length=64)
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _device_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("device name must not be blank")
        return value


class ConfigureWorkerModelRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=8192)
    model: str = Field(min_length=1, max_length=200)
    model_provider: str = Field(default="", max_length=40)

    @field_validator("model_provider")
    @classmethod
    def _valid_model_provider(cls, value: str) -> str:
        value = str(value).strip().lower()
        allowed = frozenset({"", "openai", "chat_completions", "anthropic"})
        if value not in allowed:
            raise ValueError(f"model_provider must be one of: {', '.join(sorted(allowed))}")
        return value

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or (parsed.scheme == "http" and not loopback)
        ):
            raise ValueError("base_url must use HTTPS, except for loopback development")
        return value

    @field_validator("api_key", "model")
    @classmethod
    def _not_blank_or_multiline(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("value must be a non-empty single line")
        return value


class CreateTaskRequest(BaseModel):
    session_id: str
    input: str = Field(min_length=1, max_length=100000)
    max_steps: int | None = Field(default=None, ge=1, le=25)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "none"

    @field_validator("input")
    @classmethod
    def _input_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]


class RenameEntityRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    expected_updated_at: str | None = Field(default=None, max_length=64)

    @field_validator("display_name")
    @classmethod
    def _display_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value


class TaskQueuedResponse(BaseModel):
    task_id: str
    run_id: str
    session_id: str
    status: str
    events_url: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)
    request_id: str = ""


class ErrorResponse(BaseModel):
    error: ErrorBody

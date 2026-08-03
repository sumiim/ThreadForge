"""Public request/response Pydantic models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=200)


class CreateTaskRequest(BaseModel):
    session_id: str
    input: str = Field(min_length=1, max_length=100000)
    max_steps: int | None = Field(default=None, ge=1, le=25)

    @field_validator("input")
    @classmethod
    def _input_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]


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

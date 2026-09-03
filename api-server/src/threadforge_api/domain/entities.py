"""Task / Approval control-state entities.

These mirror the JSON repository records. The JSON repository is the unique
writer for control state; Run artifacts stay owned by the legacy RunStore.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .enums import ApprovalStatus, TaskStatus
from .identity import canonical_owner_id

SCHEMA_VERSION = 5


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Task:
    task_id: str
    session_id: str
    workspace_id: str
    owner_id: str
    run_id: str
    input: str
    execution_environment: str = "backend_process"
    device_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    max_steps: int = 6
    permission_mode: str = "default"
    model_id: str = ""
    reasoning_effort: str = "none"
    provider_id: str = ""
    # §review 双 provider（2026-09-03）：会话级独立 review provider/model（可选）。
    review_provider_id: str = ""
    review_model_id: str = ""
    review_reasoning_effort: str = "none"
    run_index: list[dict] = field(default_factory=list)
    final_answer: str | None = None
    stop_reason: str | None = None
    error_stage: str = ""
    error_code: str = ""
    error_retryable: bool = False
    error_attempts: int = 0
    pending_approval: dict | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    generation: int = 0
    transition_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        data = dict(data)
        data["status"] = TaskStatus(data.get("status", TaskStatus.QUEUED.value))
        data["owner_id"] = canonical_owner_id(data["owner_id"])
        known = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Approval:
    approval_id: str
    task_id: str
    run_id: str
    owner_id: str
    tool_call_id: str
    tool_name: str
    args_digest: str
    args_preview: dict
    request_digest: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: str | None = None
    created_at: str = field(default_factory=utc_now)
    expires_at: str = ""
    decided_at: str | None = None
    transition_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Approval:
        data = dict(data)
        data["status"] = ApprovalStatus(data.get("status", ApprovalStatus.PENDING.value))
        data["owner_id"] = canonical_owner_id(data["owner_id"])
        known = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

"""Domain enums for the public Task/Approval state machines."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED}


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ExecutionEnvironment(str, Enum):
    BACKEND_PROCESS = "backend_process"
    LOCAL_WORKER = "local_worker"

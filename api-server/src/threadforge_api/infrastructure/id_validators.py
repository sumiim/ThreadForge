"""Validate ID format before it reaches any file‑system path construction."""

from __future__ import annotations

import re

from ..domain.errors import (
    ApprovalNotFoundError,
    RunNotFoundError,
    SessionNotFoundError,
    TaskNotFoundError,
)

# "ses_" / "task_" / "run_" / "apr_" + uuid4().hex (32 hex chars).
_SES_RE = re.compile(r"^ses_[a-f0-9]{32}$")
_TASK_RE = re.compile(r"^task_[a-f0-9]{32}$")
_RUN_RE = re.compile(r"^run_[a-f0-9]{32}$")
_APR_RE = re.compile(r"^apr_[a-f0-9]{32}$")


def validate_session_id(session_id: str) -> None:
    if not _SES_RE.fullmatch(session_id):
        raise SessionNotFoundError(session_id)


def validate_task_id(task_id: str) -> None:
    if not _TASK_RE.fullmatch(task_id):
        raise TaskNotFoundError(task_id)


def validate_run_id(run_id: str) -> None:
    if not _RUN_RE.fullmatch(run_id):
        raise RunNotFoundError(run_id)


def validate_approval_id(approval_id: str) -> None:
    if not _APR_RE.fullmatch(approval_id):
        raise ApprovalNotFoundError(approval_id)

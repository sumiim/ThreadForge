"""Coordinator-owned repair of legacy Run artifacts."""

from __future__ import annotations

from datetime import datetime, timezone

from pico.run_store import RunStore
from pico.security import redact_artifact
from pico.task_state import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_USER_CANCELLED,
    TaskState,
)

from ..domain.enums import TaskStatus


def converge_run_artifacts(data_dir, task, *, status: TaskStatus, stop_reason: str, final_answer: str = "") -> None:
    if not task.run_id:
        return
    store = RunStore(data_dir / "runs")
    try:
        state = TaskState.from_dict(store.load_task_state(task.run_id))
    except (FileNotFoundError, ValueError, TypeError):
        state = TaskState(
            run_id=task.run_id,
            task_id=task.task_id,
            user_request=task.input,
        )
    if status is TaskStatus.COMPLETED:
        state.status = STATUS_COMPLETED
    elif status is TaskStatus.CANCELLED:
        state.status = STATUS_STOPPED
    else:
        state.status = STATUS_FAILED
    state.stop_reason = stop_reason
    state.final_answer = final_answer
    store.start_run(state)
    store.append_trace(
        state,
        {
            "event": "run_interrupted",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": state.status,
            "stop_reason": stop_reason,
        },
    )
    store.write_report(
        state,
        redact_artifact({
            "status": state.status,
            "stop_reason": stop_reason,
            "final_answer": final_answer,
            "task_state": state.to_dict(),
        }),
    )


def run_artifacts_match(data_dir, task) -> bool:
    if not task.run_id:
        return True
    store = RunStore(data_dir / "runs")
    try:
        state = TaskState.from_dict(store.load_task_state(task.run_id))
        report = store.load_report(task.run_id)
    except (OSError, ValueError, TypeError):
        return False
    expected_status = {
        TaskStatus.COMPLETED: STATUS_COMPLETED,
        TaskStatus.CANCELLED: STATUS_STOPPED,
        TaskStatus.FAILED: STATUS_FAILED,
        TaskStatus.INTERRUPTED: STATUS_FAILED,
        TaskStatus.BLOCKED: STATUS_FAILED,
    }.get(task.status)
    return (
        expected_status is not None
        and state.run_id == task.run_id
        and state.task_id == task.task_id
        and state.status == expected_status
        and state.stop_reason == (task.stop_reason or "")
        and state.final_answer == (task.final_answer or "")
        and report.get("status") == expected_status
        and report.get("stop_reason") == (task.stop_reason or "")
        and store.trace_path(task.run_id).is_file()
    )


def terminal_task_from_run(data_dir, task) -> tuple[TaskStatus, str, str] | None:
    """Return a trustworthy public terminal state without modifying artifacts."""
    if not task.run_id:
        return None
    store = RunStore(data_dir / "runs")
    try:
        state = TaskState.from_dict(store.load_task_state(task.run_id))
    except (OSError, ValueError, TypeError):
        return None
    if (
        state.run_id != task.run_id
        or state.task_id != task.task_id
        or state.status == STATUS_RUNNING
    ):
        return None
    if state.status == STATUS_COMPLETED and state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED:
        public_status = TaskStatus.COMPLETED
    elif state.status == STATUS_STOPPED and state.stop_reason == STOP_REASON_USER_CANCELLED:
        public_status = TaskStatus.CANCELLED
    elif state.status in {STATUS_STOPPED, STATUS_FAILED}:
        if state.stop_reason in {"service_restarted", "service_shutdown_timeout"}:
            public_status = TaskStatus.INTERRUPTED
        elif state.stop_reason in {
            "approval_denied",
            "budget_exhausted",
            "convergence_guard_triggered",
            "no_changes_to_review",
            "retry_limit_reached",
            "review_retry_limit_reached",
            "step_limit_reached",
        }:
            public_status = TaskStatus.BLOCKED
        else:
            public_status = TaskStatus.FAILED
    else:
        return None
    return public_status, state.stop_reason, state.final_answer


def repair_terminal_run_artifacts(data_dir, task, recovered: tuple[TaskStatus, str, str]) -> None:
    """Fill missing trace/report from a trusted terminal task_state."""
    _status, stop_reason, final_answer = recovered
    store = RunStore(data_dir / "runs")
    state = TaskState.from_dict(store.load_task_state(task.run_id))
    trace_path = store.trace_path(task.run_id)
    if not trace_path.is_file():
        store.append_trace(
            state,
            {
                "event": "run_recovered",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": state.status,
                "stop_reason": stop_reason,
            },
        )
    report_ok = False
    try:
        report = store.load_report(task.run_id)
        report_ok = report.get("status") == state.status and report.get("stop_reason") == stop_reason
    except (OSError, ValueError, TypeError):
        pass
    if not report_ok:
        store.write_report(
            state,
            redact_artifact(
                {
                    "status": state.status,
                    "stop_reason": stop_reason,
                    "final_answer": final_answer,
                    "task_state": state.to_dict(),
                }
            ),
        )

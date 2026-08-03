"""Task application service: create / query / cancel / approve."""

from __future__ import annotations

import uuid

from pico.security import redact_artifact

from ..domain.entities import Task, utc_now
from ..domain.enums import ExecutionEnvironment, TaskStatus
from ..domain.errors import (
    ActiveTaskExistsError,
    ApprovalNotFoundError,
    InputTooLongError,
    ModelNotConfiguredError,
    TaskRunnerUnavailableError,
)
from ..infrastructure.id_validators import validate_session_id
from ..infrastructure.json_repositories import (
    JsonApprovalRepository,
    JsonTaskRepository,
)
from ..infrastructure.run_reconciliation import converge_run_artifacts
from ..infrastructure.run_store_reader import RunStoreReader
from ..infrastructure.task_runner import RunRequest, TaskRunner
from .session_service import SessionService


class TaskService:
    def __init__(
        self,
        *,
        settings,
        session_service: SessionService,
        task_repo: JsonTaskRepository,
        approval_repo: JsonApprovalRepository,
        runner: TaskRunner,
        publisher,
        run_store_reader: RunStoreReader,
    ):
        self._settings = settings
        self._session_service = session_service
        self._task_repo = task_repo
        self._approval_repo = approval_repo
        self._runner = runner
        self._publisher = publisher
        self._run_store_reader = run_store_reader

    def create_task(self, session_id: str, input_text: str, max_steps: int) -> Task:
        validate_session_id(session_id)
        if not self._runner.is_available():
            raise TaskRunnerUnavailableError("runner is unavailable")
        if not self._settings.model_configured():
            raise ModelNotConfiguredError("model configuration is incomplete")
        if len(input_text) > self._settings.task_input_max_chars:
            raise InputTooLongError(self._settings.task_input_max_chars)
        session = self._session_service.load_raw(session_id)  # 404 session_not_found
        workspace_id = session.get("workspace_id", "")
        input_text = input_text.strip()
        task_id = "task_" + uuid.uuid4().hex
        run_id = "run_" + uuid.uuid4().hex
        task = Task(
            task_id=task_id,
            session_id=session_id,
            workspace_id=workspace_id,
            run_id=run_id,
            input=input_text,
            max_steps=max_steps,
        )
        with self._runner.active_lock:
            if not self._runner.is_available():
                raise TaskRunnerUnavailableError("runner is unavailable")
            if self._runner.is_active():
                raise ActiveTaskExistsError(self._runner.active_task_id() or "")
            self._task_repo.create(task)
            self._publisher.publish(task_id, run_id, "task.queued", {"status": "queued"})
            try:
                self._runner.register(
                    RunRequest(
                        task_id=task_id,
                        run_id=run_id,
                        session_id=session_id,
                        workspace_id=workspace_id,
                        input=input_text,
                        max_steps=max_steps,
                        session_data=session,
                    )
                )
            except Exception:
                audit_ok = False
                try:
                    converge_run_artifacts(
                        self._settings.data_dir,
                        task,
                        status=TaskStatus.FAILED,
                        stop_reason="task_runner_unavailable",
                        final_answer="",
                    )
                    self._task_repo.update(
                        task_id,
                        lambda t: _terminal(t, TaskStatus.FAILED, "task_runner_unavailable", None),
                    )
                    self._publisher.publish(
                        task_id,
                        run_id,
                        "task.failed",
                        {"stop_reason": "task_runner_unavailable"},
                    )
                    audit_ok = True
                except Exception:
                    self._runner.mark_degraded()
                if not audit_ok:
                    self._runner.mark_degraded()
                raise TaskRunnerUnavailableError(
                    "runner unavailable",
                    {"task_id": task_id},
                ) from None
        # Return the Task entity as-persisted (queued) — the Runner will update it to running.
        return task

    def get_task(self, task_id: str) -> dict:
        task = self._task_repo.get(task_id)
        return self._snapshot(task)

    def cancel_task(self, task_id: str) -> dict:
        task = self._task_repo.get(task_id)
        if task.status.terminal:
            return self._snapshot(task)
        self._runner.cancel(task_id)
        return self._snapshot(self._task_repo.get(task_id))

    def resolve_approval(self, task_id: str, approval_id: str, decision: str) -> dict:
        approval = self._approval_repo.get(approval_id)
        if approval.task_id != task_id:
            raise ApprovalNotFoundError(approval_id)
        resolved = self._runner.resolve_approval(approval_id, decision)
        return {
            "approval_id": resolved.approval_id,
            "task_id": resolved.task_id,
            "status": resolved.status.value,
            "decision": resolved.decision,
        }

    # ---- snapshot -------------------------------------------------------------

    def _snapshot(self, task: Task) -> dict:
        progress = self._run_store_reader.read_progress(task.run_id) if task.run_id else {}
        pending = task.pending_approval
        if pending and task.status == TaskStatus.WAITING_FOR_APPROVAL:
            # Defensive: if the approval record was never created, don't
            # advertise a dangling approval_id.
            try:
                self._approval_repo.get(pending["approval_id"])
            except Exception:
                pending = None
        return {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "session_id": task.session_id,
            "workspace_id": task.workspace_id,
            "status": task.status.value,
            "input": redact_artifact(task.input),
            "final_answer": redact_artifact(task.final_answer),
            "stop_reason": task.stop_reason,
            "attempts": progress.get("attempts"),
            "tool_steps": progress.get("tool_steps"),
            "pending_approval": pending,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "execution_environment": ExecutionEnvironment.BACKEND_PROCESS.value,
            "container_sandbox_enabled": False,
        }


def _terminal(task, status: TaskStatus, stop_reason: str, final_answer):
    task.status = status
    task.stop_reason = stop_reason
    task.final_answer = final_answer
    task.pending_approval = None
    task.updated_at = utc_now()
    return task

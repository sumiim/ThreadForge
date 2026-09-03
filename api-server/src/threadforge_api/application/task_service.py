"""Task application service: create / query / cancel / approve."""

from __future__ import annotations

import uuid
from dataclasses import replace

from pico.security import redact_artifact

from ..domain.entities import Task, utc_now
from ..domain.enums import TaskStatus
from ..domain.errors import (
    ActiveTaskExistsError,
    ApprovalNotFoundError,
    AuthorizationDeniedError,
    InputTooLongError,
    ModelCapabilityUnavailableError,
    ModelNotConfiguredError,
    ProviderNotConfiguredError,
    TaskRunnerUnavailableError,
    TaskTerminalError,
    WorkerCapabilityUnavailableError,
    WorkerOfflineError,
)
from ..domain.identity import canonical_owner_id
from ..infrastructure.id_validators import validate_session_id
from ..infrastructure.json_repositories import (
    JsonApprovalRepository,
    JsonTaskRepository,
)
from ..infrastructure.run_reconciliation import converge_run_artifacts
from ..infrastructure.run_store_reader import RunStoreReader
from ..infrastructure.task_runner import RunRequest, TaskRunner
from ..infrastructure.workspace_catalog import WorkspaceNotFoundError
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
        worker_hub=None,
        device_store=None,
        provider_service=None,
    ):
        self._settings = settings
        self._session_service = session_service
        self._task_repo = task_repo
        self._approval_repo = approval_repo
        self._runner = runner
        self._publisher = publisher
        self._run_store_reader = run_store_reader
        self._worker_hub = worker_hub
        self._device_store = device_store
        self._provider_service = provider_service

    def create_task(
        self,
        session_id: str,
        input_text: str,
        max_steps: int,
        owner_id: str,
        *,
        model_id: str | None = None,
        reasoning_effort: str = "none",
        permission_mode: str = "default",
        # §review 双 provider（2026-09-03）：会话级主循环 provider + 独立 review provider/model。
        provider_id: str | None = None,
        review_provider_id: str | None = None,
        review_model_id: str | None = None,
    ) -> Task:
        owner_id = canonical_owner_id(owner_id)
        validate_session_id(session_id)
        if len(input_text) > self._settings.task_input_max_chars:
            raise InputTooLongError(self._settings.task_input_max_chars)
        session = self._session_service.load_raw(session_id, owner_id)  # 404 session_not_found
        input_text = input_text.strip()
        session = self._session_service.initialize_from_first_request(session, input_text)
        workspace_id = session.get("workspace_id", "")
        execution_environment = session.get("execution_environment", "backend_process")
        device_id = session.get("device_id", "")
        selected_model = str(model_id or "").strip()
        selected_effort = str(reasoning_effort or "none").strip().lower()
        active_provider_id = ""
        requested_provider = str(provider_id or "").strip()
        if execution_environment == "local_worker" and self._provider_service is not None:
            device_providers = self._provider_service.list_providers(owner_id, device_id)
            provider_ids = {str(item.get("provider_id", "")) for item in device_providers}
            # §review 双 provider（2026-09-03）：客户端显式传 provider_id 时视为会话级
            # provider 优先使用；否则回退设备 active provider（与 Composer 展示逻辑一致，
            # 保证新建的唯一 Provider 不会以空 provider_id 派发）。
            if requested_provider:
                if requested_provider not in provider_ids:
                    raise ProviderNotConfiguredError(requested_provider, {"device_id": device_id})
                active_provider = self._provider_service.get_provider(requested_provider, owner_id)
            else:
                active_provider = self._provider_service.get_active_provider(owner_id, device_id)
                if active_provider is None:
                    if len(device_providers) == 1:
                        active_provider = device_providers[0]
                    elif len(device_providers) > 1:
                        raise ProviderNotConfiguredError(
                            "multiple Providers are configured for this Worker; select a default Provider",
                            {"device_id": device_id},
                        )
            if active_provider is not None:
                active_provider_id = str(active_provider.get("provider_id", ""))
                selected_model = selected_model or str(active_provider.get("model", ""))
        if execution_environment == "local_worker":
            if self._worker_hub is None or not self._worker_hub.is_online(device_id):
                raise WorkerOfflineError("the selected local Worker is offline")
            device = self._device_store.get_for_owner(device_id, owner_id)
            if not any(workspace.workspace_id == workspace_id for workspace in device.workspaces):
                raise WorkspaceNotFoundError(workspace_id)
            if not device.model_configured:
                raise ModelNotConfiguredError("the selected local Worker has no model configuration")
            selected_model = selected_model or device.model
            models = {
                str(item.get("id", "")): set(item.get("reasoning_efforts", []))
                for item in device.model_capabilities.get("models", [])
                if isinstance(item, dict)
            }
            if not models:
                models = {device.model: {"none"}}
            if selected_model not in models or selected_effort not in models[selected_model]:
                raise ModelCapabilityUnavailableError(
                    "the selected model or reasoning effort is unavailable on this Worker"
                )
        else:
            if self._settings.identity_mode == "github_oauth":
                raise AuthorizationDeniedError(
                    "server-side workspaces are disabled in multi-user mode"
                )
            if not self._runner.is_available():
                raise TaskRunnerUnavailableError("runner is unavailable")
            if not self._settings.model_configured():
                raise ModelNotConfiguredError("model configuration is incomplete")
            provider_model = self._settings.provider_model()
            selected_model = selected_model or provider_model
            if selected_model != provider_model or selected_effort != "none":
                raise WorkerCapabilityUnavailableError(
                    "server-side execution does not advertise the selected model settings"
                )
        task_id = "task_" + uuid.uuid4().hex
        run_id = "run_" + uuid.uuid4().hex
        task = Task(
            task_id=task_id,
            session_id=session_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            input=input_text,
            max_steps=max_steps,
            permission_mode=str(permission_mode or "default").strip(),
            execution_environment=execution_environment,
            device_id=device_id,
            model_id=selected_model,
            reasoning_effort=selected_effort,
            provider_id=active_provider_id,
            review_provider_id=str(review_provider_id or "").strip(),
            review_model_id=str(review_model_id or "").strip(),
        )
        if execution_environment == "local_worker":
            # The plaintext prompt is dispatched from memory. Persist only
            # control state; local Worker sessions own all conversation text.
            self._task_repo.create(replace(task, input=""))
            self._publisher.publish(task_id, run_id, "task.queued", {"status": "queued"})
            try:
                self._worker_hub.dispatch(task, session)
            except Exception:
                self._task_repo.update(
                    task_id,
                    lambda item: _terminal(item, TaskStatus.FAILED, "worker_unavailable", None),
                )
                self._publisher.publish(
                    task_id,
                    run_id,
                    "task.failed",
                    {"stop_reason": "worker_unavailable"},
                )
                raise
            return task
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
                        owner_id=owner_id,
                        input=input_text,
                        max_steps=max_steps,
                        permission_mode=str(permission_mode or "default").strip(),
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

    def get_task(self, task_id: str, owner_id: str) -> dict:
        task = self._task_repo.get_for_owner(task_id, owner_id)
        return self._snapshot(task)

    def cancel_task(self, task_id: str, owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        task = self._task_repo.get_for_owner(task_id, owner_id)
        if task.status.terminal:
            return self._snapshot(task)
        if task.execution_environment == "local_worker":
            self._worker_hub.cancel(task_id)
        else:
            self._runner.cancel(task_id)
        return self._snapshot(self._task_repo.get_for_owner(task_id, owner_id))

    def resolve_approval(self, task_id: str, approval_id: str, decision: str, owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        self._task_repo.get_for_owner(task_id, owner_id)
        approval = self._approval_repo.get_for_owner(approval_id, owner_id)
        if approval.task_id != task_id:
            raise ApprovalNotFoundError(approval_id)
        task = self._task_repo.get_for_owner(task_id, owner_id)
        if task.execution_environment == "local_worker":
            resolved = self._worker_hub.resolve_approval(approval_id, decision)
        else:
            resolved = self._runner.resolve_approval(approval_id, decision)
        return {
            "approval_id": resolved.approval_id,
            "task_id": resolved.task_id,
            "status": resolved.status.value,
            "decision": resolved.decision,
        }

    def append_message(self, task_id: str, content: str, wake: bool, owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        task = self._task_repo.get_for_owner(task_id, owner_id)
        if task.status.terminal:
            raise TaskTerminalError(task_id)
        if task.execution_environment != "local_worker":
            # backend_process（原生）路径已降级为 CLI/评测兼容，运行中追加未接线。
            raise TaskTerminalError(task_id)
        self._worker_hub.send_task_message(task.device_id, task_id, content, wake)
        return {"task_id": task_id, "status": "queued"}

    # ---- snapshot -------------------------------------------------------------

    def _snapshot(self, task: Task) -> dict:
        progress = self._run_store_reader.read_progress(task.run_id) if task.run_id else {}
        if task.execution_environment == "local_worker" and self._worker_hub is not None:
            progress.update(self._worker_hub.ephemeral_agent_progress(task.task_id) or {})
        pending = task.pending_approval
        if pending and task.status == TaskStatus.WAITING_FOR_APPROVAL:
            # Defensive: if the approval record was never created, don't
            # advertise a dangling approval_id.
            try:
                self._approval_repo.get(pending["approval_id"])
            except Exception:
                pending = None
        local_answer = (
            self._worker_hub.ephemeral_final_answer(task.task_id)
            if task.execution_environment == "local_worker" and self._worker_hub is not None
            else None
        )
        return {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "session_id": task.session_id,
            "workspace_id": task.workspace_id,
            "status": task.status.value,
            "input": (
                "" if task.execution_environment == "local_worker" else redact_artifact(task.input)
            ),
            "final_answer": (
                local_answer
                if task.execution_environment == "local_worker"
                else redact_artifact(task.final_answer)
            ),
            "stop_reason": task.stop_reason,
            "error_stage": task.error_stage,
            "error_code": task.error_code,
            "error_retryable": task.error_retryable,
            "error_attempts": task.error_attempts,
            "attempts": progress.get("attempts"),
            "tool_steps": progress.get("tool_steps"),
            "phase": progress.get("phase", ""),
            "next_step": progress.get("next_step", ""),
            "checklist": progress.get("checklist", []),
            "done_when": progress.get("done_when", []),
            "completed_items": progress.get("completed_items", []),
            "read_files": progress.get("read_files", 0),
            "max_tool_steps": progress.get("max_tool_steps", 0),
            "max_read_files": progress.get("max_read_files", 0),
            "max_total_steps": progress.get("max_total_steps", 0),
            "pending_approval": pending,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "execution_environment": task.execution_environment,
            "device_id": task.device_id,
            "model_id": task.model_id,
            "reasoning_effort": task.reasoning_effort,
            "run_index": list(task.run_index),
            "container_sandbox_enabled": bool(self._settings.sandbox_enabled),
        }


def _terminal(task, status: TaskStatus, stop_reason: str, final_answer):
    task.status = status
    task.stop_reason = stop_reason
    task.final_answer = final_answer
    task.pending_approval = None
    task.updated_at = utc_now()
    return task

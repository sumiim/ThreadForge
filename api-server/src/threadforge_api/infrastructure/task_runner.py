"""Single active TaskRunner on a dedicated daemon thread."""

from __future__ import annotations

import threading
from copy import copy
from dataclasses import dataclass

from pico.approval import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStrategy,
    strategy_for_mode,
)
from pico.run_store import RunStore
from pico.security import redact_artifact
from pico.task_state import (
    STATUS_COMPLETED,
    STATUS_STOPPED,
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_PROCESS_CLEANUP_FAILED,
    STOP_REASON_RUNTIME_ERROR,
    STOP_REASON_USER_CANCELLED,
)

from ..domain.enums import TaskStatus
from ..domain.errors import (
    ActiveTaskExistsError,
    PersistenceUnavailableError,
    TaskRunnerUnavailableError,
)
from .approval_gate import ApprovalGate, RunContext
from .cancellation import CancellationToken
from .event_publisher import EventPublisher
from .execution_boundary import ExecutionBoundary
from .fenced_run_store import FencedRunStore
from .json_repositories import JsonTaskRepository, StaleGenerationError
from .native_runtime import NativeRuntimeAdapter
from .run_gate import RunGate
from .run_reconciliation import converge_run_artifacts, run_artifacts_match


@dataclass(frozen=True)
class RunRequest:
    task_id: str
    run_id: str
    session_id: str
    workspace_id: str
    owner_id: str
    input: str
    max_steps: int
    session_data: dict
    permission_mode: str = "default"


class WebApprovalStrategy(ApprovalStrategy):
    """Web strategy: delegate each precise tool call to the ApprovalGate."""

    def __init__(self, gate: ApprovalGate, run: RunContext):
        self._gate = gate
        self._run = run

    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        return self._gate.request(
            run=self._run,
            tool_call_id=request.tool_call_id,
            tool_name=request.name,
            args=request.args,
        )


class TaskRunner:
    def __init__(
        self,
        *,
        settings,
        workspace_catalog,
        session_store,
        task_repo: JsonTaskRepository,
        approval_gate: ApprovalGate,
        broker,
        publisher: EventPublisher,
        model_client_factory,
        on_degraded=None,
        isolation=None,
    ):
        self._settings = settings
        self._workspace_catalog = workspace_catalog
        self._session_store = session_store
        self._task_repo = task_repo
        self._approval_gate = approval_gate
        self._broker = broker
        self._publisher = publisher
        self._model_client_factory = model_client_factory
        self._on_degraded = on_degraded
        self._isolation = isolation
        self._degraded = False
        self._shutting_down = False
        self.active_lock = threading.RLock()
        self._active_task_id: str | None = None
        self._contexts: dict[str, RunContext] = {}

    def is_active(self) -> bool:
        return self._active_task_id is not None

    def is_degraded(self) -> bool:
        return self._degraded

    def is_available(self) -> bool:
        return not self._degraded and not self._shutting_down

    def mark_degraded(self) -> None:
        self._degraded = True
        if self._on_degraded is not None:
            self._on_degraded()

    def active_task_id(self) -> str | None:
        return self._active_task_id

    def get_context(self, task_id: str) -> RunContext | None:
        return self._contexts.get(task_id)

    def register(self, request: RunRequest) -> RunContext:
        """Atomically claim the active bit and start the daemon Runner thread."""
        with self.active_lock:
            if not self.is_available():
                raise TaskRunnerUnavailableError("runner is unavailable")
            if self._active_task_id is not None:
                raise ActiveTaskExistsError(self._active_task_id)
            gate = RunGate()
            token = CancellationToken()
            run = RunContext(
                task_id=request.task_id,
                run_id=request.run_id,
                owner_id=request.owner_id,
                gate=gate,
                token=token,
            )
            self._contexts[request.task_id] = run
            self._active_task_id = request.task_id
            try:
                thread = threading.Thread(
                    target=self._worker,
                    args=(request, run),
                    name=f"run-{request.task_id}",
                    daemon=True,
                )
                thread.start()
            except Exception:
                self._contexts.pop(request.task_id, None)
                self._active_task_id = None
                raise TaskRunnerUnavailableError("failed to start runner thread") from None
        return run

    # ---- worker --------------------------------------------------------------

    def _worker(self, request: RunRequest, run: RunContext) -> None:
        gate = run.gate
        with gate:
            if run.token.is_cancelled():
                # Cancelled before the worker started: converge straight to cancelled.
                try:
                    self._persist_terminal(request, run, TaskStatus.CANCELLED, "user_cancelled", "")
                    run.gate.close()
                    self._publisher.publish(
                        request.task_id,
                        request.run_id,
                        "task.cancelled",
                        {"final_answer": ""},
                        phase="final",
                        status="cancelled",
                        summary="user_cancelled",
                    )
                except StaleGenerationError:
                    pass
                except Exception:
                    self.mark_degraded()
                self._release_active(request.task_id)
                return
            try:
                self._task_repo.update(
                    request.task_id,
                    lambda task: _set_status(task, TaskStatus.RUNNING),
                    expected_generation=gate.generation,
                )
                self._publisher.publish(
                    request.task_id, request.run_id, "task.started", {}, phase="system", status="running"
                )
            except StaleGenerationError:
                self._release_active(request.task_id)
                return
            except Exception:
                # Disk full / OSError / etc — persist FAILED to disk.
                # If the FAILED write also fails, keep the active bit — the system
                # is in an unknown state and must not accept a new task.
                fail_write_ok = False
                try:
                    self._persist_terminal(request, run, TaskStatus.FAILED, "persistence_error", "")
                    fail_write_ok = True
                except Exception:
                    pass
                if fail_write_ok:
                    run.gate.close()
                    self._publisher.publish(
                        request.task_id,
                        request.run_id,
                        "task.failed",
                        {"stop_reason": "persistence_error"},
                        phase="final",
                        status="failed",
                        summary="persistence_error",
                    )
                    self._release_active(request.task_id)
                else:
                    self.mark_degraded()
                # else: active bit stays held — ready must return 503 until restart
                return

        try:
            hooks = ExecutionBoundary(
                publisher=self._publisher,
                task_id=request.task_id,
                run_id=request.run_id,
                gate=gate,
                token=run.token,
            )
            strategy = strategy_for_mode(
                request.permission_mode,
                WebApprovalStrategy(self._approval_gate, run),
            )
            model_client = self._model_client_factory()
            fenced_store = FencedRunStore(
                RunStore(self._settings.data_dir / "runs"),
                gate,
            )
            workspace_entry = self._workspace_catalog.recheck(request.workspace_id)
            adapter = NativeRuntimeAdapter(
                settings=self._settings,
                workspace_entry=workspace_entry,
                session_data=request.session_data,
                session_store=self._session_store,
                fenced_run_store=fenced_store,
                model_client=model_client,
                approval_strategy=strategy,
                token=run.token,
                execution_hooks=hooks,
                publisher=self._publisher,
                task_id=request.task_id,
                run_id=request.run_id,
                max_steps=request.max_steps,
                isolation=self._isolation,
            )
            run.adapter = adapter
        except Exception as exc:
            # Runner init failure — but don't mask cancellation.
            if run.token.is_cancelled():
                task_state_status = TaskStatus.CANCELLED
                terminal_stop_reason = "user_cancelled"
            else:
                task_state_status = TaskStatus.FAILED
                terminal_stop_reason = (
                    "sandbox_unavailable" if _is_sandbox_error(exc) else "task_runner_unavailable"
                )
            terminal_write_ok = False
            try:
                self._persist_terminal(request, run, task_state_status, terminal_stop_reason, "")
                terminal_write_ok = True
                run.gate.close()
                self._publisher.publish(
                    request.task_id,
                    request.run_id,
                    "task.cancelled" if task_state_status == TaskStatus.CANCELLED else "task.failed",
                    {"final_answer": ""},
                    phase="final",
                    status=task_state_status.value,
                    summary=terminal_stop_reason,
                )
            except Exception:
                pass
            if terminal_write_ok:
                self._release_active(request.task_id)
            else:
                self.mark_degraded()
            return
        try:
            adapter.run(request.input)
        except Exception:
            import traceback

            traceback.print_exc()  # DEBUG: surface the real failure (was silently swallowed)
        finally:
            ok = False
            try:
                self._converge_terminal(request, run)
                ok = True
            except Exception:
                pass
            if ok:
                self._release_active(request.task_id)
            else:
                self.mark_degraded()
            # else: active bit stays held — the system is in an unknown state

    def _converge_terminal(self, request: RunRequest, run: RunContext) -> None:
        task_state = getattr(getattr(run.adapter, "_pico", None), "current_task_state", None)
        status = getattr(task_state, "status", "")
        stop_reason = getattr(task_state, "stop_reason", "")
        final_answer = getattr(task_state, "final_answer", "") or ""
        if run.token.is_cancelled():
            public_status = TaskStatus.CANCELLED
            terminal_event = "task.cancelled"
            stop_reason = STOP_REASON_USER_CANCELLED
            final_answer = ""
        elif status == STATUS_COMPLETED and stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED:
            public_status = TaskStatus.COMPLETED
            terminal_event = "task.completed"
        elif status == STATUS_STOPPED and stop_reason == STOP_REASON_USER_CANCELLED:
            public_status = TaskStatus.CANCELLED
            terminal_event = "task.cancelled"
        else:
            stop_reason = stop_reason or STOP_REASON_RUNTIME_ERROR
            if stop_reason in {"service_restarted", "service_shutdown_timeout"}:
                public_status = TaskStatus.INTERRUPTED
                terminal_event = "task.interrupted"
            elif stop_reason in {
                "approval_denied",
                "budget_exhausted",
                "convergence_guard_triggered",
                "no_changes_to_review",
                "retry_limit_reached",
                "review_retry_limit_reached",
                "step_limit_reached",
            }:
                public_status = TaskStatus.BLOCKED
                terminal_event = "task.blocked"
            else:
                public_status = TaskStatus.FAILED
                terminal_event = "task.failed"
        if stop_reason == STOP_REASON_PROCESS_CLEANUP_FAILED:
            run.process_cleanup_succeeded = False
            self.mark_degraded()
        if not run.process_cleanup_succeeded:
            public_status = TaskStatus.FAILED
            stop_reason = STOP_REASON_PROCESS_CLEANUP_FAILED
            final_answer = ""
            terminal_event = "task.failed"
        if self._task_repo.get(request.task_id).pending_approval is not None:
            self._approval_gate.cancel_pending(run)
        self._persist_terminal(request, run, public_status, stop_reason, final_answer)
        # Fence every Runtime-owned writer before exposing the terminal event.
        # The terminal Task/Run artifacts are already durable at this point.
        run.gate.close()
        if public_status is TaskStatus.COMPLETED:
            self._publisher.publish(
                request.task_id,
                request.run_id,
                "message.completed",
                {"text": redact_artifact(final_answer)},
                phase="final",
                status="completed",
            )
        self._publisher.publish(
            request.task_id,
            request.run_id,
            terminal_event,
            {"final_answer": redact_artifact(final_answer) if isinstance(final_answer, str) else ""},
            phase="final",
            status=public_status.value,
            summary=stop_reason,
        )

    def _persist_terminal(
        self,
        request: RunRequest,
        run: RunContext,
        status: TaskStatus,
        stop_reason: str,
        final_answer: str,
    ) -> None:
        with run.gate:
            if run.gate.closed:
                raise StaleGenerationError()
            task = self._task_repo.get(request.task_id)
            expected = copy(task)
            _set_terminal(expected, status, stop_reason, final_answer)
            if not run_artifacts_match(self._settings.data_dir, expected):
                converge_run_artifacts(
                    self._settings.data_dir,
                    task,
                    status=status,
                    stop_reason=stop_reason,
                    final_answer=final_answer,
                )
            self._task_repo.update(
                request.task_id,
                lambda current: _set_terminal(current, status, stop_reason, final_answer),
                expected_generation=run.gate.generation,
            )

    def _release_active(self, task_id: str) -> None:
        with self.active_lock:
            self._contexts.pop(task_id, None)
            if self._active_task_id == task_id:
                self._active_task_id = None
        self._approval_gate.release_gate(task_id)

    # ---- control-plane -------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Request cancellation. Returns True if a live Run was signalled."""
        run = self._contexts.get(task_id)
        if run is None:
            return False
        persistence_error = None
        with run.gate:
            try:
                current = self._task_repo.get(task_id)
                if not current.status.terminal and current.status is not TaskStatus.CANCEL_REQUESTED:
                    self._task_repo.update(
                        task_id,
                        lambda task: _set_status(task, TaskStatus.CANCEL_REQUESTED),
                        expected_generation=run.gate.generation,
                    )
                    self._publisher.publish(
                        run.task_id,
                        run.run_id,
                        "task.cancel_requested",
                        {},
                        phase="final",
                        status="cancel_requested",
                    )
            except StaleGenerationError:
                pass
            except Exception as exc:
                self.mark_degraded()
                persistence_error = exc
            run.token.cancel()
        approval_error = None
        try:
            self._approval_gate.cancel_pending(run)
        except Exception as exc:
            self.mark_degraded()
            approval_error = exc
        finally:
            if not run.terminate_shell():
                self.mark_degraded()
        if approval_error is not None:
            raise PersistenceUnavailableError("failed to persist approval cancellation") from approval_error
        if persistence_error is not None:
            raise PersistenceUnavailableError("failed to persist cancellation") from persistence_error
        return True

    def resolve_approval(self, approval_id: str, decision: str):
        return self._approval_gate.apply_decision(approval_id, decision)

    def shutdown(self) -> None:
        """Cancel the active run, wait for cleanup, write service_shutdown_timeout on expiry."""
        import time as _time

        with self.active_lock:
            self._shutting_down = True
            task_ids = list(self._contexts.keys())
        for task_id in task_ids:
            try:
                self.cancel(task_id)
            except Exception:
                pass
        deadline = _time.monotonic() + self._settings.model_timeout_seconds + self._settings.shell_cleanup_grace_seconds
        for task_id in task_ids:
            run = self._contexts.get(task_id)
            if run is None:
                continue
            remaining = max(0, deadline - _time.monotonic())
            if run.token.is_cancelled():
                # Runner is converging — give it remaining time.
                while remaining > 0 and self._contexts.get(task_id) is not None:
                    _time.sleep(0.1)
                    remaining = max(0, deadline - _time.monotonic())
        with self.active_lock:
            task_ids = list(self._contexts.keys())
        for task_id in task_ids:
            run = self._contexts.get(task_id)
            if run is None:
                continue
            run.gate.close()
            try:
                task = self._task_repo.get(task_id)
                converge_run_artifacts(
                    self._settings.data_dir,
                    task,
                    status=TaskStatus.INTERRUPTED,
                    stop_reason="service_shutdown_timeout",
                )
                self._task_repo.update(
                    task_id,
                    lambda task: _set_terminal(task, TaskStatus.INTERRUPTED, "service_shutdown_timeout", ""),
                )
                self._publisher.publish(
                    task_id,
                    run.run_id,
                    "task.interrupted",
                    {"stop_reason": "service_shutdown_timeout"},
                    phase="final",
                    status="interrupted",
                    summary="service_shutdown_timeout",
                )
                self._release_active(task_id)
            except Exception:
                self.mark_degraded()


def _is_sandbox_error(exc: Exception) -> bool:
    try:
        from threadforge_sandbox import SandboxError

        return isinstance(exc, SandboxError)
    except Exception:
        return False


def _set_status(task, status: TaskStatus):
    task.status = status
    return task


def _set_terminal(task, status: TaskStatus, stop_reason: str, final_answer: str):
    task.status = status
    task.stop_reason = stop_reason or None
    task.final_answer = final_answer or None
    task.pending_approval = None
    return task

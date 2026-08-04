"""Persistent, cancellable approval gate for the Web backend.

The wait is predicate-based on the persisted approval status (never on notify
counts), so a decision that lands before the wait starts is observed
immediately. Approval resolution and task cancellation serialize on the same
RunGate so "approved while cancel_requested" can never be linearized.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from pico.approval import ApprovalOutcome
from pico.execution_hooks import RunCancelled

from ..domain.entities import Approval, canonical_json, utc_now
from ..domain.enums import ApprovalStatus, TaskStatus
from ..domain.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalStaleError,
    PersistenceUnavailableError,
)
from .json_repositories import JsonApprovalRepository, JsonTaskRepository
from .recovery_journal import RecoveryJournal


class RunContext:
    """Everything the TaskRunner owns for one active Run."""

    def __init__(self, task_id: str, run_id: str, owner_id: str, gate, token):
        self.task_id = task_id
        self.run_id = run_id
        self.owner_id = owner_id
        self.gate = gate
        self.token = token
        self.adapter = None  # set by the worker once the Runtime is built
        self.process_cleanup_succeeded = True

    def terminate_shell(self) -> bool:
        if self.adapter is not None:
            self.process_cleanup_succeeded = bool(self.adapter.terminate_shell())
        return self.process_cleanup_succeeded


def _now_utc() -> str:
    return utc_now()


class ApprovalGate:
    def __init__(
        self,
        *,
        approval_repo: JsonApprovalRepository,
        task_repo: JsonTaskRepository,
        publisher,
        hmac_key: bytes,
        timeout_seconds: int,
        preview_max_chars: int,
        redact_artifact: Callable[[dict], dict],
        recovery_journal: RecoveryJournal,
        on_degraded: Callable[[], None] | None = None,
    ):
        self._approval_repo = approval_repo
        self._task_repo = task_repo
        self._publisher = publisher
        self._key = hmac_key
        self._timeout = int(timeout_seconds)
        self._preview_max_chars = int(preview_max_chars)
        self._redact = redact_artifact
        self._journal = recovery_journal
        self._on_degraded = on_degraded
        self._conditions: dict[str, threading.Condition] = {}
        self._task_gates: dict[str, object] = {}
        self._cond_lock = threading.Lock()

    def set_degraded_callback(self, callback: Callable[[], None]) -> None:
        self._on_degraded = callback

    def _mark_degraded(self) -> None:
        if self._on_degraded is not None:
            self._on_degraded()

    # ---- helpers -----------------------------------------------------------

    def _digest(self, args: dict) -> str:
        return hmac.new(self._key, canonical_json(args), hashlib.sha256).hexdigest()

    def _preview(self, args: dict) -> dict:
        redacted = self._redact(dict(args))
        if len(canonical_json(redacted)) <= self._preview_max_chars:
            return redacted
        import json

        return {"_truncated": True, "text": json.dumps(redacted, ensure_ascii=False)[: self._preview_max_chars]}

    def _condition(self, approval_id: str) -> threading.Condition:
        with self._cond_lock:
            cond = self._conditions.get(approval_id)
            if cond is None:
                cond = threading.Condition()
                self._conditions[approval_id] = cond
            return cond

    def _drop_condition(self, approval_id: str) -> None:
        with self._cond_lock:
            self._conditions.pop(approval_id, None)

    def _gate_for(self, task_id: str):
        with self._cond_lock:
            return self._task_gates.get(task_id)

    def release_gate(self, task_id: str) -> None:
        """Drop the gate reference so completed tasks do not leak memory."""
        with self._cond_lock:
            self._task_gates.pop(task_id, None)

    # ---- request (Runner thread) --------------------------------------------

    def request(self, *, run: RunContext, tool_call_id: str, tool_name: str, args: dict) -> ApprovalOutcome:
        approval_id = "apr_" + uuid.uuid4().hex
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self._timeout)).isoformat().replace("+00:00", "Z")
        approval = Approval(
            approval_id=approval_id,
            task_id=run.task_id,
            run_id=run.run_id,
            owner_id=run.owner_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_digest=self._digest(args),
            args_preview=self._preview(args),
            expires_at=expires_at,
        )
        with self._cond_lock:
            self._task_gates[run.task_id] = run.gate
        with run.gate:
            if run.token.is_cancelled():
                raise RunCancelled()
            try:
                transition_id = self._journal.begin(
                    "approval_request",
                    task_id=run.task_id,
                    approval_id=approval_id,
                )
                approval.transition_id = transition_id
                self._task_repo.update(
                    run.task_id,
                    lambda task: _set_pending_approval(
                        task,
                        approval_id,
                        tool_call_id,
                        tool_name,
                        approval.args_preview,
                        approval.created_at,
                        approval.expires_at,
                        transition_id,
                    ),
                    expected_generation=run.gate.generation,
                )
                self._approval_repo.create(approval)
                self._journal.commit(transition_id)
            except Exception as exc:
                rollback_ok = self._rollback_request(run, approval_id)
                if rollback_ok and "transition_id" in locals():
                    try:
                        self._journal.commit(transition_id)
                    except Exception:
                        rollback_ok = False
                if not rollback_ok:
                    self._mark_degraded()
                raise PersistenceUnavailableError("failed to persist approval request") from exc
            self._publisher.publish(
                run.task_id,
                run.run_id,
                "approval.required",
                {
                    "approval_id": approval_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "args_preview": approval.args_preview,
                    "created_at": approval.created_at,
                    "expires_at": expires_at,
                },
            )
        cond = self._condition(approval_id)
        try:
            outcome = self._wait(approval_id, run, expires_at, cond)
        finally:
            self._drop_condition(approval_id)
        if outcome is ApprovalOutcome.APPROVED:
            with run.gate:
                if run.token.is_cancelled():
                    return ApprovalOutcome.CANCELLED
                current = self._approval_repo.get(approval_id)
                if not hmac.compare_digest(self._digest(args), current.args_digest):
                    self._resolve_locked(approval_id, run, ApprovalStatus.REJECTED, "digest_mismatch")
                    return ApprovalOutcome.REJECTED
        elif outcome is ApprovalOutcome.EXPIRED:
            # Persist expiry, restore Task, and publish so the audit is complete.
            with run.gate:
                if run.token.is_cancelled():
                    return ApprovalOutcome.CANCELLED
                current = self._approval_repo.get(approval_id)
                if current.status is not ApprovalStatus.PENDING:
                    return ApprovalOutcome(current.status.value)
                self._resolve_locked(approval_id, run, ApprovalStatus.EXPIRED, "expired")
        return outcome

    def _wait(self, approval_id: str, run: RunContext, expires_at: str, cond: threading.Condition) -> ApprovalOutcome:
        deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        with cond:
            while True:
                current = self._approval_repo.get(approval_id)
                if current.status != ApprovalStatus.PENDING:
                    return ApprovalOutcome(current.status.value)
                if run.token.is_cancelled():
                    return ApprovalOutcome.CANCELLED
                remaining = deadline - time.time()
                if remaining <= 0:
                    return ApprovalOutcome.EXPIRED
                cond.wait(timeout=min(remaining, 1.0))

    def _resolve_locked(self, approval_id: str, run: RunContext, status: ApprovalStatus, decision: str) -> bool:
        """Persist Approval outcome + Task running, publish, notify. Caller holds run.gate.

        Writes Task first, then Approval. A failed second write is compensated
        back to WAITING/PENDING. If compensation cannot be proven, the service
        is degraded and refuses new work.
        """
        prior_pending = self._task_repo.get(run.task_id).pending_approval
        try:
            transition_id = self._journal.begin(
                "approval_resolution",
                task_id=run.task_id,
                approval_id=approval_id,
            )
            self._task_repo.update(
                run.task_id,
                lambda task: _clear_pending_approval(task, TaskStatus.RUNNING, transition_id),
                expected_generation=run.gate.generation,
            )
            self._approval_repo.update(
                approval_id,
                lambda a: _set_decided(a, status, decision, _now_utc(), transition_id),
            )
            self._journal.commit(transition_id)
        except Exception as exc:
            rollback_ok = self._rollback_resolution(run, approval_id, prior_pending)
            if rollback_ok and "transition_id" in locals():
                try:
                    self._journal.commit(transition_id)
                except Exception:
                    rollback_ok = False
            if not rollback_ok:
                self._mark_degraded()
            raise PersistenceUnavailableError("failed to persist approval decision") from exc
        self._publisher.publish(
            run.task_id,
            run.run_id,
            "approval.resolved",
            {"approval_id": approval_id, "status": status.value, "decision": decision},
        )
        with self._cond_lock:
            cond = self._conditions.get(approval_id)
        if cond is not None:
            with cond:
                cond.notify_all()
        return True

    # ---- decision (API thread) ---------------------------------------------

    def apply_decision(self, approval_id: str, decision: str) -> Approval:
        approval = self._approval_repo.get(approval_id)
        if approval.status != ApprovalStatus.PENDING:
            if approval.status.value == decision:
                return approval
            raise ApprovalAlreadyResolvedError()
        expires_at = datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00")).timestamp()
        if time.time() > expires_at:
            raise ApprovalExpiredError()
        gate = self._gate_for(approval.task_id)
        if gate is None:
            raise ApprovalStaleError()
        status = ApprovalStatus.APPROVED if decision == "approved" else ApprovalStatus.REJECTED
        run = RunContext(
            task_id=approval.task_id,
            run_id=approval.run_id,
            owner_id=approval.owner_id,
            gate=gate,
            token=None,
        )
        with gate:
            # Re-read under gate: prevent two concurrent decisions from both succeeding.
            current_approval = self._approval_repo.get(approval_id)
            if current_approval.status != ApprovalStatus.PENDING:
                if current_approval.status.value == decision:
                    return self._approval_repo.get(approval_id)
                raise ApprovalAlreadyResolvedError()
            current_task = self._task_repo.get(approval.task_id)
            if current_task.status in {
                TaskStatus.CANCEL_REQUESTED,
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
                TaskStatus.COMPLETED,
            }:
                raise ApprovalStaleError()
            self._resolve_locked(approval_id, run, status, decision)
        return self._approval_repo.get(approval_id)

    def cancel_pending(self, run: RunContext) -> None:
        with run.gate:
            for approval in self._approval_repo.list_pending_for_task(run.task_id):
                self._approval_repo.update(
                    approval.approval_id,
                    lambda a, _id=approval.approval_id: _set_decided(a, ApprovalStatus.CANCELLED, "cancelled", _now_utc()),
                )
                self._publisher.publish(
                    run.task_id,
                    run.run_id,
                    "approval.resolved",
                    {"approval_id": approval.approval_id, "status": "cancelled", "decision": "cancelled"},
                )
                with self._cond_lock:
                    cond = self._conditions.get(approval.approval_id)
                if cond is not None:
                    with cond:
                        cond.notify_all()

    def _rollback_request(self, run: RunContext, approval_id: str) -> bool:
        from ..domain.errors import ApprovalNotFoundError

        try:
            self._approval_repo.get(approval_id)
        except ApprovalNotFoundError:
            approval_ok = True
        except Exception:
            approval_ok = False
        else:
            try:
                self._approval_repo.update(
                    approval_id,
                    lambda a: _set_decided(a, ApprovalStatus.CANCELLED, "persistence_error", _now_utc()),
                )
                approval_ok = True
            except Exception:
                approval_ok = False
        try:
            self._task_repo.update(
                run.task_id,
                lambda task: _clear_pending_approval(task, TaskStatus.RUNNING),
                expected_generation=run.gate.generation,
            )
            task_ok = True
        except Exception:
            task_ok = False
        return approval_ok and task_ok

    def _rollback_resolution(self, run: RunContext, approval_id: str, pending: dict | None) -> bool:
        try:
            self._approval_repo.update(approval_id, _restore_pending_approval)
            approval_ok = True
        except Exception:
            approval_ok = False
        try:
            self._task_repo.update(
                run.task_id,
                lambda task: _restore_waiting_task(task, pending),
                expected_generation=run.gate.generation,
            )
            task_ok = True
        except Exception:
            task_ok = False
        return approval_ok and task_ok


def _set_pending_approval(
    task,
    approval_id,
    tool_call_id,
    tool_name,
    args_preview,
    created_at,
    expires_at,
    transition_id=None,
):
    task.status = TaskStatus.WAITING_FOR_APPROVAL
    task.pending_approval = {
        "approval_id": approval_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "args_preview": args_preview,
        "created_at": created_at,
        "expires_at": expires_at,
    }
    task.transition_id = transition_id
    return task


def _clear_pending_approval(task, next_status, transition_id=None):
    task.status = next_status
    task.pending_approval = None
    task.transition_id = transition_id
    return task


def _set_decided(approval, status: ApprovalStatus, decision: str, decided_at: str, transition_id=None):
    approval.status = status
    approval.decision = decision
    approval.decided_at = decided_at
    approval.transition_id = transition_id
    return approval


def _restore_pending_approval(approval):
    approval.status = ApprovalStatus.PENDING
    approval.decision = None
    approval.decided_at = None
    approval.transition_id = None
    return approval


def _restore_waiting_task(task, pending):
    task.status = TaskStatus.WAITING_FOR_APPROVAL
    task.pending_approval = pending
    task.transition_id = None
    return task

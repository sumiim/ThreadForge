"""Central control-plane bridge for outbound local Worker connections."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket
from pico.security import redact_artifact

from ..domain.entities import Approval, canonical_json, utc_now
from ..domain.enums import ApprovalStatus, TaskStatus
from ..domain.errors import (
    ActiveTaskExistsError,
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalStaleError,
    PersistenceUnavailableError,
    WorkerOfflineError,
    WorkerProtocolError,
)
from .device_store import Device, DeviceStore, WorkerWorkspace
from .run_reconciliation import converge_run_artifacts

_PUBLIC_WORKER_EVENTS = {
    "model.started",
    "model.completed",
    "tool.requested",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "policy.violation",
}
_WORKSPACE_ID = re.compile(r"^ws_[a-f0-9]{32}$")


@dataclass
class WorkerConnection:
    device: Device
    websocket: WebSocket
    outbox: asyncio.Queue
    ready: bool = False


class WorkerHub:
    def __init__(
        self,
        *,
        loop,
        settings,
        device_store: DeviceStore,
        task_repo,
        approval_repo,
        session_store,
        publisher,
    ):
        self._loop = loop
        self._settings = settings
        self._devices = device_store
        self._task_repo = task_repo
        self._approval_repo = approval_repo
        self._session_store = session_store
        self._publisher = publisher
        self._connections: dict[str, WorkerConnection] = {}
        self._active_by_device: dict[str, str] = {}
        self._device_by_task: dict[str, str] = {}
        self._lock = threading.RLock()
        # The digest only needs to remain stable for the lifetime of an active
        # Worker run. Active runs are failed during restart recovery, so a
        # fresh unpredictable key is safer than a value derived from a path.
        self._approval_key = os.urandom(32)

    async def connect(self, device: Device, websocket: WebSocket) -> WorkerConnection:
        await websocket.accept()
        connection = WorkerConnection(
            device=device,
            websocket=websocket,
            outbox=asyncio.Queue(maxsize=256),
        )
        replaced_task_id = ""
        with self._lock:
            previous = self._connections.get(device.device_id)
            self._connections[device.device_id] = connection
            if previous is not None:
                replaced_task_id = self._active_by_device.pop(device.device_id, "")
                if replaced_task_id:
                    self._device_by_task.pop(replaced_task_id, None)
        if replaced_task_id:
            self._fail_task(replaced_task_id, "worker_reconnected")
        if previous is not None:
            await previous.websocket.close(code=4001, reason="device reconnected")
        return connection

    async def sender(self, connection: WorkerConnection) -> None:
        while True:
            message = await connection.outbox.get()
            if message is None:
                return
            await connection.websocket.send_json(message)

    async def disconnect(self, connection: WorkerConnection) -> None:
        task_id = ""
        with self._lock:
            if self._connections.get(connection.device.device_id) is connection:
                self._connections.pop(connection.device.device_id, None)
                task_id = self._active_by_device.pop(connection.device.device_id, "")
                if task_id:
                    self._device_by_task.pop(task_id, None)
        if task_id:
            self._fail_task(task_id, "worker_disconnected")
        try:
            connection.outbox.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def is_online(self, device_id: str) -> bool:
        with self._lock:
            connection = self._connections.get(device_id)
            return connection is not None and connection.ready

    def online_ids(self, owner_id: str) -> set[str]:
        owned = {device.device_id for device in self._devices.list_for_owner(owner_id)}
        with self._lock:
            return {
                device_id
                for device_id in owned
                if (connection := self._connections.get(device_id)) is not None and connection.ready
            }

    def revoke(self, device_id: str, owner_id: str) -> None:
        self._devices.get_for_owner(device_id, owner_id)
        task_id = ""
        with self._lock:
            connection = self._connections.pop(device_id, None)
            task_id = self._active_by_device.pop(device_id, "")
            if task_id:
                self._device_by_task.pop(task_id, None)
        self._devices.revoke(device_id, owner_id)
        if task_id:
            self._fail_task(task_id, "worker_revoked")
        if connection is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(connection.websocket.close(code=4003, reason="device revoked"))
            )

    def dispatch(self, task, session: dict) -> None:
        with self._lock:
            connection = self._connections.get(task.device_id)
            if connection is None or not connection.ready:
                raise WorkerOfflineError("the selected local Worker is offline")
            if (
                connection.device.owner_id != task.owner_id
                or session.get("owner_id") != task.owner_id
                or session.get("workspace_id") != task.workspace_id
                or session.get("device_id") != task.device_id
                or not any(
                    workspace.workspace_id == task.workspace_id
                    for workspace in connection.device.workspaces
                )
            ):
                raise WorkerProtocolError("task, device and workspace ownership do not match")
            active = self._active_by_device.get(task.device_id)
            if active:
                raise ActiveTaskExistsError(active)
            self._active_by_device[task.device_id] = task.task_id
            self._device_by_task[task.task_id] = task.device_id
        try:
            self._task_repo.update(task.task_id, lambda item: _set_status(item, TaskStatus.RUNNING))
            self._publisher.publish(task.task_id, task.run_id, "task.started", {})
            self._send(
                task.device_id,
                {
                    "type": "task.start",
                    "task": {
                        "task_id": task.task_id,
                        "run_id": task.run_id,
                        "session_id": task.session_id,
                        "workspace_id": task.workspace_id,
                        "input": task.input,
                        "max_steps": task.max_steps,
                        "session": session,
                        "settings": {
                            "max_new_tokens": self._settings.max_new_tokens,
                            "model_timeout_seconds": self._settings.model_timeout_seconds,
                            "shell_output_max_bytes": self._settings.shell_output_max_bytes,
                            "shell_cleanup_grace_seconds": self._settings.shell_cleanup_grace_seconds,
                        },
                    },
                },
            )
        except Exception:
            self._release(task.task_id, task.device_id)
            raise

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            device_id = self._device_by_task.get(task_id)
            if not device_id:
                return False
            task = self._task_repo.get(task_id)
            if not task.status.terminal:
                self._cancel_pending_approvals(task, "cancelled")
                self._task_repo.update(task_id, _set_cancel_requested)
                self._publisher.publish(task_id, task.run_id, "task.cancel_requested", {})
            self._send(device_id, {"type": "task.cancel", "task_id": task_id})
            return True

    def resolve_approval(self, approval_id: str, decision: str) -> Approval:
        with self._lock:
            approval = self._approval_repo.get(approval_id)
            if approval.status != ApprovalStatus.PENDING:
                if approval.decision == decision:
                    return approval
                raise ApprovalAlreadyResolvedError()
            expiry = datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expiry:
                raise ApprovalExpiredError()
            device_id = self._device_by_task.get(approval.task_id)
            connection = self._connections.get(device_id or "")
            if not device_id or connection is None or not connection.ready:
                raise ApprovalStaleError()
            task = self._task_repo.get(approval.task_id)
            if task.status != TaskStatus.WAITING_FOR_APPROVAL:
                raise ApprovalStaleError()
            status = ApprovalStatus.APPROVED if decision == "approved" else ApprovalStatus.REJECTED
            try:
                self._approval_repo.update(
                    approval_id,
                    lambda item: _set_approval_decision(item, status, decision),
                )
                self._task_repo.update(approval.task_id, lambda item: _clear_approval(item))
            except Exception as exc:
                try:
                    self._approval_repo.update(approval_id, _restore_pending_approval)
                except Exception:
                    pass
                raise PersistenceUnavailableError("failed to persist Worker approval decision") from exc
            self._publisher.publish(
                approval.task_id,
                approval.run_id,
                "approval.resolved",
                {"approval_id": approval_id, "status": status.value, "decision": decision},
            )
            self._send(
                device_id,
                {
                    "type": "approval.decision",
                    "task_id": approval.task_id,
                    "approval_id": approval_id,
                    "tool_call_id": approval.tool_call_id,
                    "decision": decision,
                    "args_digest": approval.request_digest,
                },
            )
            return self._approval_repo.get(approval_id)

    async def handle(self, connection: WorkerConnection, message: dict) -> None:
        message_type = str(message.get("type", ""))
        if message_type == "hello":
            await self._handle_hello(connection, message)
        elif message_type == "event":
            self._handle_event(connection, message)
        elif message_type == "approval.requested":
            self._handle_approval(connection, message)
        elif message_type == "terminal":
            self._handle_terminal(connection, message)
        elif message_type == "heartbeat":
            self._send(connection.device.device_id, {"type": "heartbeat.ack"})
        else:
            raise WorkerProtocolError("unknown Worker message type")

    async def _handle_hello(self, connection: WorkerConnection, message: dict) -> None:
        raw_workspaces = message.get("workspaces", [])
        if not isinstance(raw_workspaces, list):
            raise WorkerProtocolError("workspaces must be a list")
        workspaces = []
        for item in raw_workspaces:
            if not isinstance(item, dict):
                raise WorkerProtocolError("workspace record must be an object")
            workspace_id = str(item.get("workspace_id", ""))
            name = str(item.get("name", "")).strip()
            if not _WORKSPACE_ID.fullmatch(workspace_id) or not name or len(name) > 200:
                raise WorkerProtocolError("invalid Worker workspace metadata")
            workspaces.append(WorkerWorkspace(workspace_id, name, bool(item.get("is_git", False))))
        connection.device = self._devices.update_presence(
            connection.device.device_id,
            model=str(message.get("model", "")),
            model_configured=bool(message.get("model_configured", False)),
            workspaces=workspaces,
        )
        connection.ready = True
        self._send(
            connection.device.device_id,
            {"type": "hello.ack", "device_id": connection.device.device_id, "server_time": utc_now()},
        )

    def _assigned_task(self, connection: WorkerConnection, task_id: str):
        with self._lock:
            if self._device_by_task.get(task_id) != connection.device.device_id:
                raise WorkerProtocolError("task is not assigned to this Worker")
        task = self._task_repo.get(task_id)
        if task.owner_id != connection.device.owner_id:
            raise WorkerProtocolError("task owner does not match Worker owner")
        return task

    def _handle_event(self, connection: WorkerConnection, message: dict) -> None:
        task_id = str(message.get("task_id", ""))
        event_type = str(message.get("event_type", ""))
        task = self._assigned_task(connection, task_id)
        if event_type not in _PUBLIC_WORKER_EVENTS:
            raise WorkerProtocolError("Worker event type is not public")
        data = message.get("data", {})
        if not isinstance(data, dict):
            raise WorkerProtocolError("Worker event data must be an object")
        if task.status is TaskStatus.CANCEL_REQUESTED:
            return
        self._publisher.publish(task_id, task.run_id, event_type, _sanitize_event_data(event_type, data))

    def _handle_approval(self, connection: WorkerConnection, message: dict) -> None:
        task_id = str(message.get("task_id", ""))
        task = self._assigned_task(connection, task_id)
        tool_call_id = str(message.get("tool_call_id", ""))
        tool_name = str(message.get("tool_name", ""))
        args = message.get("args", {})
        if not tool_call_id or len(tool_call_id) > 200 or not tool_name or len(tool_name) > 100:
            raise WorkerProtocolError("invalid approval identity")
        if not isinstance(args, dict):
            raise WorkerProtocolError("approval args must be an object")
        encoded_args = canonical_json(args)
        request_digest = hashlib.sha256(encoded_args).hexdigest()
        if not hmac.compare_digest(str(message.get("args_digest", "")), request_digest):
            raise WorkerProtocolError("approval args digest does not match")
        approval_id = "apr_" + uuid.uuid4().hex
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self._settings.approval_timeout_seconds)
        ).isoformat().replace("+00:00", "Z")
        preview = redact_artifact(dict(args))
        encoded_preview = canonical_json(preview)
        if len(encoded_preview) > self._settings.approval_preview_max_chars:
            preview = {"_truncated": True, "text": encoded_preview[: self._settings.approval_preview_max_chars].decode("utf-8", errors="ignore")}
        approval = Approval(
            approval_id=approval_id,
            task_id=task_id,
            run_id=task.run_id,
            owner_id=task.owner_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_digest=hmac.new(self._approval_key, encoded_args, hashlib.sha256).hexdigest(),
            args_preview=preview,
            request_digest=request_digest,
            expires_at=expires_at,
        )
        with self._lock:
            current = self._task_repo.get(task_id)
            if current.status is TaskStatus.CANCEL_REQUESTED:
                return
            if current.status != TaskStatus.RUNNING or current.pending_approval is not None:
                raise WorkerProtocolError("task cannot accept an approval request")
            try:
                self._task_repo.update(task_id, lambda item: _set_pending_approval(item, approval))
                self._approval_repo.create(approval)
            except Exception as exc:
                try:
                    self._task_repo.update(task_id, lambda item: _clear_approval(item))
                except Exception:
                    pass
                raise PersistenceUnavailableError("failed to persist Worker approval request") from exc
        self._publisher.publish(
            task_id,
            task.run_id,
            "approval.required",
            {
                "approval_id": approval_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "args_preview": preview,
                "created_at": approval.created_at,
                "expires_at": expires_at,
            },
        )
        self._send(
            connection.device.device_id,
            {
                "type": "approval.registered",
                "task_id": task_id,
                "tool_call_id": tool_call_id,
                "approval_id": approval_id,
            },
        )

    def _handle_terminal(self, connection: WorkerConnection, message: dict) -> None:
        with self._lock:
            self._handle_terminal_locked(connection, message)

    def _handle_terminal_locked(self, connection: WorkerConnection, message: dict) -> None:
        task_id = str(message.get("task_id", ""))
        task = self._assigned_task(connection, task_id)
        status_text = str(message.get("status", "failed"))
        status = {
            "completed": TaskStatus.COMPLETED,
            "cancelled": TaskStatus.CANCELLED,
            "failed": TaskStatus.FAILED,
        }.get(status_text)
        if status is None:
            raise WorkerProtocolError("invalid terminal status")
        stop_reason = str(message.get("stop_reason", ""))[:200] or "runtime_error"
        cancel_won = task.status is TaskStatus.CANCEL_REQUESTED and status is TaskStatus.COMPLETED
        if cancel_won:
            status = TaskStatus.CANCELLED
            stop_reason = "user_cancelled"
        final_answer = "" if cancel_won else redact_artifact(str(message.get("final_answer", "")))
        session = message.get("session")
        if isinstance(session, dict):
            self._merge_session(task, session)
        self._cancel_pending_approvals(task, stop_reason)
        converge_run_artifacts(
            self._settings.data_dir,
            task,
            status=status,
            stop_reason=stop_reason,
            final_answer=final_answer,
        )
        self._task_repo.update(
            task_id,
            lambda item: _set_terminal(item, status, stop_reason, final_answer),
        )
        terminal_event = {
            TaskStatus.COMPLETED: "task.completed",
            TaskStatus.CANCELLED: "task.cancelled",
            TaskStatus.FAILED: "task.failed",
        }[status]
        if status is TaskStatus.COMPLETED:
            self._publisher.publish(task_id, task.run_id, "message.completed", {"text": final_answer})
        self._publisher.publish(task_id, task.run_id, terminal_event, {"final_answer": final_answer, "stop_reason": stop_reason})
        self._release(task_id, connection.device.device_id)

    def _merge_session(self, task, incoming: dict) -> None:
        current = self._session_store.load(task.session_id)
        if (
            incoming.get("id") != task.session_id
            or current.get("owner_id") != task.owner_id
            or current.get("workspace_id") != task.workspace_id
            or current.get("device_id") != task.device_id
            or incoming.get("workspace_id") != task.workspace_id
        ):
            raise WorkerProtocolError("Worker returned an invalid session")
        expected_types = {
            "history": list,
            "memory": dict,
            "checkpoints": dict,
            "runtime_identity": dict,
            "resume_state": dict,
        }
        for key, expected_type in expected_types.items():
            if key in incoming:
                if not isinstance(incoming[key], expected_type):
                    raise WorkerProtocolError(f"Worker session field {key} has an invalid type")
                current[key] = incoming[key]
        current["updated_at"] = utc_now()
        self._session_store.save(current)

    def _fail_task(self, task_id: str, reason: str) -> None:
        with self._lock:
            try:
                task = self._task_repo.get(task_id)
                if task.status.terminal:
                    return
                self._cancel_pending_approvals(task, reason)
                converge_run_artifacts(
                    self._settings.data_dir,
                    task,
                    status=TaskStatus.FAILED,
                    stop_reason=reason,
                    final_answer="",
                )
                self._task_repo.update(
                    task_id,
                    lambda item: _set_terminal(item, TaskStatus.FAILED, reason, ""),
                )
                self._publisher.publish(
                    task_id,
                    task.run_id,
                    "task.failed",
                    {"stop_reason": reason, "final_answer": ""},
                )
            except Exception:
                return

    def _cancel_pending_approvals(self, task, decision: str) -> None:
        for approval in self._approval_repo.list_pending_for_task(task.task_id):
            self._approval_repo.update(
                approval.approval_id,
                lambda item: _set_approval_decision(
                    item,
                    ApprovalStatus.CANCELLED,
                    decision,
                ),
            )
            self._publisher.publish(
                task.task_id,
                task.run_id,
                "approval.resolved",
                {
                    "approval_id": approval.approval_id,
                    "status": ApprovalStatus.CANCELLED.value,
                    "decision": decision,
                },
            )

    def _release(self, task_id: str, device_id: str) -> None:
        with self._lock:
            self._device_by_task.pop(task_id, None)
            if self._active_by_device.get(device_id) == task_id:
                self._active_by_device.pop(device_id, None)

    def _send(self, device_id: str, message: dict) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._settings.worker_message_max_bytes:
            raise WorkerProtocolError("server message exceeds Worker size limit")
        with self._lock:
            connection = self._connections.get(device_id)
        if connection is None:
            raise WorkerOfflineError("local Worker is offline")

        def enqueue() -> None:
            try:
                connection.outbox.put_nowait(message)
            except asyncio.QueueFull:
                asyncio.create_task(connection.websocket.close(code=4002, reason="Worker outbox overflow"))

        self._loop.call_soon_threadsafe(enqueue)


def _set_status(task, status: TaskStatus):
    task.status = status
    task.updated_at = utc_now()
    return task


def _set_cancel_requested(task):
    task.status = TaskStatus.CANCEL_REQUESTED
    task.pending_approval = None
    task.updated_at = utc_now()
    return task


def _set_pending_approval(task, approval: Approval):
    task.status = TaskStatus.WAITING_FOR_APPROVAL
    task.pending_approval = {
        "approval_id": approval.approval_id,
        "tool_call_id": approval.tool_call_id,
        "tool_name": approval.tool_name,
        "args_preview": approval.args_preview,
        "created_at": approval.created_at,
        "expires_at": approval.expires_at,
    }
    task.updated_at = utc_now()
    return task


def _set_approval_decision(approval, status: ApprovalStatus, decision: str):
    approval.status = status
    approval.decision = decision
    approval.decided_at = utc_now()
    return approval


def _restore_pending_approval(approval):
    approval.status = ApprovalStatus.PENDING
    approval.decision = None
    approval.decided_at = None
    return approval


def _clear_approval(task):
    task.status = TaskStatus.RUNNING
    task.pending_approval = None
    task.updated_at = utc_now()
    return task


def _set_terminal(task, status: TaskStatus, stop_reason: str, final_answer: str):
    task.status = status
    task.stop_reason = stop_reason
    task.final_answer = final_answer or None
    task.pending_approval = None
    task.updated_at = utc_now()
    return task


def _sanitize_event_data(event_type: str, data: dict) -> dict:
    """Keep Worker events useful without accepting arbitrary local data."""
    if event_type == "model.completed":
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            return {"usage": {}}
        return {
            "usage": {
                key: value
                for key, value in usage.items()
                if key in {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
                and isinstance(value, int)
                and value >= 0
            }
        }
    if event_type == "model.started":
        return {}
    safe = {
        "tool_call_id": str(data.get("tool_call_id", ""))[:200],
        "tool_name": str(data.get("tool_name", ""))[:100],
    }
    if event_type in {"tool.completed", "tool.failed"}:
        safe["tool_status"] = str(data.get("tool_status", ""))[:50]
        safe["tool_error_code"] = str(data.get("tool_error_code", ""))[:100]
        paths = data.get("affected_paths", [])
        safe["affected_paths"] = [
            path
            for item in paths[:100] if isinstance(paths, list)
            if (path := _safe_relative_path(item)) is not None
        ] if isinstance(paths, list) else []
    if event_type == "policy.violation":
        safe["policy_code"] = str(data.get("policy_code", ""))[:100]
    return redact_artifact(safe)


def _safe_relative_path(value) -> str | None:
    text = str(value).replace("\\", "/")
    if not text or len(text) > 500 or text.startswith(("/", "~/")):
        return None
    head = text.split("/", 1)[0]
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if ":" in head or ".." in parts:
        return None
    return "/".join(parts)

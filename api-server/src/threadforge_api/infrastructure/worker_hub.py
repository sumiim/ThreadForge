"""Central control-plane bridge for outbound local Worker connections."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket
from pico.features.memory import default_memory_state
from pico.security import redact_artifact

from ..domain.entities import Approval, canonical_json, utc_now
from ..domain.enums import ApprovalStatus, TaskStatus
from ..domain.errors import (
    ActiveTaskExistsError,
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalStaleError,
    NotFoundError,
    PersistenceUnavailableError,
    WorkerCapabilityUnavailableError,
    WorkerCommandFailedError,
    WorkerCommandPendingError,
    WorkerOfflineError,
    WorkerProtocolError,
)
from .device_store import Device, DeviceStore, WorkerWorkspace

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
_SESSION_ID = re.compile(r"^ses_[a-f0-9]{32}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_WORKSPACE_SELECTION_TTL_SECONDS = 120
_EPHEMERAL_ANSWER_TTL_SECONDS = 600


@dataclass
class WorkerConnection:
    device: Device
    websocket: WebSocket
    outbox: asyncio.Queue
    ready: bool = False


@dataclass
class WorkspaceSelectionRequest:
    request_id: str
    owner_id: str
    device_id: str
    created_at: str
    expires_at: str
    status: str = "pending"
    workspace_id: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "device_id": self.device_id,
            "status": self.status,
            "workspace_id": self.workspace_id or None,
            "error": self.error or None,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class PendingWorkerRequest:
    owner_id: str
    device_id: str
    future: asyncio.Future
    subject_id: str = ""
    created_at: float = 0.0


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
        self._workspace_requests: dict[str, WorkspaceSelectionRequest] = {}
        self._history_requests: dict[str, PendingWorkerRequest] = {}
        self._model_requests: dict[str, PendingWorkerRequest] = {}
        self._terminal_answers: dict[str, tuple[float, str]] = {}
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
                self._fail_workspace_requests_locked(device.device_id, "worker_reconnected")
                self._fail_pending_requests_locked(device.device_id, "worker_reconnected")
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
                self._fail_workspace_requests_locked(
                    connection.device.device_id, "worker_disconnected"
                )
                self._fail_pending_requests_locked(
                    connection.device.device_id, "worker_disconnected"
                )
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

    def ephemeral_final_answer(self, task_id: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._expire_terminal_answers_locked(now)
            item = self._terminal_answers.get(task_id)
            return item[1] if item is not None else None

    def _remember_terminal_answer(self, task_id: str, answer: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._expire_terminal_answers_locked(now)
            self._terminal_answers[task_id] = (now, answer)
            while len(self._terminal_answers) > 512:
                self._terminal_answers.pop(next(iter(self._terminal_answers)))

    def _expire_terminal_answers_locked(self, now: float) -> None:
        expired = [
            task_id
            for task_id, (created_at, _) in self._terminal_answers.items()
            if now - created_at >= _EPHEMERAL_ANSWER_TTL_SECONDS
        ]
        for task_id in expired:
            self._terminal_answers.pop(task_id, None)

    async def request_session_history(
        self,
        *,
        device_id: str,
        session_id: str,
        message_limit: int,
        owner_id: str,
    ) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        request_id = "hist_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._history_requests,
            request_id=request_id,
            device_id=device_id,
            owner_id=owner_id,
            capability="local_history",
            subject_id=session_id,
        )
        timed_out = False
        try:
            self._send(
                device_id,
                {
                    "type": "session.history.get",
                    "request_id": request_id,
                    "session_id": session_id,
                    "message_limit": message_limit,
                },
            )
            result = await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError as exc:
            timed_out = True
            raise WorkerOfflineError("local history request timed out") from exc
        finally:
            if not timed_out:
                with self._lock:
                    self._history_requests.pop(request_id, None)
        if result.get("status") != "completed":
            raise WorkerCommandFailedError(
                "local session history is unavailable",
                {"reason": result.get("error", "history_unavailable")},
            )
        return result

    async def configure_model(
        self,
        *,
        device_id: str,
        owner_id: str,
        base_url: str,
        api_key: str,
        model: str,
    ) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        request_id = "model_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._model_requests,
            request_id=request_id,
            device_id=device_id,
            owner_id=owner_id,
            capability="model_configuration",
        )
        timed_out = False
        try:
            self._send(
                device_id,
                {
                    "type": "model.configure",
                    "request_id": request_id,
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": model,
                },
            )
            result = await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError as exc:
            timed_out = True
            raise WorkerOfflineError("model configuration request timed out") from exc
        finally:
            if not timed_out:
                with self._lock:
                    self._model_requests.pop(request_id, None)
        if result.get("status") != "completed":
            raise WorkerCommandFailedError(
                "local model configuration was rejected",
                {"reason": result.get("error", "model_configuration_failed")},
            )
        return {"status": "completed", "model": result.get("model", "")}

    def _register_pending_request(
        self,
        requests: dict[str, PendingWorkerRequest],
        *,
        request_id: str,
        device_id: str,
        owner_id: str,
        capability: str,
        subject_id: str = "",
    ) -> asyncio.Future:
        with self._lock:
            self._expire_pending_requests_locked()
            connection = self._connections.get(device_id)
            if connection is None or not connection.ready:
                raise WorkerOfflineError("local Worker is offline")
            if capability not in connection.device.capabilities:
                raise WorkerCapabilityUnavailableError(
                    f"the selected Worker does not provide {capability}"
                )
            future = self._loop.create_future()
            requests[request_id] = PendingWorkerRequest(
                owner_id, device_id, future, subject_id, time.monotonic()
            )
            return future

    def _expire_pending_requests_locked(self) -> None:
        cutoff = time.monotonic() - 60
        for requests in (self._history_requests, self._model_requests):
            expired = [
                request_id
                for request_id, request in requests.items()
                if request.created_at < cutoff
            ]
            for request_id in expired:
                request = requests.pop(request_id)
                if not request.future.done():
                    request.future.cancel()

    def request_workspace_selection(self, device_id: str, owner_id: str) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        with self._lock:
            self._expire_workspace_requests_locked()
            connection = self._connections.get(device_id)
            if connection is None or not connection.ready:
                raise WorkerOfflineError("local Worker is offline")
            if "workspace_selection" not in connection.device.capabilities:
                raise WorkerCapabilityUnavailableError(
                    "the selected Worker does not provide the Companion directory picker"
                )
            existing = next(
                (
                    item
                    for item in self._workspace_requests.values()
                    if item.device_id == device_id and item.status == "pending"
                ),
                None,
            )
            if existing is not None:
                raise WorkerCommandPendingError(
                    "a directory selection request is already pending",
                    {"request_id": existing.request_id},
                )
            now = datetime.now(timezone.utc)
            request = WorkspaceSelectionRequest(
                request_id="wsel_" + uuid.uuid4().hex,
                owner_id=owner_id,
                device_id=device_id,
                created_at=now.isoformat().replace("+00:00", "Z"),
                expires_at=(now + timedelta(seconds=_WORKSPACE_SELECTION_TTL_SECONDS))
                .isoformat()
                .replace("+00:00", "Z"),
            )
            self._workspace_requests[request.request_id] = request
        try:
            self._send(
                device_id,
                {
                    "type": "workspace.select",
                    "request_id": request.request_id,
                    "expires_at": request.expires_at,
                },
            )
        except Exception:
            with self._lock:
                request.status = "failed"
                request.error = "worker_offline"
            raise
        return request.to_dict()

    def get_workspace_selection(self, device_id: str, request_id: str, owner_id: str) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        with self._lock:
            self._expire_workspace_requests_locked()
            request = self._workspace_requests.get(request_id)
            if request is None or request.device_id != device_id or request.owner_id != owner_id:
                raise NotFoundError("workspace selection request not found")
            return request.to_dict()

    def revoke(self, device_id: str, owner_id: str) -> None:
        self._devices.get_for_owner(device_id, owner_id)
        task_id = ""
        with self._lock:
            connection = self._connections.pop(device_id, None)
            self._fail_workspace_requests_locked(device_id, "worker_revoked")
            self._fail_pending_requests_locked(device_id, "worker_revoked")
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
        elif message_type == "workspaces.updated":
            self._handle_workspaces_updated(connection, message)
        elif message_type == "sessions.updated":
            self._handle_sessions_updated(connection, message)
        elif message_type == "workspace.selection.completed":
            self._handle_workspace_selection_completed(connection, message)
        elif message_type == "session.history.result":
            self._handle_history_result(connection, message)
        elif message_type == "model.configuration.completed":
            self._handle_model_configuration_completed(connection, message)
        elif message_type == "heartbeat":
            self._send(connection.device.device_id, {"type": "heartbeat.ack"})
        else:
            raise WorkerProtocolError("unknown Worker message type")

    async def _handle_hello(self, connection: WorkerConnection, message: dict) -> None:
        workspaces = _parse_workspaces(message.get("workspaces", []))
        capabilities = _parse_capabilities(message.get("capabilities", []))
        version = _parse_worker_version(message.get("version", ""))
        protocol_version = _parse_protocol_version(message.get("protocol_version", 0))
        platform = _parse_platform_value(message.get("platform", "unknown"), "platform")
        architecture = _parse_platform_value(
            message.get("architecture", "unknown"), "architecture"
        )
        connection.device = self._devices.update_presence(
            connection.device.device_id,
            model=str(message.get("model", "")),
            model_configured=bool(message.get("model_configured", False)),
            version=version,
            protocol_version=protocol_version,
            platform=platform,
            architecture=architecture,
            capabilities=capabilities,
            workspaces=workspaces,
        )
        connection.ready = True
        self._send(
            connection.device.device_id,
            {"type": "hello.ack", "device_id": connection.device.device_id, "server_time": utc_now()},
        )

    def _handle_workspaces_updated(self, connection: WorkerConnection, message: dict) -> None:
        if not connection.ready or "workspace_selection" not in connection.device.capabilities:
            raise WorkerProtocolError("Worker cannot update workspaces before Companion hello")
        self._update_workspace_presence(connection, message.get("workspaces", []))
        self._send(connection.device.device_id, {"type": "workspaces.updated.ack"})

    def _handle_sessions_updated(self, connection: WorkerConnection, message: dict) -> None:
        if not connection.ready or "local_history" not in connection.device.capabilities:
            raise WorkerProtocolError("Worker cannot update sessions before Companion hello")
        raw_sessions = message.get("sessions", [])
        if not isinstance(raw_sessions, list) or len(raw_sessions) > 100:
            raise WorkerProtocolError("invalid local session index chunk")
        workspace_ids = {item.workspace_id for item in connection.device.workspaces}
        for raw in raw_sessions:
            summary = _parse_session_summary(raw, workspace_ids)
            self._merge_local_session_summary(connection, summary)
        self._send(
            connection.device.device_id,
            {
                "type": "sessions.updated.ack",
                "complete": bool(message.get("complete", False)),
            },
        )

    def _merge_local_session_summary(self, connection: WorkerConnection, summary: dict) -> None:
        session_id = summary["session_id"]
        try:
            current = self._session_store.load(session_id)
        except Exception as exc:
            from pico.session_store import SessionNotFoundError

            if not isinstance(exc, SessionNotFoundError):
                raise
            current = None
        if current is not None:
            if (
                current.get("owner_id") != connection.device.owner_id
                or current.get("device_id") != connection.device.device_id
                or current.get("execution_environment") != "local_worker"
                or current.get("workspace_id") != summary["workspace_id"]
            ):
                raise WorkerProtocolError("local session identity conflicts with control-plane data")
            changes = {
                "title": summary["title"],
                "local_message_total": summary["message_total"],
                "local_updated_at": summary["updated_at"],
            }
            changed = any(current.get(key) != value for key, value in changes.items())
            if changed:
                current.update(changes)
            scrubbed = self._scrub_local_session_content(current)
            if changed or scrubbed:
                self._session_store.save(current)
            self._scrub_local_task_content(session_id, connection.device.owner_id)
            return
        self._session_store.save(
            {
                "id": session_id,
                "created_at": summary["created_at"] or utc_now(),
                "workspace_root": (
                    f"worker://{connection.device.device_id}/{summary['workspace_id']}"
                ),
                "workspace_id": summary["workspace_id"],
                "execution_environment": "local_worker",
                "device_id": connection.device.device_id,
                "owner_id": connection.device.owner_id,
                "title": summary["title"] or f"Session {session_id[-8:]}",
                "history": [],
                "memory": default_memory_state(),
                "local_message_total": summary["message_total"],
                "local_updated_at": summary["updated_at"],
            }
        )

    def _handle_history_result(self, connection: WorkerConnection, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._history_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("history request is not pending")
        status = str(message.get("status", ""))
        result: dict = {"status": status}
        if status == "completed":
            if str(message.get("session_id", "")) != request.subject_id:
                raise WorkerProtocolError("history result session does not match request")
            messages = _parse_history_messages(message.get("messages", []))
            message_total = message.get("message_total", 0)
            if not isinstance(message_total, int) or message_total < len(messages):
                raise WorkerProtocolError("invalid local history message count")
            result.update(
                {
                    "messages": messages,
                    "message_total": message_total,
                    "session_id": str(message.get("session_id", "")),
                }
            )
        elif status == "failed":
            result["error"] = _safe_error_code(message.get("error"), "history_unavailable")
        else:
            raise WorkerProtocolError("invalid local history result status")
        if not request.future.done():
            request.future.set_result(result)
        with self._lock:
            self._history_requests.pop(request_id, None)

    def _handle_model_configuration_completed(
        self, connection: WorkerConnection, message: dict
    ) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._model_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("model configuration request is not pending")
        status = str(message.get("status", ""))
        if status == "completed":
            model = str(message.get("model", ""))[:200]
            if not model:
                raise WorkerProtocolError("configured model is missing")
            connection.device = self._devices.update_presence(
                connection.device.device_id,
                model=model,
                model_configured=True,
                version=connection.device.version,
                protocol_version=connection.device.protocol_version,
                platform=connection.device.platform,
                architecture=connection.device.architecture,
                capabilities=connection.device.capabilities,
                workspaces=connection.device.workspaces,
            )
            result = {"status": "completed", "model": model}
        elif status == "failed":
            result = {
                "status": "failed",
                "error": _safe_error_code(
                    message.get("error"), "model_configuration_failed"
                ),
            }
        else:
            raise WorkerProtocolError("invalid model configuration status")
        if not request.future.done():
            request.future.set_result(result)
        with self._lock:
            self._model_requests.pop(request_id, None)
        self._send(
            connection.device.device_id,
            {"type": "model.configuration.ack", "request_id": request_id},
        )

    def _handle_workspace_selection_completed(
        self, connection: WorkerConnection, message: dict
    ) -> None:
        request_id = str(message.get("request_id", ""))
        status = str(message.get("status", ""))
        if status not in {"selected", "cancelled", "failed"}:
            raise WorkerProtocolError("invalid workspace selection status")
        with self._lock:
            self._expire_workspace_requests_locked()
            request = self._workspace_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("workspace selection request is not pending")
            if request.status != "pending":
                self._send(
                    connection.device.device_id,
                    {
                        "type": "workspace.selection.ack",
                        "request_id": request_id,
                        "status": request.status,
                    },
                )
                return
        if status == "selected":
            workspaces = self._update_workspace_presence(
                connection, message.get("workspaces", [])
            )
            workspace_id = str(message.get("workspace_id", ""))
            if not any(item.workspace_id == workspace_id for item in workspaces):
                raise WorkerProtocolError("selected workspace is missing from Worker metadata")
            with self._lock:
                request.workspace_id = workspace_id
                request.status = "completed"
        elif status == "cancelled":
            with self._lock:
                request.status = "cancelled"
        else:
            error = str(message.get("error", "selection_failed"))
            with self._lock:
                request.status = "failed"
                request.error = error if _CAPABILITY.fullmatch(error) else "selection_failed"
        self._send(
            connection.device.device_id,
            {"type": "workspace.selection.ack", "request_id": request_id},
        )

    def _update_workspace_presence(
        self, connection: WorkerConnection, raw_workspaces
    ) -> list[WorkerWorkspace]:
        workspaces = _parse_workspaces(raw_workspaces)
        connection.device = self._devices.update_presence(
            connection.device.device_id,
            model=connection.device.model,
            model_configured=connection.device.model_configured,
            version=connection.device.version,
            protocol_version=connection.device.protocol_version,
            platform=connection.device.platform,
            architecture=connection.device.architecture,
            capabilities=connection.device.capabilities,
            workspaces=workspaces,
        )
        return workspaces

    def _expire_workspace_requests_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for request in self._workspace_requests.values():
            expiry = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
            if request.status == "pending" and now >= expiry:
                request.status = "expired"
                request.error = "selection_expired"
        while len(self._workspace_requests) > 512:
            request_id = next(
                (
                    item.request_id
                    for item in self._workspace_requests.values()
                    if item.status != "pending"
                ),
                next(iter(self._workspace_requests)),
            )
            self._workspace_requests.pop(request_id, None)

    def _fail_workspace_requests_locked(self, device_id: str, reason: str) -> None:
        for request in self._workspace_requests.values():
            if request.device_id == device_id and request.status == "pending":
                request.status = "failed"
                request.error = reason

    def _fail_pending_requests_locked(self, device_id: str, reason: str) -> None:
        for requests in (self._history_requests, self._model_requests):
            for request in requests.values():
                if request.device_id == device_id and not request.future.done():
                    self._loop.call_soon_threadsafe(
                        _resolve_future,
                        request.future,
                        {"status": "failed", "error": reason},
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
        message_total = message.get("message_total", 0)
        if not isinstance(message_total, int) or message_total < 0:
            raise WorkerProtocolError("invalid local session message count")
        session_persisted = message.get("session_persisted", False)
        if not isinstance(session_persisted, bool):
            raise WorkerProtocolError("invalid local session persistence flag")
        self._update_local_session_count(task, message_total, session_persisted)
        self._cancel_pending_approvals(task, stop_reason)
        self._task_repo.update(
            task_id,
            lambda item: _set_terminal(item, status, stop_reason, ""),
        )
        terminal_event = {
            TaskStatus.COMPLETED: "task.completed",
            TaskStatus.CANCELLED: "task.cancelled",
            TaskStatus.FAILED: "task.failed",
        }[status]
        if status is TaskStatus.COMPLETED:
            self._remember_terminal_answer(task_id, final_answer)
            self._publisher.publish(task_id, task.run_id, "message.completed", {"text": final_answer})
        self._publisher.publish(task_id, task.run_id, terminal_event, {"final_answer": final_answer, "stop_reason": stop_reason})
        self._release(task_id, connection.device.device_id)

    def _update_local_session_count(
        self, task, message_total: int, session_persisted: bool
    ) -> None:
        current = self._session_store.load(task.session_id)
        if (
            current.get("owner_id") != task.owner_id
            or current.get("workspace_id") != task.workspace_id
            or current.get("device_id") != task.device_id
        ):
            raise WorkerProtocolError("Worker returned an invalid session identity")
        current["local_message_total"] = message_total
        current["updated_at"] = utc_now()
        if session_persisted:
            self._scrub_local_session_content(current)
        self._session_store.save(current)
        if session_persisted:
            self._scrub_local_task_content(task.session_id, task.owner_id)

    @staticmethod
    def _scrub_local_session_content(session: dict) -> bool:
        empty_memory = default_memory_state()
        changed = (
            session.get("history") != []
            or session.get("memory") != empty_memory
            or any(
                key in session
                for key in ("checkpoints", "runtime_identity", "resume_state")
            )
        )
        session["history"] = []
        session["memory"] = empty_memory
        for key in ("checkpoints", "runtime_identity", "resume_state"):
            session.pop(key, None)
        return changed

    def _scrub_local_task_content(self, session_id: str, owner_id: str) -> None:
        tasks, _ = self._task_repo.list_for_session(session_id, owner_id)
        for task in tasks:
            if task.execution_environment == "local_worker" and (
                task.input or task.final_answer
            ):
                self._task_repo.update(task.task_id, _clear_local_task_content)

    def _fail_task(self, task_id: str, reason: str) -> None:
        with self._lock:
            try:
                task = self._task_repo.get(task_id)
                if task.status.terminal:
                    return
                self._cancel_pending_approvals(task, reason)
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


def _parse_workspaces(raw_workspaces) -> list[WorkerWorkspace]:
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
    if len(workspaces) > 100 or len({item.workspace_id for item in workspaces}) != len(workspaces):
        raise WorkerProtocolError("invalid Worker workspace collection")
    return workspaces


def _parse_capabilities(raw_capabilities) -> list[str]:
    if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 20:
        raise WorkerProtocolError("capabilities must be a bounded list")
    capabilities = []
    for item in raw_capabilities:
        capability = str(item)
        if not _CAPABILITY.fullmatch(capability):
            raise WorkerProtocolError("invalid Worker capability")
        if capability not in capabilities:
            capabilities.append(capability)
    return capabilities


def _parse_worker_version(raw_version) -> str:
    version = str(raw_version).strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise WorkerProtocolError("version must be a semantic version")
    return version


def _parse_protocol_version(raw_version) -> int:
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise WorkerProtocolError("protocol_version must be an integer")
    if not 1 <= raw_version <= 1000:
        raise WorkerProtocolError("protocol_version is unsupported")
    return raw_version


def _parse_platform_value(raw_value, field: str) -> str:
    value = str(raw_value).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,31}", value):
        raise WorkerProtocolError(f"{field} is invalid")
    return value


def _parse_session_summary(raw, workspace_ids: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise WorkerProtocolError("local session summary must be an object")
    session_id = str(raw.get("session_id", ""))
    workspace_id = str(raw.get("workspace_id", ""))
    title = str(redact_artifact(raw.get("title", ""))).strip()[:200]
    created_at = str(raw.get("created_at", ""))[:40]
    updated_at = str(raw.get("updated_at", ""))[:40]
    message_total = raw.get("message_total", 0)
    if (
        not _SESSION_ID.fullmatch(session_id)
        or workspace_id not in workspace_ids
        or not isinstance(message_total, int)
        or message_total < 0
        or message_total > 10_000_000
    ):
        raise WorkerProtocolError("invalid local session summary")
    return {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "message_total": message_total,
    }


def _parse_history_messages(raw_messages) -> list[dict]:
    if not isinstance(raw_messages, list) or len(raw_messages) > 500:
        raise WorkerProtocolError("invalid local history messages")
    messages = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise WorkerProtocolError("local history message must be an object")
        messages.append(
            {
                "role": str(raw.get("role", ""))[:32],
                "name": str(raw.get("name", ""))[:100],
                "content": str(redact_artifact(raw.get("content", "")))[:4000],
                "created_at": str(raw.get("created_at", ""))[:40],
            }
        )
    return messages


def _safe_error_code(value, default: str) -> str:
    text = str(value or default)
    return text if _CAPABILITY.fullmatch(text) else default


def _resolve_future(future: asyncio.Future, result: dict) -> None:
    if not future.done():
        future.set_result(result)


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


def _clear_local_task_content(task):
    task.input = ""
    task.final_answer = None
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

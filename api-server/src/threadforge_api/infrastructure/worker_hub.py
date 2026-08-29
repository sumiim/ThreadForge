"""Central control-plane bridge for outbound local Worker connections."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket
from pico.features.memory import default_memory_state
from pico.security import (
    public_tool_args_preview,
    public_tool_result_preview,
    redact_artifact,
)

from ..domain.entities import Approval, canonical_json, utc_now
from ..domain.enums import ApprovalStatus, TaskStatus
from ..domain.errors import (
    AppError,
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalStaleError,
    DeviceNotFoundError,
    NotFoundError,
    PersistenceUnavailableError,
    RenameConflictError,
    UninstallUnavailableError,
    UpdateBackoffError,
    UpdateUnavailableError,
    WorkerBusyError,
    WorkerCapabilityUnavailableError,
    WorkerCommandFailedError,
    WorkerConcurrencyLimitError,
    WorkerOfflineError,
    WorkerProtocolError,
)
from ..domain.events import event_phase
from .device_store import Device, DeviceStore, WorkerWorkspace

_PUBLIC_WORKER_EVENTS = {
    "model.started",
    "model.completed",
    "model.retrying",
    "model.protocol_retrying",
    "model.heartbeat",
    "assistant.delta",
    "assistant.thinking",
    "tool.requested",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "policy.violation",
    "sandbox.started",
    "sandbox.completed",
    "sandbox.failed",
    "sandbox.cleaned",
    "agent.state",
    "plan.created",
    "plan.skipped",
    "assistant.commentary",
    "review.started",
    "review.completed",
    "review.skipped",
    "main_loop_rebuttal",
}
_WORKSPACE_ID = re.compile(r"^ws_[a-f0-9]{32}$")
_SESSION_ID = re.compile(r"^ses_[a-f0-9]{32}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PUBLIC_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_WORKSPACE_SELECTION_TTL_SECONDS = 120
_EPHEMERAL_ANSWER_TTL_SECONDS = 600
_EPHEMERAL_PROGRESS_TTL_SECONDS = 600
LOGGER = logging.getLogger(__name__)


def _worker_command_error(reason: str, *, command: str) -> AppError:
    """把 Worker 回传的稳定原因映射成明确错误码（不再一律 worker_command_failed）。

    Worker 在 update/uninstall 被拒时回 `error=worker_busy / update_unavailable /
    update_backoff / uninstall_unavailable` 等稳定原因；前端据此显示可执行文案
    （如「Worker 正在运行任务,请先停止再更新」）。
    """
    mapping = {
        "worker_busy": WorkerBusyError(f"Worker is busy; cannot {command} now"),
        "update_unavailable": UpdateUnavailableError("Worker update is unavailable"),
        "update_backoff": UpdateBackoffError("Worker update is in backoff cooldown"),
        "uninstall_unavailable": UninstallUnavailableError("Worker uninstall is unavailable"),
    }
    error = mapping.get(str(reason or ""))
    if error is not None:
        return error
    return WorkerCommandFailedError(
        f"Worker {command} was rejected",
        {"reason": reason or f"{command}_failed"},
    )


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
        self._active_by_device: dict[str, set[str]] = {}
        self._device_by_task: dict[str, str] = {}
        self._workspace_requests: dict[str, WorkspaceSelectionRequest] = {}
        self._history_requests: dict[str, PendingWorkerRequest] = {}
        self._model_requests: dict[str, PendingWorkerRequest] = {}
        self._provider_requests: dict[str, PendingWorkerRequest] = {}
        self._rename_requests: dict[str, PendingWorkerRequest] = {}
        self._delete_requests: dict[str, PendingWorkerRequest] = {}
        self._uninstall_requests: dict[str, PendingWorkerRequest] = {}
        self._update_requests: dict[str, PendingWorkerRequest] = {}
        self._terminal_answers: dict[str, tuple[float, str]] = {}
        self._agent_progress: dict[str, tuple[float, dict]] = {}
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
        replaced_task_ids: list[str] = []
        with self._lock:
            previous = self._connections.get(device.device_id)
            self._connections[device.device_id] = connection
            if previous is not None:
                self._fail_workspace_requests_locked(device.device_id, "worker_reconnected")
                self._fail_pending_requests_locked(device.device_id, "worker_reconnected")
                replaced_task_ids = list(self._active_by_device.pop(device.device_id, set()))
                for replaced_task_id in replaced_task_ids:
                    self._device_by_task.pop(replaced_task_id, None)
        for replaced_task_id in replaced_task_ids:
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
        task_ids: list[str] = []
        with self._lock:
            if self._connections.get(connection.device.device_id) is connection:
                self._connections.pop(connection.device.device_id, None)
                self._fail_workspace_requests_locked(
                    connection.device.device_id, "worker_disconnected"
                )
                self._fail_pending_requests_locked(
                    connection.device.device_id, "worker_disconnected"
                )
                task_ids = list(self._active_by_device.pop(connection.device.device_id, set()))
                for task_id in task_ids:
                    self._device_by_task.pop(task_id, None)
        for task_id in task_ids:
            # §7.8.9 修正（2026-08-19）：断线 ≠ 运行失败——任务因连接中断而终止,
            # 前端应显示「运行因服务重启或连接中断而终止」而非「Agent 运行失败」。
            self._fail_task(task_id, "worker_disconnected", status=TaskStatus.INTERRUPTED)
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

    def online_devices(self, owner_id: str) -> list[Device]:
        """Return all ready Workers owned by ``owner_id``.

        This is intentionally a separate query from task dispatch.  It gives
        the API a stable inventory boundary for future multi-Worker routing
        without changing the current one-session/one-Worker execution model.
        """
        owned = {device.device_id: device for device in self._devices.list_for_owner(owner_id)}
        with self._lock:
            return [
                device
                for device_id, device in owned.items()
                if (connection := self._connections.get(device_id)) is not None
                and connection.ready
            ]

    def ephemeral_final_answer(self, task_id: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._expire_terminal_answers_locked(now)
            item = self._terminal_answers.get(task_id)
            return item[1] if item is not None else None

    def ephemeral_agent_progress(self, task_id: str) -> dict | None:
        now = time.monotonic()
        with self._lock:
            item = self._agent_progress.get(task_id)
            if item is None:
                return None
            if now - item[0] >= _EPHEMERAL_PROGRESS_TTL_SECONDS:
                self._agent_progress.pop(task_id, None)
                return None
            return dict(item[1])

    def _remember_agent_progress(self, task_id: str, progress: dict) -> None:
        with self._lock:
            now = time.monotonic()
            self._agent_progress[task_id] = (now, dict(progress))
            while len(self._agent_progress) > 512:
                self._agent_progress.pop(next(iter(self._agent_progress)))

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
            # 旧版 Worker 对「本地无该 session」返回 failed/history_unavailable。
            # 控制面已有该会话的 task 失败记录（run_index），历史为空是合法降级，
            # 不应让整条 get_session 接口 422 导致前端「历史加载失败」。
            # 新 Worker 直接返回 completed + 空历史（error=history_unavailable 标记），
            # 此处兼容旧 Worker 的 failed 语义。
            if result.get("error") == "history_unavailable":
                return {"messages": [], "message_total": 0}
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
        model_provider: str = "",
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
                    "model_provider": model_provider,
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

    async def configure_provider(
        self,
        *,
        device_id: str,
        owner_id: str,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str,
        reasoning_efforts: list[str] | None = None,
    ) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        request_id = "provider_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._provider_requests,
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
                    "type": "provider.configure",
                    "request_id": request_id,
                    "provider_id": provider_id,
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": model,
                    "protocol": protocol,
                    "reasoning_efforts": reasoning_efforts or [],
                },
            )
            result = await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError as exc:
            timed_out = True
            raise WorkerOfflineError("provider configuration request timed out") from exc
        finally:
            if not timed_out:
                with self._lock:
                    self._provider_requests.pop(request_id, None)
        if result.get("status") != "completed":
            raise WorkerCommandFailedError(
                "local provider configuration was rejected",
                {"reason": result.get("error", "provider_configuration_failed")},
            )
        return {"provider_id": provider_id, "status": "completed"}

    async def list_provider_models(
        self,
        *,
        device_id: str,
        owner_id: str,
        provider_id: str,
    ) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        request_id = "models_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._provider_requests,
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
                    "type": "provider.list_models",
                    "request_id": request_id,
                    "provider_id": provider_id,
                },
            )
            result = await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError as exc:
            timed_out = True
            raise WorkerOfflineError("provider model listing timed out") from exc
        finally:
            if not timed_out:
                with self._lock:
                    self._provider_requests.pop(request_id, None)
        if result.get("status") != "completed":
            raise WorkerCommandFailedError(
                "local provider model listing failed",
                {"reason": result.get("error", "provider_list_models_failed")},
            )
        return {"provider_id": provider_id, "models": result.get("models", [])}

    async def rename_entity(
        self,
        *,
        device_id: str,
        owner_id: str,
        entity_type: str,
        entity_id: str,
        display_name: str,
        expected_updated_at: str | None = None,
    ) -> dict:
        device = self._devices.get_for_owner(device_id, owner_id)
        display_name = str(display_name).strip()
        if not display_name or len(display_name) > 200:
            raise ValueError("invalid display name")
        if entity_type == "workspace":
            workspace = next(
                (item for item in device.workspaces if item.workspace_id == entity_id),
                None,
            )
            if not _WORKSPACE_ID.fullmatch(entity_id) or workspace is None:
                raise NotFoundError("workspace not found")
            if expected_updated_at and expected_updated_at != workspace.display_name_updated_at:
                raise RenameConflictError("workspace display name changed on another client")
        elif entity_type == "session":
            if not _SESSION_ID.fullmatch(entity_id):
                raise NotFoundError("session not found")
            session = self._session_store.load(entity_id)
            if (
                session.get("owner_id") != owner_id
                or session.get("device_id") != device_id
                or session.get("execution_environment") != "local_worker"
            ):
                raise NotFoundError("session not found")
            display_name_updated_at = str(
                session.get("display_name_updated_at", session.get("created_at", ""))
            )
            if expected_updated_at and expected_updated_at != display_name_updated_at:
                raise RenameConflictError("session display name changed on another client")
        else:
            raise ValueError("unsupported rename entity type")

        request_id = "rename_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._rename_requests,
            request_id=request_id,
            device_id=device_id,
            owner_id=owner_id,
            capability="rename_entities",
            subject_id=entity_id,
        )
        try:
            self._send(
                device_id,
                {
                    "type": "entity.rename",
                    "request_id": request_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "display_name": display_name,
                },
            )
            result = await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError as exc:
            raise WorkerOfflineError("rename request timed out") from exc
        finally:
            with self._lock:
                self._rename_requests.pop(request_id, None)
        if result.get("status") != "completed":
            raise WorkerCommandFailedError(
                "local rename was rejected",
                {"reason": result.get("error", "rename_failed")},
            )
        if entity_type == "workspace":
            stored_device = self._devices.get_for_owner(device_id, owner_id)
            stored = next(
                item for item in stored_device.workspaces if item.workspace_id == entity_id
            )
            updated_at = stored.display_name_updated_at
        else:
            stored_session = self._session_store.load(entity_id)
            updated_at = str(stored_session.get("display_name_updated_at", ""))
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "display_name": display_name,
            "display_name_source": "user",
            "display_name_updated_at": updated_at,
        }

    async def delete_entity(
        self,
        *,
        device_id: str,
        owner_id: str,
        entity_type: str,
        entity_id: str,
        session_ids: list[str] | None = None,
        run_ids: list[str] | None = None,
    ) -> dict:
        device = self._devices.get_for_owner(device_id, owner_id)
        if entity_type == "workspace":
            if not _WORKSPACE_ID.fullmatch(entity_id) or not any(
                item.workspace_id == entity_id for item in device.workspaces
            ):
                raise NotFoundError("workspace not found")
        elif entity_type == "session":
            if not _SESSION_ID.fullmatch(entity_id):
                raise NotFoundError("session not found")
            session = self._session_store.load(entity_id)
            if (
                session.get("owner_id") != owner_id
                or session.get("device_id") != device_id
                or session.get("execution_environment") != "local_worker"
            ):
                raise NotFoundError("session not found")
        else:
            raise ValueError("unsupported delete entity type")

        request_id = "delete_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._delete_requests,
            request_id=request_id,
            device_id=device_id,
            owner_id=owner_id,
            capability="delete_entities",
            subject_id=entity_id,
        )
        try:
            self._send(
                device_id,
                {
                    "type": "entity.delete",
                    "request_id": request_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "session_ids": list(session_ids or []),
                    "run_ids": list(run_ids or []),
                },
            )
            result = await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError as exc:
            raise WorkerOfflineError("delete request timed out") from exc
        finally:
            with self._lock:
                self._delete_requests.pop(request_id, None)
        if result.get("status") != "completed":
            raise WorkerCommandFailedError(
                "local delete was rejected",
                {"reason": result.get("error", "delete_failed")},
            )
        return {
            "status": "deleted",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "deleted_session_ids": result.get("deleted_session_ids", []),
        }

    async def uninstall_worker(self, *, device_id: str, owner_id: str) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        request_id = "uninstall_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._uninstall_requests,
            request_id=request_id,
            device_id=device_id,
            owner_id=owner_id,
            capability="worker_uninstall",
            subject_id=device_id,
        )
        try:
            self._send(device_id, {"type": "worker.uninstall", "request_id": request_id})
            result = await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError as exc:
            raise WorkerOfflineError("Worker uninstall request timed out") from exc
        finally:
            with self._lock:
                self._uninstall_requests.pop(request_id, None)
        if result.get("status") != "completed":
            # Worker 回传的稳定原因（worker_busy/uninstall_unavailable）映射成
            # 明确错误码，前端据此显示可执行文案，不再笼统归为 worker_command_failed。
            raise _worker_command_error(result.get("error", "uninstall_failed"), command="uninstall")
        return {"status": "uninstalling", "device_id": device_id}

    async def update_worker(self, *, device_id: str, owner_id: str) -> dict:
        self._devices.get_for_owner(device_id, owner_id)
        request_id = "update_" + uuid.uuid4().hex
        future = self._register_pending_request(
            self._update_requests,
            request_id=request_id,
            device_id=device_id,
            owner_id=owner_id,
            capability="auto_update",
            subject_id=device_id,
        )
        try:
            self._send(device_id, {"type": "worker.update", "request_id": request_id})
            result = await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError as exc:
            raise WorkerOfflineError("Worker update request timed out") from exc
        finally:
            with self._lock:
                self._update_requests.pop(request_id, None)
        if result.get("status") not in {"started", "current"}:
            raise _worker_command_error(result.get("error", "update_failed"), command="update")
        return {"status": result.get("status"), "device_id": device_id}

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
            if (requests is self._rename_requests or requests is self._delete_requests) and any(
                item.device_id == device_id and item.subject_id == subject_id
                for item in requests.values()
            ):
                raise RenameConflictError("entity rename is already in progress")
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
        for requests in (
            self._history_requests,
            self._model_requests,
            self._provider_requests,
            self._rename_requests,
            self._delete_requests,
            self._uninstall_requests,
            self._update_requests,
        ):
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
                # Browser refreshes and transient retries must resume the
                # existing native prompt instead of creating a dead-end 409.
                return existing.to_dict()
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
        task_ids: list[str] = []
        with self._lock:
            connection = self._connections.pop(device_id, None)
            self._fail_workspace_requests_locked(device_id, "worker_revoked")
            self._fail_pending_requests_locked(device_id, "worker_revoked")
            task_ids = list(self._active_by_device.pop(device_id, set()))
            for task_id in task_ids:
                self._device_by_task.pop(task_id, None)
        self._devices.revoke(device_id, owner_id)
        for task_id in task_ids:
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
            active = self._active_by_device.get(task.device_id, set())
            limit = int(getattr(self._settings, "worker_max_concurrent_tasks", 2))
            if len(active) >= limit:
                raise WorkerConcurrencyLimitError(task.device_id, limit)
            active.add(task.task_id)
            self._active_by_device[task.device_id] = active
            self._device_by_task[task.task_id] = task.device_id
        try:
            self._task_repo.update(task.task_id, lambda item: _set_status(item, TaskStatus.RUNNING))
            self._publisher.publish(
                task.task_id, task.run_id, "task.started", {}, phase="system", status="running"
            )
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
                        "permission_mode": task.permission_mode,
                        "session": session,
                        "settings": {
                            "max_new_tokens": self._settings.max_new_tokens,
                            "model_timeout_seconds": self._settings.model_timeout_seconds,
                            "shell_output_max_bytes": self._settings.shell_output_max_bytes,
                            "shell_cleanup_grace_seconds": self._settings.shell_cleanup_grace_seconds,
                            "model_id": task.model_id,
                            "reasoning_effort": task.reasoning_effort,
                            "provider_id": task.provider_id,
                            "sandbox_enabled": bool(
                                getattr(self._settings, "sandbox_enabled", False)
                            ),
                            "sandbox_image": str(
                                getattr(self._settings, "sandbox_image", "threadforge-sandbox:latest")
                            ),
                            "sandbox_user": str(
                                getattr(self._settings, "sandbox_user", "65534:65534")
                            ),
                            "sandbox_cpu_limit": float(
                                getattr(self._settings, "sandbox_cpu_limit", 1.0)
                            ),
                            "sandbox_memory_limit": str(
                                getattr(self._settings, "sandbox_memory_limit", "512m")
                            ),
                            "sandbox_pids_limit": int(
                                getattr(self._settings, "sandbox_pids_limit", 64)
                            ),
                            "sandbox_network": str(
                                getattr(self._settings, "sandbox_network", "none")
                            ),
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
                self._publisher.publish(
                    task_id, task.run_id, "task.cancel_requested", {}, phase="final", status="cancel_requested"
                )
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
                phase="approval",
                status=status.value,
                summary=decision,
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
        elif message_type == "provider.configuration.completed":
            self._handle_provider_configuration_completed(connection, message)
        elif message_type == "provider.list_models.completed":
            self._handle_provider_list_models_completed(connection, message)
        elif message_type == "entity.rename.completed":
            self._handle_rename_completed(connection, message)
        elif message_type == "entity.delete.completed":
            self._handle_delete_completed(connection, message)
        elif message_type == "worker.uninstall.completed":
            self._handle_uninstall_completed(connection, message)
        elif message_type == "worker.update.completed":
            self._handle_update_completed(connection, message)
        elif message_type == "update.status":
            self._handle_update_status(connection, message)
        elif message_type == "heartbeat":
            self._send(connection.device.device_id, {"type": "heartbeat.ack"})
        else:
            raise WorkerProtocolError("unknown Worker message type")

    async def _handle_hello(self, connection: WorkerConnection, message: dict) -> None:
        workspaces = _parse_workspaces(message.get("workspaces", []))
        capabilities = _parse_capabilities(message.get("capabilities", []))
        model_capabilities = _parse_model_capabilities(
            message.get("model_capabilities", {}),
            str(message.get("model", "")),
        )
        version = _parse_worker_version(message.get("version", ""))
        protocol_version = _parse_protocol_version(message.get("protocol_version", 0))
        platform = _parse_platform_value(message.get("platform", "unknown"), "platform")
        architecture = _parse_platform_value(
            message.get("architecture", "unknown"), "architecture"
        )
        update_status = _parse_update_status(message.get("update_status", {}))
        connection.device = self._devices.update_presence(
            connection.device.device_id,
            model=str(message.get("model", "")),
            model_provider=str(message.get("model_provider", "")),
            model_configured=bool(message.get("model_configured", False)),
            version=version,
            protocol_version=protocol_version,
            platform=platform,
            architecture=architecture,
            capabilities=capabilities,
            workspaces=workspaces,
            orchestration_backend=str(message.get("orchestration_backend", ""))[:64],
            model_capabilities=model_capabilities,
            update_status=update_status,
        )
        connection.ready = True
        self._send(
            connection.device.device_id,
            {"type": "hello.ack", "device_id": connection.device.device_id, "server_time": utc_now()},
        )

    def _handle_update_status(self, connection: WorkerConnection, message: dict) -> None:
        if not connection.ready or "auto_update" not in connection.device.capabilities:
            raise WorkerProtocolError("Worker cannot report update status before Companion hello")
        connection.device = self._devices.update_worker_status(
            connection.device.device_id,
            _parse_update_status(message),
        )
        self._send(connection.device.device_id, {"type": "update.status.ack"})

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
        rejected = []
        for raw in raw_sessions:
            try:
                summary = _parse_session_summary(raw, workspace_ids)
                self._merge_local_session_summary(connection, summary)
            except WorkerProtocolError as exc:
                session_id = str(raw.get("session_id", "")) if isinstance(raw, dict) else ""
                LOGGER.warning(
                    "Rejected local session summary device_id=%s session_id=%s reason=%s",
                    connection.device.device_id,
                    session_id or "unknown",
                    exc.message,
                )
                rejected.append(
                    {
                        "session_id": session_id[:64],
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
        response = {
            "type": "sessions.updated.ack",
            "complete": bool(message.get("complete", False)),
        }
        if rejected:
            response["rejected"] = rejected
        self._send(connection.device.device_id, response)

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
                or current.get("execution_environment") != "local_worker"
                or current.get("workspace_id") != summary["workspace_id"]
            ):
                raise WorkerProtocolError("local session identity conflicts with control-plane data")
            changes = {
                "local_message_total": summary["message_total"],
                "local_updated_at": summary["updated_at"],
            }
            if current.get("device_id") != connection.device.device_id:
                try:
                    self._devices.get(str(current.get("device_id", "")))
                except DeviceNotFoundError:
                    # Reinstalling and pairing the same local data directory creates
                    # a new device identity. Once the old device has been explicitly
                    # unbound, its local-only session metadata can safely follow the
                    # same owner to the replacement device.
                    changes.update(
                        {
                            "device_id": connection.device.device_id,
                            "workspace_root": (
                                f"worker://{connection.device.device_id}/"
                                f"{summary['workspace_id']}"
                            ),
                        }
                    )
                else:
                    raise WorkerProtocolError(
                        "local session is still bound to another registered device"
                    )
            if summary["first_request_at"]:
                changes["first_request_at"] = summary["first_request_at"]
            if current.get("display_name_source", "auto") != "user":
                changes["title"] = summary["title"]
                if summary["display_name_updated_at"]:
                    changes["display_name_source"] = summary["display_name_source"]
                    changes["display_name_updated_at"] = summary[
                        "display_name_updated_at"
                    ]
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
                "display_name_source": summary["display_name_source"],
                "display_name_updated_at": (
                    summary["display_name_updated_at"] or summary["created_at"] or utc_now()
                ),
                "first_request_at": summary["first_request_at"],
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
                model_provider=str(message.get("model_provider", "")),
                model_configured=True,
                version=connection.device.version,
                protocol_version=connection.device.protocol_version,
                platform=connection.device.platform,
                architecture=connection.device.architecture,
                capabilities=connection.device.capabilities,
                workspaces=connection.device.workspaces,
                model_capabilities=_parse_model_capabilities(
                    message.get("model_capabilities", {}), model
                ),
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

    def _handle_provider_configuration_completed(
        self, connection: WorkerConnection, message: dict
    ) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._provider_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("provider configuration request is not pending")
        status = str(message.get("status", ""))
        if status == "completed":
            model = str(message.get("model", ""))[:200]
            if model:
                # 供应商保存/切换后立即刷新设备模型能力，避免旧能力（如只有 none）残留，
                # 导致服务端误拒 high/xhigh。
                connection.device = self._devices.update_presence(
                    connection.device.device_id,
                    model=model,
                    model_provider=connection.device.model_provider,
                    model_configured=True,
                    version=connection.device.version,
                    protocol_version=connection.device.protocol_version,
                    platform=connection.device.platform,
                    architecture=connection.device.architecture,
                    capabilities=connection.device.capabilities,
                    workspaces=connection.device.workspaces,
                    model_capabilities=_parse_model_capabilities(
                        message.get("model_capabilities", {}), model
                    ),
                )
            result = {"status": "completed", "provider_id": str(message.get("provider_id", ""))}
        elif status == "failed":
            result = {
                "status": "failed",
                "error": _safe_error_code(
                    message.get("error"), "provider_configuration_failed"
                ),
            }
        else:
            raise WorkerProtocolError("invalid provider configuration status")
        if not request.future.done():
            request.future.set_result(result)
        with self._lock:
            self._provider_requests.pop(request_id, None)
        self._send(
            connection.device.device_id,
            {"type": "provider.configuration.ack", "request_id": request_id},
        )

    def _handle_provider_list_models_completed(
        self, connection: WorkerConnection, message: dict
    ) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._provider_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("provider list-models request is not pending")
        status = str(message.get("status", ""))
        if status == "completed":
            models = message.get("models", [])
            if not isinstance(models, list):
                raise WorkerProtocolError("invalid provider model list")
            result = {"status": "completed", "models": [str(item)[:200] for item in models]}
        elif status == "failed":
            result = {
                "status": "failed",
                "error": _safe_error_code(
                    message.get("error"), "provider_list_models_failed"
                ),
            }
        else:
            raise WorkerProtocolError("invalid provider list-models status")
        if not request.future.done():
            request.future.set_result(result)
        with self._lock:
            self._provider_requests.pop(request_id, None)
        self._send(
            connection.device.device_id,
            {"type": "provider.list_models.ack", "request_id": request_id},
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

    def _handle_rename_completed(self, connection: WorkerConnection, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._rename_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("rename request is not pending")
        status = str(message.get("status", ""))
        entity_type = str(message.get("entity_type", ""))
        entity_id = str(message.get("entity_id", ""))
        display_name = str(message.get("display_name", "")).strip()
        if entity_id != request.subject_id:
            raise WorkerProtocolError("renamed entity does not match request")
        if status == "completed" and display_name:
            if entity_type == "workspace":
                workspaces = self._update_workspace_presence(
                    connection, message.get("workspaces", [])
                )
                if not any(
                    item.workspace_id == entity_id and item.name == display_name
                    for item in workspaces
                ):
                    raise WorkerProtocolError("renamed workspace metadata is inconsistent")
                connection.device, _ = self._devices.set_workspace_display_name(
                    request.device_id,
                    request.owner_id,
                    entity_id,
                    display_name,
                )
            elif entity_type == "session":
                session = self._session_store.load(entity_id)
                if (
                    session.get("owner_id") != request.owner_id
                    or session.get("device_id") != request.device_id
                ):
                    raise WorkerProtocolError("renamed session ownership changed")
                session["title"] = display_name[:200]
                session["display_name_source"] = "user"
                session["display_name_updated_at"] = utc_now()
                session["updated_at"] = session["display_name_updated_at"]
                self._session_store.save(session)
            else:
                raise WorkerProtocolError("invalid renamed entity type")
            result = {"status": "completed"}
        elif status == "failed":
            result = {"status": "failed", "error": _safe_error_code(message.get("error"), "rename_failed")}
        else:
            raise WorkerProtocolError("invalid rename result")
        if not request.future.done():
            request.future.set_result(result)

    def _handle_delete_completed(self, connection: WorkerConnection, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._delete_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("delete request is not pending")
        entity_type = str(message.get("entity_type", ""))
        entity_id = str(message.get("entity_id", ""))
        if entity_id != request.subject_id or entity_type not in {"session", "workspace"}:
            raise WorkerProtocolError("deleted entity does not match request")
        status = str(message.get("status", ""))
        if status == "completed":
            raw_deleted = message.get("deleted_session_ids", [])
            if not isinstance(raw_deleted, list) or len(raw_deleted) > 1000:
                raise WorkerProtocolError("invalid deleted session list")
            deleted_session_ids = [str(item) for item in raw_deleted]
            if any(not _SESSION_ID.fullmatch(item) for item in deleted_session_ids):
                raise WorkerProtocolError("invalid deleted session id")
            if entity_type == "session" and entity_id not in deleted_session_ids:
                raise WorkerProtocolError("deleted session is missing from result")
            if entity_type == "workspace":
                self._update_workspace_presence(connection, message.get("workspaces", []))
            result = {
                "status": "completed",
                "deleted_session_ids": deleted_session_ids,
            }
        elif status == "failed":
            result = {
                "status": "failed",
                "error": _safe_error_code(message.get("error"), "delete_failed"),
            }
        else:
            raise WorkerProtocolError("invalid delete result")
        if not request.future.done():
            request.future.set_result(result)
        self._send(
            connection.device.device_id,
            {"type": "entity.delete.ack", "request_id": request_id},
        )

    def _handle_uninstall_completed(self, connection: WorkerConnection, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._uninstall_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("uninstall request is not pending")
        status = str(message.get("status", ""))
        if status == "completed":
            result = {"status": "completed"}
        elif status == "failed":
            result = {
                "status": "failed",
                "error": _safe_error_code(message.get("error"), "uninstall_failed"),
            }
        else:
            raise WorkerProtocolError("invalid uninstall result")
        if not request.future.done():
            request.future.set_result(result)
        self._send(
            connection.device.device_id,
            {"type": "worker.uninstall.ack", "request_id": request_id},
        )

    def _handle_update_completed(self, connection: WorkerConnection, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        with self._lock:
            request = self._update_requests.get(request_id)
            if (
                request is None
                or request.device_id != connection.device.device_id
                or request.owner_id != connection.device.owner_id
            ):
                raise WorkerProtocolError("update request is not pending")
        status = str(message.get("status", ""))
        if status == "started":
            result = {"status": "started"}
        elif status == "failed":
            result = {
                "status": "failed",
                "error": _safe_error_code(message.get("error"), "update_failed"),
            }
        else:
            raise WorkerProtocolError("invalid update result")
        if not request.future.done():
            request.future.set_result(result)
        self._send(
            connection.device.device_id,
            {"type": "worker.update.ack", "request_id": request_id},
        )

    def _update_workspace_presence(
        self, connection: WorkerConnection, raw_workspaces
    ) -> list[WorkerWorkspace]:
        workspaces = _parse_workspaces(raw_workspaces)
        connection.device = self._devices.update_presence(
            connection.device.device_id,
            model=connection.device.model,
            model_provider=connection.device.model_provider,
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
        for requests in (
            self._history_requests,
            self._model_requests,
            self._provider_requests,
            self._rename_requests,
            self._delete_requests,
            self._uninstall_requests,
            self._update_requests,
        ):
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
        if not _PUBLIC_EVENT_TYPE.fullmatch(event_type):
            raise WorkerProtocolError("Worker event type is invalid")
        if event_type not in _PUBLIC_WORKER_EVENTS:
            LOGGER.warning(
                "Ignoring unsupported Worker progress event",
                extra={
                    "device_id": connection.device.device_id,
                    "task_id": task_id,
                    "worker_event_type": event_type,
                },
            )
            return
        data = message.get("data", {})
        if not isinstance(data, dict):
            raise WorkerProtocolError("Worker event data must be an object")
        if task.status is TaskStatus.CANCEL_REQUESTED:
            return
        safe_data = _sanitize_event_data(event_type, data)
        if event_type == "agent.state":
            self._remember_agent_progress(task_id, safe_data)
        metadata = _envelope_metadata(event_type, safe_data)
        event = self._publisher.publish(
            task_id, task.run_id, event_type, safe_data, **metadata
        )
        if event_type in {
            "plan.created",
            "plan.skipped",
            "assistant.commentary",
            # Keep the complete, bounded conversation projection.  The live
            # client already receives these frames, but history reload used to
            # discard them because they were never appended to run_index.
            "assistant.thinking",
            "model.started",
            "model.completed",
            "review.started",
            "review.completed",
            "review.skipped",
            "main_loop_rebuttal",
            "tool.requested",
            "tool.started",
            "tool.completed",
            "tool.failed",
            "message.completed",
        }:
            self._task_repo.update(
                task_id,
                lambda item: _append_run_index(item, event.to_dict(), safe_data),
            )

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
            phase="approval",
            status="pending",
            summary=tool_name,
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
            "interrupted": TaskStatus.INTERRUPTED,
            "blocked": TaskStatus.BLOCKED,
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
        error = _sanitize_terminal_error(message.get("error", {}))
        self._cancel_pending_approvals(task, stop_reason)
        self._task_repo.update(
            task_id,
            lambda item: _set_terminal(item, status, stop_reason, "", error=error),
        )
        terminal_event = {
            TaskStatus.COMPLETED: "task.completed",
            TaskStatus.CANCELLED: "task.cancelled",
            TaskStatus.FAILED: "task.failed",
            TaskStatus.INTERRUPTED: "task.interrupted",
            TaskStatus.BLOCKED: "task.blocked",
        }[status]
        if status is TaskStatus.COMPLETED:
            self._remember_terminal_answer(task_id, final_answer)
            self._publisher.publish(
                task_id,
                task.run_id,
                "message.completed",
                {"text": final_answer},
                phase="final",
                status="completed",
            )
        terminal_data = {
            "final_answer": final_answer,
            "stop_reason": stop_reason,
            **error,
        }
        terminal_envelope = self._publisher.publish(
            task_id,
            task.run_id,
            terminal_event,
            terminal_data,
            phase="final",
            status=status.value,
            summary=stop_reason,
        )
        self._task_repo.update(
            task_id,
            lambda item: _append_run_index(item, terminal_envelope.to_dict(), terminal_data),
        )
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

    def _fail_task(
        self, task_id: str, reason: str, *, status: TaskStatus = TaskStatus.FAILED
    ) -> None:
        interrupted = status == TaskStatus.INTERRUPTED
        with self._lock:
            try:
                task = self._task_repo.get(task_id)
                if task.status.terminal:
                    return
                self._cancel_pending_approvals(task, reason)
                self._task_repo.update(
                    task_id,
                    lambda item: _set_terminal(item, status, reason, ""),
                )
                self._publisher.publish(
                    task_id,
                    task.run_id,
                    "task.interrupted" if interrupted else "task.failed",
                    {"stop_reason": reason, "final_answer": ""},
                    phase="final",
                    status="interrupted" if interrupted else "failed",
                    summary=reason,
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
                phase="approval",
                status=ApprovalStatus.CANCELLED.value,
                summary=decision,
            )

    def _release(self, task_id: str, device_id: str) -> None:
        with self._lock:
            self._device_by_task.pop(task_id, None)
            active = self._active_by_device.get(device_id)
            if active is not None:
                active.discard(task_id)
                if not active:
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

    def send_task_message(self, device_id: str, task_id: str, content: str, wake: bool) -> None:
        self._send(
            device_id,
            {
                "type": "task.message",
                "task_id": task_id,
                "content": content,
                "wake": wake,
            },
        )


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


def _parse_update_status(raw) -> dict:
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise WorkerProtocolError("update_status must be an object")
    status = str(raw.get("status", ""))
    allowed = {
        "",
        "checking",
        "downloading",
        "retrying",
        "installing",
        "current",
        "failed",
        "auth_failed",
        "unsupported",
    }
    if status not in allowed:
        raise WorkerProtocolError("invalid Worker update status")
    try:
        downloaded_bytes = int(raw.get("downloaded_bytes", 0))
        total_bytes = int(raw.get("total_bytes", 0))
        bytes_per_second = int(raw.get("bytes_per_second", 0))
        retry_count = int(raw.get("retry_count", 0))
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("invalid Worker update progress") from exc
    if (
        downloaded_bytes < 0
        or total_bytes < 0
        or downloaded_bytes > 128 * 1024 * 1024
        or total_bytes > 128 * 1024 * 1024
        or bytes_per_second < 0
        or bytes_per_second > 1024 * 1024 * 1024
        or retry_count < 0
        or retry_count > 100
        or (total_bytes and downloaded_bytes > total_bytes)
    ):
        raise WorkerProtocolError("invalid Worker update progress")
    return {
        "status": status,
        "current_version": str(raw.get("current_version", ""))[:32],
        "target_version": str(raw.get("target_version", ""))[:32],
        "downloaded_bytes": downloaded_bytes,
        "total_bytes": total_bytes,
        "bytes_per_second": bytes_per_second,
        "retry_count": retry_count,
        "error": str(raw.get("error", ""))[:500],
        "updated_at": str(raw.get("updated_at", ""))[:40],
    }


def _parse_model_capabilities(raw, fallback_model: str) -> dict:
    if not isinstance(raw, dict):
        raise WorkerProtocolError("model_capabilities must be an object")
    provider = str(raw.get("provider", "openai-compatible")).strip()
    if not provider or len(provider) > 64:
        raise WorkerProtocolError("invalid model provider capability")
    raw_models = raw.get("models", [])
    if not isinstance(raw_models, list) or len(raw_models) > 20:
        raise WorkerProtocolError("model capabilities must contain a bounded model list")
    models = []
    allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise WorkerProtocolError("model capability must be an object")
        model_id = str(raw_model.get("id", "")).strip()
        display_name = str(raw_model.get("display_name", model_id)).strip()
        efforts = raw_model.get("reasoning_efforts", [])
        if (
            not model_id
            or len(model_id) > 200
            or not display_name
            or len(display_name) > 200
            or not isinstance(efforts, list)
        ):
            raise WorkerProtocolError("invalid model capability")
        normalized_efforts = list(dict.fromkeys(str(item).lower() for item in efforts))
        if not normalized_efforts or any(item not in allowed_efforts for item in normalized_efforts):
            raise WorkerProtocolError("invalid reasoning effort capability")
        models.append(
            {
                "id": model_id,
                "display_name": display_name,
                "reasoning_efforts": normalized_efforts,
            }
        )
    if not models and fallback_model:
        models = [{"id": fallback_model[:200], "display_name": fallback_model[:200], "reasoning_efforts": ["none"]}]
    return {"provider": provider, "models": models}


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
    display_name_source = str(raw.get("display_name_source", "auto"))
    display_name_updated_at = str(raw.get("display_name_updated_at", ""))[:40]
    first_request_at = str(raw.get("first_request_at", ""))[:40]
    message_total = raw.get("message_total", 0)
    if (
        not _SESSION_ID.fullmatch(session_id)
        or workspace_id not in workspace_ids
        or not isinstance(message_total, int)
        or message_total < 0
        or message_total > 10_000_000
        or display_name_source not in {"auto", "user"}
    ):
        raise WorkerProtocolError("invalid local session summary")
    return {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "display_name_source": display_name_source,
        "display_name_updated_at": display_name_updated_at,
        "first_request_at": first_request_at,
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


def _set_terminal(task, status: TaskStatus, stop_reason: str, final_answer: str, *, error=None):
    task.status = status
    task.stop_reason = stop_reason
    task.final_answer = final_answer or None
    task.pending_approval = None
    error = dict(error or {})
    task.error_stage = str(error.get("error_stage", ""))
    task.error_code = str(error.get("error_code", ""))
    task.error_retryable = bool(error.get("error_retryable", False))
    task.error_attempts = _nonnegative_int(error.get("error_attempts", 0))
    task.updated_at = utc_now()
    return task


def _clear_local_task_content(task):
    task.input = ""
    task.final_answer = None
    task.updated_at = utc_now()
    return task


def _append_run_index(task, event: dict, data: dict):
    event_type = str(event.get("type", ""))
    # §7.8.9 决策（2026-08-19）：model.heartbeat 不写入运行审计——心跳每秒一条,
    # 纯 keepalive/计时用途,持久化几百条会淹没真正的工具/审查/模型事件。
    if event_type == "model.heartbeat":
        return task
    labels = {
        "plan.created": "计划已创建",
        "plan.skipped": "直接回答",
        "assistant.commentary": "过程更新",
        "assistant.thinking": "思考",
        "model.started": "模型请求",
        "model.completed": "模型完成",
        "model.retrying": "模型重试",
        "model.protocol_retrying": "协议重试",
        "model.heartbeat": "模型心跳",
        "review.started": "开始审查",
        "review.completed": "审查完成",
        "review.skipped": "审查跳过",
        "main_loop_rebuttal": "主循环反驳",
        "tool.requested": "工具请求",
        "tool.started": "工具开始",
        "tool.completed": "工具完成",
        "tool.failed": "工具失败",
        "approval.required": "等待审批",
        "approval.resolved": "审批完成",
        "message.completed": "最终回答",
        "task.completed": "运行完成",
        "task.cancelled": "运行已取消",
        "task.failed": "运行失败",
        "task.interrupted": "运行已中断",
        "task.blocked": "运行受阻",
    }
    # Unified event contract: prefer envelope-level fields, fall back to the
    # redacted ``data`` payload for Worker events emitted before the contract
    # was lifted to the envelope.
    parent_event_id = str(event.get("parent_event_id") or data.get("parent_event_id", ""))
    started_at = str(event.get("started_at") or data.get("started_at", ""))
    ended_at = str(event.get("ended_at") or data.get("ended_at", ""))
    item = {
        "event_id": str(event.get("event_id", ""))[:64],
        "run_id": str(event.get("run_id", ""))[:128],
        "type": event_type[:64],
        "timestamp": str(event.get("timestamp", ""))[:40],
        "label": labels.get(event_type, event_type)[:64],
        "phase": str(event.get("phase") or event_phase(event_type))[:32],
    }
    if parent_event_id:
        item["parent_event_id"] = parent_event_id[:64]
    if started_at:
        item["started_at"] = started_at[:40]
    if ended_at:
        item["ended_at"] = ended_at[:40]
    summary = str(event.get("summary") or data.get("summary", ""))
    if summary:
        item["summary"] = summary[:1000]
    attempt = event.get("attempt", data.get("attempt"))
    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0:
        item["attempt"] = attempt
    if event_type.startswith("tool."):
        item["tool_name"] = str(data.get("tool_name", ""))[:100]
        item["tool_call_id"] = str(data.get("tool_call_id", ""))[:200]
        # §7.8.9 决策（2026-08-19）：审计持久化工具具体参数与结果——
        # 参数（args_preview，已脱敏限长）挂 tool.requested；结果预览
        # （result_preview，已脱敏限长）挂 tool.completed/tool.failed。
        if event_type == "tool.requested":
            args_preview = data.get("args_preview")
            if isinstance(args_preview, dict) and args_preview:
                item["args_preview"] = args_preview
        elif event_type in {"tool.completed", "tool.failed"}:
            result_preview = data.get("result_preview")
            if isinstance(result_preview, str) and result_preview:
                item["result_preview"] = result_preview[:8000]
                if data.get("result_truncated"):
                    item["result_truncated"] = True
    elif event_type == "model.completed":
        usage = data.get("usage", {})
        if isinstance(usage, dict):
            item["usage"] = {
                key: value
                for key, value in usage.items()
                if key in {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
        # §7.8.9 决策（2026-08-19）：模型回复进审计。
        text = str(data.get("text", ""))[:4000]
        if text:
            item["text"] = text
    elif event_type == "plan.created":
        item["intent"] = str(data.get("intent", ""))[:32]
        item["step_count"] = _nonnegative_int(data.get("step_count", 0))
    elif event_type == "message.completed":
        # §7.8.9 修正（2026-08-19）：终答进审计——message.completed 带最终回答文本。
        text = str(data.get("text", ""))[:4000]
        if text:
            item["text"] = text
    elif event_type == "assistant.thinking":
        # §7.8.9 决策（2026-08-19）：thinking 持久化——对话回放/审计可看思考过程。
        text = str(data.get("text", ""))[:4000]
        if text:
            item["text"] = text
    elif event_type == "assistant.commentary":
        # §7.8.9 修正（2026-08-19）：过程更新持久化——否则 run 结束历史重载后
        # 中途话（"我现在去查查XX"）会消失。
        text = str(data.get("text", ""))[:1000]
        if text:
            item["text"] = text
    elif event_type == "review.started":
        item["trigger"] = str(data.get("trigger", ""))[:32]
    elif event_type == "review.completed":
        # §7.8.9 决策（2026-08-19）：审查对抗明细持久化——verdict/feedback/
        # obstacles/reason 写进运行审计，前端可回放「谁判了什么、为什么」。
        item["verdict"] = str(data.get("verdict", ""))[:32]
        feedback = str(data.get("feedback", ""))[:500]
        if feedback:
            item["feedback"] = feedback
        item["reason"] = str(data.get("reason", ""))[:100]
        obstacles = data.get("obstacles", [])
        if isinstance(obstacles, list):
            item["obstacles"] = [str(item)[:100] for item in obstacles[:10]]
        item["tool_rounds"] = _nonnegative_int(data.get("tool_rounds", 0))
    elif event_type == "review.skipped":
        # §7.8.9 决策（2026-08-19）：只读任务 review 跳过——审计留痕避免
        # 「没有审查提示」的误解。
        item["reason"] = str(data.get("reason", ""))[:100]
    elif event_type == "main_loop_rebuttal":
        item["against_verdict"] = str(data.get("against_verdict", ""))[:32]
        item["action"] = str(data.get("action", ""))[:100]
        feedback = str(data.get("feedback", ""))[:500]
        if feedback:
            item["feedback"] = feedback
    elif event_type.startswith("task."):
        item["status"] = event_type.removeprefix("task.")[:32]
    task.run_index = [*task.run_index[-499:], item]
    task.updated_at = utc_now()
    return task


def _envelope_metadata(event_type: str, data: dict) -> dict:
    """Lift the unified event-contract fields out of the redacted payload.

    The Worker emits ``parent_event_id``/``started_at``/``ended_at``/``attempt``
    inside ``data`` (see ``local-worker`` RemoteExecutionHooks). The control
    plane re-exposes them on the public envelope so the frontend projects the
    timeline from one canonical source instead of re-parsing ``data``.
    """
    attempt = data.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool):
        attempt = None
    metadata = {
        "parent_event_id": str(data.get("parent_event_id", ""))[:64],
        "started_at": str(data.get("started_at", ""))[:64],
        "ended_at": str(data.get("ended_at", ""))[:64],
        "phase": event_phase(event_type),
        "attempt": attempt,
    }
    if event_type in {"tool.completed", "tool.failed"}:
        metadata["status"] = str(data.get("tool_status", ""))[:50]
        metadata["summary"] = str(data.get("tool_name", ""))[:100]
    elif event_type == "review.completed":
        metadata["status"] = str(data.get("status", ""))[:32]
    elif event_type == "plan.created":
        metadata["summary"] = str(data.get("summary", ""))[:500]
    elif event_type == "assistant.commentary":
        metadata["summary"] = str(data.get("text", ""))[:1000]
    elif event_type == "agent.state":
        metadata["phase"] = str(data.get("phase", ""))[:64] or event_phase(event_type)
        metadata["summary"] = str(data.get("next_step", ""))[:300]
    return {key: value for key, value in metadata.items() if value not in ("", None)}


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
            },
            "round_id": str(data.get("round_id", ""))[:64],
            "started_at": str(data.get("started_at", ""))[:64],
            "ended_at": str(data.get("ended_at", ""))[:64],
            # §7.8.9 决策（2026-08-19）：本轮模型回复进审计（已脱敏,截断）。
            "text": redact_artifact(str(data.get("text", ""))[:4000]),
        }
    if event_type == "model.started":
        return {
            "round": max(1, _nonnegative_int(data.get("round", 1))),
            "run_elapsed_seconds": _nonnegative_float(data.get("run_elapsed_seconds", 0.0)),
            "round_id": str(data.get("round_id", ""))[:64],
            "started_at": str(data.get("started_at", ""))[:64],
        }
    if event_type == "model.retrying":
        return {
            "stage": str(data.get("stage", ""))[:32],
            "attempt": max(1, _nonnegative_int(data.get("attempt", 1))),
            "max_attempts": max(1, _nonnegative_int(data.get("max_attempts", 1))),
            "error_code": str(data.get("error_code", ""))[:100],
            "retry_delay_seconds": _nonnegative_float(data.get("retry_delay_seconds", 0.0)),
            "elapsed_seconds": _nonnegative_float(data.get("elapsed_seconds", 0.0)),
            "reset_stream": bool(data.get("reset_stream", False)),
        }
    if event_type == "model.protocol_retrying":
        top_level_keys = data.get("top_level_keys", [])
        if not isinstance(top_level_keys, list):
            top_level_keys = []
        safe_keys = [
            str(key)
            for key in top_level_keys[:20]
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key))
        ]
        detected_format = str(data.get("detected_format", ""))[:32]
        if detected_format not in {
            "empty",
            "markdown_fence",
            "xml_tool",
            "xml_talk",
            "xml_final",
            "json_object",
            "json_array",
            "plain_text",
        }:
            detected_format = "unknown"
        response_hash = str(data.get("response_hash", ""))[:64].lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", response_hash):
            response_hash = ""
        return {
            "stage": str(data.get("stage", ""))[:32],
            "attempt": max(1, _nonnegative_int(data.get("attempt", 1))),
            "max_attempts": max(1, _nonnegative_int(data.get("max_attempts", 1))),
            "error_code": "model_protocol_invalid",
            "response_chars": _nonnegative_int(data.get("response_chars", 0)),
            "detected_format": detected_format,
            "top_level_keys": safe_keys,
            "response_hash": response_hash,
            "reset_stream": bool(data.get("reset_stream", True)),
        }
    if event_type == "model.heartbeat":
        return {
            "stage": str(data.get("stage", ""))[:32],
            "elapsed_seconds": _nonnegative_float(data.get("elapsed_seconds", 0.0)),
            "run_elapsed_seconds": _nonnegative_float(data.get("run_elapsed_seconds", 0.0)),
            "round": max(1, _nonnegative_int(data.get("round", 1))),
        }
    if event_type == "assistant.delta":
        return {"text": redact_artifact(str(data.get("text", ""))[:4000])}
    if event_type == "assistant.thinking":
        return {"text": redact_artifact(str(data.get("text", ""))[:4000])}
    if event_type == "assistant.commentary":
        return {"text": redact_artifact(str(data.get("text", ""))[:1000])}
    if event_type == "message.completed":
        # §7.8.9 修正（2026-08-19）：终答文本透传（脱敏截断）,进审计/前端。
        return {"text": redact_artifact(str(data.get("text", ""))[:4000])}
    if event_type == "plan.created":
        raw_steps = data.get("steps", [])
        steps = []
        if isinstance(raw_steps, list):
            for raw in raw_steps[:20]:
                if not isinstance(raw, dict):
                    continue
                dependencies = raw.get("dependencies", [])
                done_when = raw.get("done_when", [])
                steps.append(
                    {
                        "id": str(raw.get("id", ""))[:64],
                        "goal": str(raw.get("goal", ""))[:300],
                        "dependencies": [
                            str(item)[:64] for item in dependencies[:20]
                        ] if isinstance(dependencies, list) else [],
                        "done_when": [
                            str(item)[:300] for item in done_when[:20]
                        ] if isinstance(done_when, list) else [],
                    }
                )
        return redact_artifact(
            {
                "plan_id": str(data.get("plan_id", ""))[:64],
                "revision": _nonnegative_int(data.get("revision", 0)),
                "intent": str(data.get("intent", ""))[:32],
                "summary": str(data.get("summary", ""))[:500],
                "risk_level": str(data.get("risk_level", ""))[:16],
                "step_count": min(20, _nonnegative_int(data.get("step_count", 0))),
                "steps": steps,
            }
        )
    if event_type == "plan.skipped":
        return redact_artifact(
            {
                "reason": str(data.get("reason", ""))[:100],
                "intent": str(data.get("intent", ""))[:32],
                "summary": str(data.get("summary", ""))[:500],
            }
        )
    if event_type == "review.started":
        return {"attempt": _nonnegative_int(data.get("attempt", 0)), "trigger": str(data.get("trigger", ""))[:32]}
    if event_type == "review.skipped":
        # §7.8.9 决策（2026-08-19）：只读任务 review 跳过——reason 透传留痕。
        return {"reason": str(data.get("reason", ""))[:100]}
    if event_type == "review.completed":
        return redact_artifact(
            {
                "status": str(data.get("status", ""))[:32],
                "attempt": _nonnegative_int(data.get("attempt", 0)),
                "issue_count": _nonnegative_int(data.get("issue_count", 0)),
                # §7.8.9 决策（2026-08-18）：审查对抗明细——verdict/feedback/obstacles/
                # 工具轮数透传前端审查卡片（双向对抗前端展示的数据基础）。
                "trigger": str(data.get("trigger", ""))[:32],
                "verdict": str(data.get("verdict", ""))[:32],
                "feedback": str(data.get("feedback", ""))[:500],
                "reason": str(data.get("reason", ""))[:100],
                "obstacles": (
                    [str(item)[:100] for item in data.get("obstacles", [])[:10]]
                    if isinstance(data.get("obstacles"), list)
                    else []
                ),
                "tool_rounds": _nonnegative_int(data.get("tool_rounds", 0)),
                "review_shell_ok": bool(data.get("review_shell_ok", False)),
            }
        )
    if event_type == "main_loop_rebuttal":
        # §7.8.9 决策（2026-08-18）：主循环反驳 review 的「可以结束」——行动即理由。
        return redact_artifact(
            {
                "against_verdict": str(data.get("against_verdict", ""))[:32],
                "action": str(data.get("action", ""))[:100],
                "feedback": str(data.get("feedback", ""))[:500],
            }
        )
    if event_type == "agent.state":
        def bounded_list(value):
            return [str(item)[:300] for item in value[:20]] if isinstance(value, list) else []

        return redact_artifact(
            {
                "phase": str(data.get("phase", ""))[:64],
                "next_step": str(data.get("next_step", ""))[:300],
                "checklist": bounded_list(data.get("checklist", [])),
                "done_when": bounded_list(data.get("done_when", [])),
                "completed_items": bounded_list(data.get("completed_items", [])),
                "tool_steps": _nonnegative_int(data.get("tool_steps", 0)),
                "read_files": _nonnegative_int(data.get("read_files", 0)),
                "max_tool_steps": _nonnegative_int(data.get("max_tool_steps", 0)),
                "max_read_files": _nonnegative_int(data.get("max_read_files", 0)),
                "max_total_steps": _nonnegative_int(data.get("max_total_steps", 0)),
                "reason": str(data.get("reason", ""))[:100],
            }
        )
    safe = {
        "tool_call_id": str(data.get("tool_call_id", ""))[:200],
        "tool_name": str(data.get("tool_name", ""))[:100],
    }
    if event_type in {"tool.started", "tool.completed", "tool.failed"}:
        safe["parent_event_id"] = str(data.get("parent_event_id", ""))[:64]
        safe["started_at"] = str(data.get("started_at", ""))[:64]
    if event_type in {"tool.completed", "tool.failed"}:
        safe["tool_status"] = str(data.get("tool_status", ""))[:50]
        safe["tool_error_code"] = str(data.get("tool_error_code", ""))[:100]
        safe["ended_at"] = str(data.get("ended_at", ""))[:64]
        paths = data.get("affected_paths", [])
        safe["affected_paths"] = [
            path
            for item in paths[:100] if isinstance(paths, list)
            if (path := _safe_relative_path(item)) is not None
        ] if isinstance(paths, list) else []
        result_preview, result_truncated = public_tool_result_preview(
            safe["tool_name"], data.get("result_preview", "")
        )
        if result_preview:
            safe["result_preview"] = result_preview
            safe["result_truncated"] = result_truncated or bool(data.get("result_truncated", False))
    if event_type == "tool.requested":
        args_preview = public_tool_args_preview(safe["tool_name"], data.get("args_preview", {}))
        if args_preview:
            safe["args_preview"] = args_preview
    if event_type == "policy.violation":
        safe["policy_code"] = str(data.get("policy_code", ""))[:100]
    if event_type.startswith("sandbox."):
        return redact_artifact(
            {
                "container": str(data.get("container", ""))[:64],
                "image": str(data.get("image", ""))[:200],
                "reason": str(data.get("reason", ""))[:100],
                "exit_code": _nonnegative_int(data.get("exit_code", -1))
                if isinstance(data.get("exit_code"), int)
                else -1,
            }
        )
    return redact_artifact(safe)


def _sanitize_terminal_error(value) -> dict:
    if not isinstance(value, dict):
        return {}
    code = str(value.get("code", ""))[:100]
    if not code:
        return {}
    return {
        "error_stage": str(value.get("stage", ""))[:64],
        "error_code": code,
        "error_retryable": bool(value.get("retryable", False)),
        "error_attempts": min(10, _nonnegative_int(value.get("attempts", 0))),
        "error_detail": str(value.get("detail", ""))[:500],
    }


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _nonnegative_float(value) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _safe_relative_path(value) -> str | None:
    text = str(value).replace("\\", "/")
    if not text or len(text) > 500 or text.startswith(("/", "~/")):
        return None
    head = text.split("/", 1)[0]
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if ":" in head or ".." in parts:
        return None
    return "/".join(parts)

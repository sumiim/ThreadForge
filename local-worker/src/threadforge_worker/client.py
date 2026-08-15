"""Pairing HTTP client and outbound Worker WebSocket loop."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pico.approval import strategy_for_mode
from pico.security import redact_artifact
from pico.session_store import SessionStore
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus
from websockets.sync.client import connect

from . import __version__
from .config import (
    ConfigStore,
    WorkerConfig,
    WorkspaceConfigWriteError,
    WorkspacePathError,
)
from .runtime import ActiveRun, CancellationToken, RemoteApprovalStrategy, run_task
from .updater import apply_update, load_update_status

_SESSION_ID = re.compile(r"^ses_[a-f0-9]{32}$")
_RUN_ID = re.compile(r"^run_[a-f0-9]{32}$")
_SESSION_SYNC_CHUNK_SIZE = 100
_MESSAGE_CONTENT_MAX = 4000
_HISTORY_PAYLOAD_CONTENT_BUDGET = 1700 * 1024
WORKER_PROTOCOL_VERSION = 1
_UPDATE_RETRY_COOLDOWN_SECONDS = 300
LOGGER = logging.getLogger(__name__)


class WorkerProtocolRejectedError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code or "unknown"
        self.message = message.strip()
        detail = f": {self.message}" if self.message else ""
        super().__init__(f"Worker protocol rejected: {self.code}{detail}")


def pair(server_url: str, code: str, name: str) -> dict:
    server_url = _validated_server_url(server_url)
    payload = json.dumps({"code": code, "name": name}).encode("utf-8")
    request = urllib.request.Request(
        server_url.rstrip("/") + "/api/v1/workers/pair",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "ThreadForge-Worker"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.load(response)
    if not isinstance(body, dict) or not body.get("device_token"):
        raise RuntimeError("server returned an invalid pairing response")
    return body


class WorkerClient:
    def __init__(
        self,
        store: ConfigStore,
        config: WorkerConfig,
        *,
        workspace_selector: Callable[[str], str | None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        uninstall_callback: Callable[[], None] | None = None,
    ):
        self.store = store
        self.config = config
        self.active_runs: dict[str, ActiveRun] = {}
        self._socket = None
        self._send_lock = threading.Lock()
        self._workspace_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._workspace_selector = workspace_selector
        self._status_callback = status_callback
        self._uninstall_callback = uninstall_callback
        self._stop_event = threading.Event()
        self._updating = threading.Event()
        self._ready_event = threading.Event()
        self._last_update_failure_at = 0.0

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def stop(self) -> None:
        self._stop_event.set()
        with self._activity_lock:
            runs = list(self.active_runs.values())
        for active in runs:
            active.cancel(5)
        websocket = self._socket
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass

    def begin_update(self) -> bool:
        with self._activity_lock:
            if self.active_runs or self._updating.is_set():
                return False
            self._updating.set()
            return True

    def end_update(self) -> None:
        with self._activity_lock:
            self._updating.clear()

    def wait_for_stop(self, timeout_seconds: float) -> bool:
        return self._stop_event.wait(timeout_seconds)

    def report_update_status(self, status: dict) -> None:
        if not self._ready_event.is_set():
            return
        try:
            self._send({"type": "update.status", **status})
        except RuntimeError:
            # The current state is also persisted locally and included in the
            # next hello, so a disconnected control plane does not lose it.
            pass

    def sync_workspaces(self) -> bool:
        self.config = self.store.load()
        try:
            self._send(
                {
                    "type": "workspaces.updated",
                    "workspaces": [item.public_dict() for item in self.config.workspaces],
                }
            )
            return True
        except RuntimeError:
            return False

    def run_forever(self) -> None:
        if not self.config.device_id or not self.config.device_token:
            raise RuntimeError("Worker is not paired; run `threadforge-worker pair` first")
        self.store.load_model_env()
        delay = 1.0
        while not self._stop_event.is_set():
            self._set_status("connecting")
            try:
                self._run_once()
                reason = "server closed the connection"
            except ConnectionClosed as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None)
                if code in {4001, 4003, 4400, 4401, 4403}:
                    LOGGER.error("Worker connection was rejected or revoked (code %s)", code)
                    self._set_status("rejected")
                    return
                reason = f"connection closed (code {code or 'unknown'})"
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status not in {408, 425, 429} and not 500 <= status < 600:
                    LOGGER.error("Worker WebSocket handshake was rejected (HTTP %s)", status)
                    self._set_status("rejected")
                    return
                reason = f"temporary WebSocket handshake failure (HTTP {status})"
            except WorkerProtocolRejectedError as exc:
                LOGGER.error("%s", exc)
                self._set_status("rejected")
                return
            # A reverse proxy or tunnel can close the socket before it has
            # written an HTTP status line.  websockets reports that as
            # InvalidMessage rather than OSError; it is still a transient
            # transport failure and must not terminate the Worker service.
            except (InvalidMessage, OSError, TimeoutError) as exc:
                reason = str(exc) or type(exc).__name__
            if self._stop_event.is_set():
                break
            self._set_status("retrying")
            print(f"Worker disconnected: {reason}; retrying in {delay:g}s")
            if self._stop_event.wait(delay):
                break
            delay = min(delay * 2, 30.0)
        self._set_status("stopped")

    def _run_once(self) -> None:
        uri = _websocket_url(self.config.server_url) + "/api/v1/workers/connect"
        with connect(
            uri,
            additional_headers={"Authorization": f"Bearer {self.config.device_token}"},
            max_size=2 * 1024 * 1024,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self._socket = websocket
            self._ready_event.clear()
            self._send(
                {
                    "type": "hello",
                    "version": __version__,
                    "protocol_version": WORKER_PROTOCOL_VERSION,
                    "platform": platform.system().lower(),
                    "architecture": platform.machine().lower(),
                    "model": os.environ.get("PICO_OPENAI_MODEL", "gpt-5.4"),
                    "model_configured": bool(os.environ.get("PICO_OPENAI_API_KEY", "").strip()),
                    "model_provider": os.environ.get("PICO_MODEL_PROVIDER", ""),
                    "orchestration_backend": "langgraph-v1.1",
                    "update_status": load_update_status(self.store),
                    "model_capabilities": _model_capabilities(),
                    "capabilities": [
                        "local_history",
                        "model_configuration",
                        "auto_update",
                        "resumable_auto_update",
                        "langgraph_v1_1",
                        "run_model_settings",
                        "model_streaming_sse",
                        "rename_entities",
                        "delete_entities",
                        *(["worker_uninstall"] if self._uninstall_callback else []),
                        *(["workspace_selection"] if self._workspace_selector else []),
                    ],
                    "workspaces": [workspace.public_dict() for workspace in self.config.workspaces],
                }
            )
            self._set_status("online")
            try:
                for raw in websocket:
                    message = json.loads(raw)
                    if isinstance(message, dict):
                        self._handle(message)
            finally:
                with self._activity_lock:
                    runs = list(self.active_runs.values())
                for active in runs:
                    active.cancel(5)
                for active in runs:
                    if active.thread is not None:
                        active.thread.join()
                self._socket = None
                self._ready_event.clear()

    def _handle(self, message: dict) -> None:
        message_type = str(message.get("type", ""))
        if message_type == "task.start":
            self._start_task(message.get("task", {}))
        elif message_type == "task.cancel":
            active = self.active_runs.get(str(message.get("task_id", "")))
            if active is not None:
                active.cancel(5)
        elif message_type == "approval.decision":
            active = self.active_runs.get(str(message.get("task_id", "")))
            if active is not None:
                active.approval.resolve(
                    str(message.get("tool_call_id", "")),
                    str(message.get("decision", "")),
                    str(message.get("args_digest", "")),
                )
        elif message_type == "workspace.select":
            self._start_workspace_selection(
                str(message.get("request_id", "")),
                str(message.get("expires_at", "")),
            )
        elif message_type == "session.history.get":
            self._start_history_read(message)
        elif message_type == "model.configure":
            self._configure_model(message)
        elif message_type == "entity.rename":
            self._rename_entity(message)
        elif message_type == "entity.delete":
            self._delete_entity(message)
        elif message_type == "worker.uninstall":
            self._uninstall_worker(message)
        elif message_type == "worker.update":
            self._request_update(message)
        elif message_type == "hello.ack":
            self._ready_event.set()
            self._start_session_sync()
        elif message_type == "sessions.updated.ack":
            rejected = message.get("rejected", [])
            if isinstance(rejected, list) and rejected:
                LOGGER.warning(
                    "Server isolated %d invalid local session summaries; see server logs for details",
                    len(rejected),
                )
            return
        elif message_type in {
            "heartbeat.ack",
            "workspaces.updated.ack",
            "workspace.selection.ack",
            "model.configuration.ack",
            "entity.delete.ack",
            "worker.uninstall.ack",
            "worker.update.ack",
            "approval.registered",
            "update.status.ack",
        }:
            return
        elif message_type == "protocol.error":
            raise WorkerProtocolRejectedError(
                str(message.get("code", "unknown")),
                str(message.get("message", "")),
            )

    def _start_workspace_selection(self, request_id: str, expires_at: str) -> None:
        if not request_id or self._workspace_selector is None:
            self._send_workspace_selection_result(request_id, "failed", error="companion_required")
            return
        thread = threading.Thread(
            target=self._select_workspace,
            args=(request_id, expires_at),
            name=f"workspace-selection-{request_id}",
            daemon=True,
        )
        thread.start()

    def _start_session_sync(self) -> None:
        threading.Thread(
            target=self._sync_sessions,
            name="worker-session-index-sync",
            daemon=True,
        ).start()

    def _sync_sessions(self) -> None:
        summaries = self._session_summaries()
        chunks = [
            summaries[index : index + _SESSION_SYNC_CHUNK_SIZE]
            for index in range(0, len(summaries), _SESSION_SYNC_CHUNK_SIZE)
        ] or [[]]
        for index, chunk in enumerate(chunks):
            try:
                self._send(
                    {
                        "type": "sessions.updated",
                        "sessions": chunk,
                        "complete": index == len(chunks) - 1,
                    }
                )
            except RuntimeError:
                return

    def _session_summaries(self) -> list[dict]:
        session_store = SessionStore(self.store.root / "sessions")
        summaries = []
        for session_id in session_store.list_ids():
            if not _SESSION_ID.fullmatch(session_id):
                continue
            try:
                session = session_store.load(session_id)
            except Exception:
                continue
            workspace_id = str(session.get("workspace_id", ""))
            if not any(item.workspace_id == workspace_id for item in self.config.workspaces):
                continue
            summaries.append(
                {
                    "session_id": session_id,
                    "workspace_id": workspace_id,
                    "title": str(session.get("title", ""))[:200],
                    "created_at": str(session.get("created_at", ""))[:40],
                    "updated_at": str(session.get("updated_at", ""))[:40],
                    "display_name_source": str(session.get("display_name_source", "auto"))[:16],
                    "display_name_updated_at": str(
                        session.get("display_name_updated_at", session.get("created_at", ""))
                    )[:40],
                    "first_request_at": str(session.get("first_request_at", ""))[:40],
                    "message_total": len(session.get("history", [])),
                }
            )
        # The central SessionStore orders by file mtime. Sending oldest first
        # preserves the Worker's newest-first order on a fresh control plane.
        return list(reversed(summaries))

    def _start_history_read(self, message: dict) -> None:
        threading.Thread(
            target=self._read_history,
            args=(dict(message),),
            name="worker-history-read",
            daemon=True,
        ).start()

    def _read_history(self, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        session_id = str(message.get("session_id", ""))
        try:
            if not request_id or not _SESSION_ID.fullmatch(session_id):
                raise ValueError("invalid history request")
            message_limit = max(1, min(int(message.get("message_limit", 100)), 500))
            session = SessionStore(self.store.root / "sessions").load(session_id)
            history = session.get("history", [])
            if not isinstance(history, list):
                raise TypeError("invalid local session history")
            recent = [item for item in history[-message_limit:] if isinstance(item, dict)]
            content_byte_limit = max(
                256,
                _HISTORY_PAYLOAD_CONTENT_BUDGET // max(1, len(recent)),
            )
            messages = [
                {
                    "role": str(item.get("role", ""))[:32],
                    "name": str(item.get("name", ""))[:100],
                    "content": _clip_utf8(
                        str(redact_artifact(item.get("content", "")))[:_MESSAGE_CONTENT_MAX],
                        content_byte_limit,
                    ),
                    "created_at": str(item.get("created_at", ""))[:40],
                }
                for item in recent
            ]
            response = {
                "type": "session.history.result",
                "request_id": request_id,
                "session_id": session_id,
                "status": "completed",
                "message_total": len(history),
                "messages": messages,
            }
        except Exception:
            response = {
                "type": "session.history.result",
                "request_id": request_id,
                "session_id": session_id,
                "status": "failed",
                "error": "history_unavailable",
            }
        try:
            self._send(response)
        except RuntimeError:
            pass

    def _configure_model(self, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        try:
            self.store.save_model_env(
                base_url=str(message.get("base_url", "")),
                api_key=str(message.get("api_key", "")),
                model=str(message.get("model", "")),
                model_provider=str(message.get("model_provider", "")),
            )
            response = {
                "type": "model.configuration.completed",
                "request_id": request_id,
                "status": "completed",
                "model": os.environ.get("PICO_OPENAI_MODEL", ""),
                "model_provider": os.environ.get("PICO_MODEL_PROVIDER", ""),
                "model_capabilities": _model_capabilities(),
            }
        except Exception:
            response = {
                "type": "model.configuration.completed",
                "request_id": request_id,
                "status": "failed",
                "error": "model_configuration_invalid",
            }
        self._send(response)

    def _rename_entity(self, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        entity_type = str(message.get("entity_type", ""))
        entity_id = str(message.get("entity_id", ""))
        display_name = str(message.get("display_name", "")).strip()
        response = {
            "type": "entity.rename.completed",
            "request_id": request_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "display_name": display_name,
        }
        try:
            if not request_id or not display_name or len(display_name) > 200:
                raise ValueError("invalid rename request")
            if entity_type == "workspace":
                self.config = self.store.load()
                workspace = next(
                    (item for item in self.config.workspaces if item.workspace_id == entity_id),
                    None,
                )
                if workspace is None:
                    raise ValueError("workspace not found")
                previous = workspace.name
                workspace.name = display_name
                try:
                    self.store.save_workspaces(self.config)
                except Exception:
                    workspace.name = previous
                    raise
                response["workspaces"] = [item.public_dict() for item in self.config.workspaces]
            elif entity_type == "session":
                if not _SESSION_ID.fullmatch(entity_id):
                    raise ValueError("session not found")
                session_store = SessionStore(self.store.root / "sessions")
                session = session_store.load(entity_id)
                if not any(
                    item.workspace_id == session.get("workspace_id")
                    for item in self.config.workspaces
                ):
                    raise ValueError("session workspace is unavailable")
                session["title"] = display_name
                session_store.save(session)
            else:
                raise ValueError("unsupported rename entity")
            response["status"] = "completed"
        except Exception:
            response["status"] = "failed"
            response["error"] = "rename_failed"
        try:
            self._send(response)
            if response["status"] == "completed" and entity_type == "session":
                self._start_session_sync()
        except RuntimeError:
            pass

    def _delete_entity(self, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        entity_type = str(message.get("entity_type", ""))
        entity_id = str(message.get("entity_id", ""))
        requested_sessions = message.get("session_ids", [])
        requested_runs = message.get("run_ids", [])
        response = {
            "type": "entity.delete.completed",
            "request_id": request_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        try:
            if not request_id or not isinstance(requested_sessions, list) or not isinstance(requested_runs, list):
                raise ValueError("invalid delete request")
            if len(requested_sessions) > 1000 or len(requested_runs) > 1000:
                raise ValueError("delete request is too large")
            session_ids = {str(item) for item in requested_sessions}
            run_ids = {str(item) for item in requested_runs}
            if any(not _SESSION_ID.fullmatch(item) for item in session_ids):
                raise ValueError("invalid session id")
            if any(not _RUN_ID.fullmatch(item) for item in run_ids):
                raise ValueError("invalid run id")
            self.config = self.store.load()
            with self._activity_lock:
                busy = any(
                    (entity_type == "session" and active.session_id == entity_id)
                    or (entity_type == "workspace" and active.workspace_id == entity_id)
                    for active in self.active_runs.values()
                )
                if busy:
                    raise RuntimeError("entity_busy")
            if entity_type == "session":
                if not _SESSION_ID.fullmatch(entity_id):
                    raise ValueError("session not found")
                session_ids.add(entity_id)
            elif entity_type == "workspace":
                workspace = next(
                    (item for item in self.config.workspaces if item.workspace_id == entity_id),
                    None,
                )
                if workspace is None:
                    raise ValueError("workspace not found")
                session_store = SessionStore(self.store.root / "sessions")
                for session_id in session_store.list_ids():
                    if not _SESSION_ID.fullmatch(session_id):
                        continue
                    session = session_store.load(session_id)
                    if session.get("workspace_id") == entity_id:
                        session_ids.add(session_id)
            else:
                raise ValueError("unsupported delete entity")

            self._delete_run_artifacts(run_ids)
            session_store = SessionStore(self.store.root / "sessions")
            for session_id in session_ids:
                if session_store.exists(session_id):
                    session_store.delete(session_id)
            if entity_type == "workspace" and not self.store.remove_workspace(self.config, entity_id):
                raise ValueError("workspace not found")
            response.update(
                {
                    "status": "completed",
                    "deleted_session_ids": sorted(session_ids),
                    "workspaces": [item.public_dict() for item in self.config.workspaces],
                }
            )
        except RuntimeError as exc:
            response["status"] = "failed"
            response["error"] = "entity_busy" if str(exc) == "entity_busy" else "delete_failed"
        except Exception:
            response["status"] = "failed"
            response["error"] = "delete_failed"
        try:
            self._send(response)
            if response["status"] == "completed":
                self._start_session_sync()
        except RuntimeError:
            pass

    def _delete_run_artifacts(self, run_ids: set[str]) -> None:
        runs_root = (self.store.root / "runs").resolve()
        for run_id in run_ids:
            run_dir = (runs_root / run_id).resolve()
            if run_dir.parent != runs_root:
                raise ValueError("invalid run artifact path")
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=False)

    def _request_update(self, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        response = {
            "type": "worker.update.completed",
            "request_id": request_id,
        }
        if not request_id:
            response.update(status="failed", error="update_unavailable")
        elif self._update_backoff_active():
            response.update(status="failed", error="update_backoff")
        elif not self.begin_update():
            response.update(status="failed", error="worker_busy")
        else:
            response["status"] = "started"
            self._send(response)
            threading.Thread(
                target=self._run_manual_update,
                name=f"worker-update-{request_id}",
                daemon=True,
            ).start()
            return
        self._send(response)

    def _update_backoff_active(self) -> bool:
        if self._last_update_failure_at <= 0:
            return False
        return time.monotonic() - self._last_update_failure_at < _UPDATE_RETRY_COOLDOWN_SECONDS

    def _run_manual_update(self) -> None:
        try:
            updated = apply_update(self.store, self.report_update_status)
            if updated:
                # The installer replaces the binaries after this process exits.
                self.stop()
        except Exception as exc:
            # 失败后进入冷却期，避免服务器反复下发 worker.update 把设备卡在 updating。
            self._last_update_failure_at = time.monotonic()
            LOGGER.warning("Manual Worker update failed: %s", exc)
        finally:
            self.end_update()

    def _uninstall_worker(self, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        response = {
            "type": "worker.uninstall.completed",
            "request_id": request_id,
        }
        with self._activity_lock:
            busy = bool(self.active_runs) or self._updating.is_set()
        if not request_id or self._uninstall_callback is None:
            response.update(status="failed", error="uninstall_unavailable")
        elif busy:
            response.update(status="failed", error="worker_busy")
        else:
            response["status"] = "completed"
        self._send(response)
        if response["status"] != "completed":
            return

        def launch() -> None:
            time.sleep(0.25)
            try:
                self._uninstall_callback()
            finally:
                self.stop()

        threading.Thread(target=launch, name="worker-uninstaller", daemon=True).start()

    def _select_workspace(self, request_id: str, expires_at: str) -> None:
        if not self._workspace_lock.acquire(blocking=False):
            self._send_workspace_selection_result(request_id, "failed", error="selection_busy")
            return
        try:
            selected_path = self._workspace_selector(expires_at) if self._workspace_selector else None
            if not selected_path:
                self._send_workspace_selection_result(request_id, "cancelled")
                return
            if _timestamp_expired(expires_at):
                self._send_workspace_selection_result(
                    request_id, "failed", error="selection_expired"
                )
                return
            # Reuse the configuration that was loaded by this live service.
            # Reading worker.json again here races with installer/reconnect
            # activity and can fail while the service is still healthy.
            config = self.config
            workspace = self.store.add_workspace(config, selected_path)
            self.config = config
            self._send_workspace_selection_result(
                request_id,
                "selected",
                workspace_id=workspace.workspace_id,
            )
        except WorkspacePathError:
            LOGGER.exception("Selected workspace path is unavailable")
            self._send_workspace_selection_result(
                request_id, "failed", error="workspace_path_unavailable"
            )
        except WorkspaceConfigWriteError:
            LOGGER.exception("Worker workspace configuration could not be saved")
            self._send_workspace_selection_result(
                request_id, "failed", error="workspace_config_write_failed"
            )
        except RuntimeError as exc:
            error = str(exc)
            if error not in {
                "native_directory_picker_unavailable",
                "native_directory_picker_failed",
            }:
                error = "selection_failed"
            self._send_workspace_selection_result(request_id, "failed", error=error)
        except Exception:
            LOGGER.exception("Unexpected workspace registration failure")
            self._send_workspace_selection_result(request_id, "failed", error="workspace_registration_failed")
        finally:
            self._workspace_lock.release()

    def _send_workspace_selection_result(
        self,
        request_id: str,
        status: str,
        *,
        workspace_id: str = "",
        error: str = "",
    ) -> None:
        message = {
            "type": "workspace.selection.completed",
            "request_id": request_id,
            "status": status,
        }
        if workspace_id:
            message["workspace_id"] = workspace_id
            message["workspaces"] = [item.public_dict() for item in self.config.workspaces]
        if error:
            message["error"] = error
        try:
            self._send(message)
        except RuntimeError:
            pass

    def _set_status(self, status: str) -> None:
        if self._status_callback is not None:
            try:
                self._status_callback(status)
            except Exception:
                pass

    def _start_task(self, task: dict) -> None:
        workspace_id = str(task.get("workspace_id", ""))
        workspace = next((item for item in self.config.workspaces if item.workspace_id == workspace_id), None)
        if workspace is None:
            session_id = str(task.get("session_id", ""))
            self._send(
                {
                    "type": "terminal",
                    "task_id": task.get("task_id", ""),
                    "status": "failed",
                    "stop_reason": "workspace_not_found",
                    "final_answer": "",
                    "message_total": self._local_message_total(session_id),
                    "session_persisted": self._local_session_exists(session_id),
                }
            )
            return

        with self._activity_lock:
            updating = self._updating.is_set()
            if not updating:
                token = CancellationToken()
                approval = strategy_for_mode(
                    str(task.get("permission_mode", "default")),
                    RemoteApprovalStrategy(self._send, str(task["task_id"]), token),
                )
                active = ActiveRun(
                    task_id=str(task["task_id"]),
                    token=token,
                    approval=approval,
                    session_id=str(task.get("session_id", "")),
                    workspace_id=workspace_id,
                )
                self.active_runs[str(task["task_id"])] = active

        if updating:
            session_id = str(task.get("session_id", ""))
            self._send(
                {
                    "type": "terminal",
                    "task_id": task.get("task_id", ""),
                    "status": "failed",
                    "stop_reason": "worker_updating",
                    "final_answer": "",
                    "message_total": self._local_message_total(
                        str(task.get("session_id", ""))
                    ),
                    "session_persisted": self._local_session_exists(
                        str(task.get("session_id", ""))
                    ),
                }
            )
            return

        def target() -> None:
            try:
                run_task(
                    task=task,
                    workspace_path=Path(workspace.path),
                    data_dir=self.store.root,
                    send=self._send,
                    active=active,
                )
            except Exception as exc:
                self._send(
                    {
                        "type": "terminal",
                        "task_id": task.get("task_id", ""),
                        "status": "failed",
                        "stop_reason": _stable_failure_reason(exc),
                        "final_answer": "",
                        "message_total": self._local_message_total(
                            str(task.get("session_id", ""))
                        ),
                        "session_persisted": self._local_session_exists(
                            str(task.get("session_id", ""))
                        ),
                    }
                )
            finally:
                with self._activity_lock:
                    if self.active_runs.get(active.task_id) is active:
                        self.active_runs.pop(active.task_id, None)

        active.thread = threading.Thread(target=target, name=f"worker-{active.task_id}", daemon=True)
        active.thread.start()

    def _local_message_total(self, session_id: str) -> int:
        if not self._local_session_exists(session_id):
            return 0
        session_store = SessionStore(self.store.root / "sessions")
        try:
            history = session_store.load(session_id).get("history", [])
            return len(history) if isinstance(history, list) else 0
        except Exception:
            return 0

    def _local_session_exists(self, session_id: str) -> bool:
        return bool(
            _SESSION_ID.fullmatch(session_id)
            and SessionStore(self.store.root / "sessions").exists(session_id)
        )

    def _send(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 2 * 1024 * 1024:
            raise RuntimeError("Worker message exceeds 2 MiB")
        with self._send_lock:
            if self._socket is None:
                raise RuntimeError("Worker is disconnected")
            self._socket.send(payload)


def _websocket_url(server_url: str) -> str:
    parsed = urllib.parse.urlsplit(_validated_server_url(server_url))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _validated_server_url(server_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(server_url).strip())
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not loopback)
    ):
        raise ValueError("Worker server must use HTTPS, except for loopback development")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def _stable_failure_reason(exc: Exception) -> str:
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "invalid_task_payload"
    if isinstance(exc, RuntimeError) and "not configured" in str(exc):
        return "model_not_configured"
    return "worker_runtime_error"


def _timestamp_expired(value: str) -> bool:
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            return False
        return expires <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _clip_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _runtime_reasoning_efforts() -> tuple[str, ...]:
    from .runtime import _supported_reasoning_efforts

    return _supported_reasoning_efforts()


def _model_capabilities() -> dict:
    """Worker-level model capability report.

    ``max_output_tokens`` mirrors the runtime's default output budget
    (``runtime.py`` ``max_new_tokens`` default 512) rather than a probed
    provider limit — provider-limit probing is a V1.2 (2.2) concern.
    ``usage_fields`` are the completion-metadata fields the client reports.
    """
    model = os.environ.get("PICO_OPENAI_MODEL", "gpt-5.4")
    model_provider = os.environ.get("PICO_MODEL_PROVIDER", "").strip().lower()
    efforts = list(_runtime_reasoning_efforts())
    if model_provider == "chat_completions":
        provider_label = "chat-completions"
    elif model_provider == "anthropic":
        provider_label = "anthropic"
    else:
        provider_label = "openai-compatible"
    return {
        "provider": provider_label,
        "models": [
            {
                "id": model,
                "display_name": model,
                "reasoning_efforts": efforts,
                "max_output_tokens": 512,
                "usage_fields": [
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "cache_hit",
                ],
                "supports_temperature": "none" in efforts,
            }
        ],
    }

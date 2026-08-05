"""Pairing HTTP client and outbound Worker WebSocket loop."""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from pico.security import redact_artifact
from pico.session_store import SessionStore
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from . import __version__
from .config import ConfigStore, WorkerConfig
from .runtime import ActiveRun, CancellationToken, RemoteApprovalStrategy, run_task

_SESSION_ID = re.compile(r"^ses_[a-f0-9]{32}$")
_SESSION_SYNC_CHUNK_SIZE = 100
_MESSAGE_CONTENT_MAX = 4000
_HISTORY_PAYLOAD_CONTENT_BUDGET = 1700 * 1024
WORKER_PROTOCOL_VERSION = 1


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
        workspace_selector: Callable[[], str | None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        ready_callback: Callable[[], None] | None = None,
    ):
        self.store = store
        self.config = config
        self.active: ActiveRun | None = None
        self._socket = None
        self._send_lock = threading.Lock()
        self._workspace_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._workspace_selector = workspace_selector
        self._status_callback = status_callback
        self._ready_callback = ready_callback
        self._stop_event = threading.Event()
        self._updating = threading.Event()
        self._update_started = threading.Event()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def stop(self) -> None:
        self._stop_event.set()
        active = self.active
        if active is not None:
            active.cancel(5)
        websocket = self._socket
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass

    def begin_update(self) -> bool:
        with self._activity_lock:
            if self.active is not None or self._updating.is_set():
                return False
            self._updating.set()
            return True

    def end_update(self) -> None:
        with self._activity_lock:
            self._updating.clear()

    def wait_for_stop(self, timeout_seconds: float) -> bool:
        return self._stop_event.wait(timeout_seconds)

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
                    raise RuntimeError(f"Worker connection was rejected or revoked (code {code})") from exc
                reason = f"connection closed (code {code or 'unknown'})"
            except (OSError, TimeoutError) as exc:
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
            self._send(
                {
                    "type": "hello",
                    "version": __version__,
                    "protocol_version": WORKER_PROTOCOL_VERSION,
                    "platform": platform.system().lower(),
                    "model": os.environ.get("PICO_OPENAI_MODEL", "gpt-5.4"),
                    "model_configured": bool(os.environ.get("PICO_OPENAI_API_KEY", "").strip()),
                    "capabilities": [
                        "local_history",
                        "model_configuration",
                        "auto_update",
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
                active = self.active
                if active is not None:
                    active.cancel(5)
                    if active.thread is not None:
                        active.thread.join()
                self._socket = None

    def _handle(self, message: dict) -> None:
        message_type = str(message.get("type", ""))
        if message_type == "task.start":
            self._start_task(message.get("task", {}))
        elif message_type == "task.cancel":
            if self.active is not None and self.active.task_id == message.get("task_id"):
                self.active.cancel(5)
        elif message_type == "approval.decision":
            if self.active is not None and self.active.task_id == message.get("task_id"):
                self.active.approval.resolve(
                    str(message.get("tool_call_id", "")),
                    str(message.get("decision", "")),
                    str(message.get("args_digest", "")),
                )
        elif message_type == "workspace.select":
            self._start_workspace_selection(str(message.get("request_id", "")))
        elif message_type == "session.history.get":
            self._start_history_read(message)
        elif message_type == "model.configure":
            self._configure_model(message)
        elif message_type == "hello.ack":
            self._start_session_sync()
            if self._ready_callback is not None and not self._update_started.is_set():
                self._update_started.set()
                threading.Thread(
                    target=self._ready_callback,
                    name="worker-auto-update",
                    daemon=True,
                ).start()
        elif message_type in {
            "heartbeat.ack",
            "workspaces.updated.ack",
            "sessions.updated.ack",
            "workspace.selection.ack",
            "model.configuration.ack",
            "approval.registered",
        }:
            return
        elif message_type == "protocol.error":
            raise RuntimeError(f"Worker protocol rejected: {message.get('code', 'unknown')}")

    def _start_workspace_selection(self, request_id: str) -> None:
        if not request_id or self._workspace_selector is None:
            self._send_workspace_selection_result(request_id, "failed", error="companion_required")
            return
        thread = threading.Thread(
            target=self._select_workspace,
            args=(request_id,),
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
            )
            response = {
                "type": "model.configuration.completed",
                "request_id": request_id,
                "status": "completed",
                "model": os.environ.get("PICO_OPENAI_MODEL", ""),
            }
        except Exception:
            response = {
                "type": "model.configuration.completed",
                "request_id": request_id,
                "status": "failed",
                "error": "model_configuration_invalid",
            }
        self._send(response)

    def _select_workspace(self, request_id: str) -> None:
        if not self._workspace_lock.acquire(blocking=False):
            self._send_workspace_selection_result(request_id, "failed", error="selection_busy")
            return
        try:
            selected_path = self._workspace_selector() if self._workspace_selector else None
            if not selected_path:
                self._send_workspace_selection_result(request_id, "cancelled")
                return
            config = self.store.load()
            workspace = self.store.add_workspace(config, selected_path)
            self.config = config
            self._send_workspace_selection_result(
                request_id,
                "selected",
                workspace_id=workspace.workspace_id,
            )
        except Exception:
            self._send_workspace_selection_result(request_id, "failed", error="selection_failed")
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
            if not updating and self.active is not None:
                raise RuntimeError("server dispatched a second task to a busy Worker")
            if not updating:
                token = CancellationToken()
                approval = RemoteApprovalStrategy(self._send, str(task["task_id"]), token)
                active = ActiveRun(task_id=str(task["task_id"]), token=token, approval=approval)
                self.active = active

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
                    if self.active is active:
                        self.active = None

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


def _clip_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")

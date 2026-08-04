"""Pairing HTTP client and outbound Worker WebSocket loop."""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from .config import ConfigStore, WorkerConfig
from .runtime import ActiveRun, CancellationToken, RemoteApprovalStrategy, run_task


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
    def __init__(self, store: ConfigStore, config: WorkerConfig):
        self.store = store
        self.config = config
        self.active: ActiveRun | None = None
        self._socket = None
        self._send_lock = threading.Lock()

    def run_forever(self) -> None:
        if not self.config.device_id or not self.config.device_token:
            raise RuntimeError("Worker is not paired; run `threadforge-worker pair` first")
        delay = 1.0
        while True:
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
            print(f"Worker disconnected: {reason}; retrying in {delay:g}s")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)

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
                    "version": "0.1.0",
                    "platform": platform.system().lower(),
                    "model": os.environ.get("PICO_OPENAI_MODEL", "gpt-5.4"),
                    "model_configured": bool(os.environ.get("PICO_OPENAI_API_KEY", "").strip()),
                    "workspaces": [workspace.public_dict() for workspace in self.config.workspaces],
                }
            )
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
        elif message_type == "protocol.error":
            raise RuntimeError(f"Worker protocol rejected: {message.get('code', 'unknown')}")

    def _start_task(self, task: dict) -> None:
        if self.active is not None and self.active.thread is not None and self.active.thread.is_alive():
            raise RuntimeError("server dispatched a second task to a busy Worker")
        workspace_id = str(task.get("workspace_id", ""))
        workspace = next((item for item in self.config.workspaces if item.workspace_id == workspace_id), None)
        if workspace is None:
            self._send(
                {
                    "type": "terminal",
                    "task_id": task.get("task_id", ""),
                    "status": "failed",
                    "stop_reason": "workspace_not_found",
                    "final_answer": "",
                }
            )
            return
        token = CancellationToken()
        approval = RemoteApprovalStrategy(self._send, str(task["task_id"]), token)
        active = ActiveRun(task_id=str(task["task_id"]), token=token, approval=approval)
        self.active = active

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
                    }
                )
            finally:
                if self.active is active:
                    self.active = None

        active.thread = threading.Thread(target=target, name=f"worker-{active.task_id}", daemon=True)
        active.thread.start()

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

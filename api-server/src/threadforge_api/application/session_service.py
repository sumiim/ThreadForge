"""Session application service."""

from __future__ import annotations

import uuid

from pico.features.memory import default_memory_state
from pico.security import redact_artifact
from pico.session_store import (
    SessionCorruptedError as LegacySessionCorruptedError,
)
from pico.session_store import (
    SessionNotFoundError as LegacySessionNotFoundError,
)
from pico.session_store import (
    SessionStoreUnavailableError as LegacySessionStoreUnavailableError,
)

from ..domain.entities import utc_now
from ..domain.errors import (
    PersistenceUnavailableError,
    SessionCorruptedError,
    SessionNotFoundError,
    WorkerOfflineError,
)
from ..domain.identity import canonical_owner_id
from ..infrastructure.json_repositories import JsonTaskRepository
from ..infrastructure.workspace_catalog import WorkspaceCatalog

DEFAULT_SESSION_TITLE_PREFIX = "Session"
MESSAGE_CONTENT_MAX = 4000


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class SessionService:
    def __init__(
        self,
        session_store,
        workspace_catalog: WorkspaceCatalog,
        task_repo: JsonTaskRepository | None = None,
        device_store=None,
        worker_hub=None,
    ):
        self._session_store = session_store
        self._workspace_catalog = workspace_catalog
        self._task_repo = task_repo
        self._device_store = device_store
        self._worker_hub = worker_hub

    def create_session(self, workspace_id: str, title: str | None, owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        local_workspace = (
            self._device_store.find_workspace(owner_id, workspace_id)
            if self._device_store is not None
            else None
        )
        if local_workspace is not None:
            device, workspace = local_workspace
            if self._worker_hub is None or not self._worker_hub.is_online(device.device_id):
                raise WorkerOfflineError("the selected local Worker is offline")
            workspace_root = f"worker://{device.device_id}/{workspace.workspace_id}"
            execution_environment = "local_worker"
            device_id = device.device_id
        else:
            entry = self._workspace_catalog.recheck(workspace_id)
            workspace_root = str(entry.canonical_path)
            execution_environment = "backend_process"
            device_id = ""
        session_id = "ses_" + uuid.uuid4().hex
        session = {
            "id": session_id,
            "created_at": utc_now(),
            "workspace_root": workspace_root,
            "workspace_id": workspace_id,
            "execution_environment": execution_environment,
            "device_id": device_id,
            "owner_id": owner_id,
            "title": (title or "").strip() or f"{DEFAULT_SESSION_TITLE_PREFIX} {session_id[-8:]}",
            "history": [],
            "memory": default_memory_state(),
        }
        try:
            self._session_store.save(session)
        except OSError as exc:
            raise PersistenceUnavailableError("session storage unavailable") from exc
        # Return redacted public view.
        return {
            "session_id": session["id"],
            "workspace_id": session["workspace_id"],
            "title": redact_artifact(session["title"]),
            "created_at": session["created_at"],
            "execution_environment": execution_environment,
            "device_id": device_id,
        }

    def _load(self, session_id: str, owner_id: str | None = None) -> dict:
        try:
            session = self._session_store.load(session_id)
        except LegacySessionNotFoundError:
            raise SessionNotFoundError(session_id) from None
        except LegacySessionCorruptedError:
            raise SessionCorruptedError(session_id) from None
        except LegacySessionStoreUnavailableError:
            raise PersistenceUnavailableError("session storage unavailable") from None
        if owner_id is not None and session.get("owner_id") != canonical_owner_id(owner_id):
            raise SessionNotFoundError(session_id)
        return session

    def list_sessions(self, limit: int, offset: int, owner_id: str) -> tuple[list[dict], int]:
        owner_id = canonical_owner_id(owner_id)
        try:
            ids = self._session_store.list_ids()
        except LegacySessionStoreUnavailableError:
            raise PersistenceUnavailableError("session storage unavailable") from None
        owned = []
        for session_id in ids:
            try:
                session = self._load(session_id)
            except SessionNotFoundError:
                continue
            if session.get("owner_id") == owner_id:
                owned.append(session)
        total = len(owned)
        return [self._summary(session) for session in owned[offset : offset + limit]], total

    def get_session(self, session_id: str, message_limit: int, owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        session = self._load(session_id, owner_id)
        history = session.get("history", [])
        recent = history[-message_limit:] if message_limit else []
        messages = []
        for item in recent:
            messages.append(
                {
                    "role": item.get("role", ""),
                    "name": item.get("name", ""),
                    "content": _clip(redact_artifact(item.get("content", "")), MESSAGE_CONTENT_MAX),
                    "created_at": item.get("created_at", ""),
                }
            )
        task_items = []
        task_total = 0
        if self._task_repo is not None:
            tasks, task_total = self._task_repo.list_for_session(session_id, owner_id)
            task_items = [
                {
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "status": task.status.value,
                    "input": _clip(redact_artifact(task.input), MESSAGE_CONTENT_MAX),
                    "final_answer": _clip(redact_artifact(task.final_answer), MESSAGE_CONTENT_MAX),
                    "stop_reason": task.stop_reason,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                }
                for task in tasks
            ]
        return {
            "session_id": session.get("id"),
            "workspace_id": session.get("workspace_id"),
            "title": redact_artifact(session.get("title", "")),
            "created_at": session.get("created_at"),
            "execution_environment": session.get("execution_environment", "backend_process"),
            "device_id": session.get("device_id", ""),
            "message_total": len(history),
            "has_more": message_limit is not None and len(history) > message_limit,
            "messages": messages,
            "task_total": task_total,
            "tasks": task_items,
        }

    def load_raw(self, session_id: str, owner_id: str) -> dict:
        return self._load(session_id, owner_id)

    @staticmethod
    def _summary(session: dict) -> dict:
        return {
            "session_id": session.get("id"),
            "workspace_id": session.get("workspace_id"),
            "title": redact_artifact(session.get("title", "")),
            "created_at": session.get("created_at"),
            "message_total": len(session.get("history", [])),
            "execution_environment": session.get("execution_environment", "backend_process"),
            "device_id": session.get("device_id", ""),
        }

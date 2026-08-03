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
)
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
    ):
        self._session_store = session_store
        self._workspace_catalog = workspace_catalog
        self._task_repo = task_repo

    def create_session(self, workspace_id: str, title: str | None) -> dict:
        entry = self._workspace_catalog.recheck(workspace_id)
        session_id = "ses_" + uuid.uuid4().hex
        session = {
            "id": session_id,
            "created_at": utc_now(),
            "workspace_root": str(entry.canonical_path),
            "workspace_id": workspace_id,
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
        }

    def _load(self, session_id: str) -> dict:
        try:
            return self._session_store.load(session_id)
        except LegacySessionNotFoundError:
            raise SessionNotFoundError(session_id) from None
        except LegacySessionCorruptedError:
            raise SessionCorruptedError(session_id) from None
        except LegacySessionStoreUnavailableError:
            raise PersistenceUnavailableError("session storage unavailable") from None

    def list_sessions(self, limit: int, offset: int) -> tuple[list[dict], int]:
        try:
            ids = self._session_store.list_ids()
        except LegacySessionStoreUnavailableError:
            raise PersistenceUnavailableError("session storage unavailable") from None
        total = len(ids)
        sessions = []
        for session_id in ids[offset : offset + limit]:
            try:
                sessions.append(self._summary(self._load(session_id)))
            except SessionNotFoundError:
                continue
        return sessions, total

    def get_session(self, session_id: str, message_limit: int) -> dict:
        session = self._load(session_id)
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
            tasks, task_total = self._task_repo.list_for_session(session_id)
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
            "message_total": len(history),
            "has_more": message_limit is not None and len(history) > message_limit,
            "messages": messages,
            "task_total": task_total,
            "tasks": task_items,
        }

    def load_raw(self, session_id: str) -> dict:
        return self._load(session_id)

    @staticmethod
    def _summary(session: dict) -> dict:
        return {
            "session_id": session.get("id"),
            "workspace_id": session.get("workspace_id"),
            "title": redact_artifact(session.get("title", "")),
            "created_at": session.get("created_at"),
            "message_total": len(session.get("history", [])),
        }

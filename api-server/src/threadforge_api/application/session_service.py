"""Session application service."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

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
    ActiveTaskExistsError,
    PersistenceUnavailableError,
    RenameConflictError,
    SessionCorruptedError,
    SessionNotFoundError,
    WorkerOfflineError,
)
from ..domain.identity import canonical_owner_id
from ..infrastructure.json_repositories import JsonTaskRepository
from ..infrastructure.workspace_catalog import WorkspaceCatalog, WorkspaceNotFoundError

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
        approval_repo=None,
        runs_root: Path | None = None,
        device_store=None,
        worker_hub=None,
        allow_backend_workspaces: bool = True,
    ):
        self._session_store = session_store
        self._workspace_catalog = workspace_catalog
        self._task_repo = task_repo
        self._approval_repo = approval_repo
        self._runs_root = Path(runs_root).resolve() if runs_root is not None else None
        self._device_store = device_store
        self._worker_hub = worker_hub
        self._allow_backend_workspaces = allow_backend_workspaces

    def create_session(
        self,
        workspace_id: str,
        title: str | None,
        owner_id: str,
        device_id: str | None = None,
    ) -> dict:
        owner_id = canonical_owner_id(owner_id)
        local_workspace = (
            self._device_store.find_workspace(owner_id, workspace_id, device_id=device_id)
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
            if device_id:
                raise WorkspaceNotFoundError(workspace_id)
            if not self._allow_backend_workspaces:
                raise WorkspaceNotFoundError(workspace_id)
            entry = self._workspace_catalog.recheck(workspace_id)
            workspace_root = str(entry.canonical_path)
            execution_environment = "backend_process"
            device_id = ""
        session_id = "ses_" + uuid.uuid4().hex
        created_at = utc_now()
        display_name = (title or "").strip() or f"{DEFAULT_SESSION_TITLE_PREFIX} {session_id[-8:]}"
        session = {
            "id": session_id,
            "created_at": created_at,
            "workspace_root": workspace_root,
            "workspace_id": workspace_id,
            "execution_environment": execution_environment,
            "device_id": device_id,
            "owner_id": owner_id,
            "title": display_name,
            "display_name_source": "user" if (title or "").strip() else "auto",
            "display_name_updated_at": created_at,
            "first_request_at": "",
            "history": [],
            "memory": default_memory_state(),
        }
        if execution_environment == "local_worker":
            session["local_message_total"] = 0
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
            "display_name_source": session["display_name_source"],
            "display_name_updated_at": session["display_name_updated_at"],
            "has_started": False,
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
            if (
                session.get("owner_id") == owner_id
                and (
                    self._allow_backend_workspaces
                    or session.get("execution_environment") == "local_worker"
                )
            ):
                owned.append(session)
        total = len(owned)
        return [self._summary(session) for session in owned[offset : offset + limit]], total

    async def get_session(self, session_id: str, message_limit: int, owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        session = self._load(session_id, owner_id)
        if (
            not self._allow_backend_workspaces
            and session.get("execution_environment") != "local_worker"
        ):
            raise SessionNotFoundError(session_id)
        history = session.get("history", [])
        task_items = []
        task_total = 0
        if self._task_repo is not None:
            tasks, task_total = self._task_repo.list_for_session(session_id, owner_id)
            task_items = [
                {
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "status": task.status.value,
                    "input": (
                        ""
                        if session.get("execution_environment") == "local_worker"
                        else _clip(redact_artifact(task.input), MESSAGE_CONTENT_MAX)
                    ),
                    "final_answer": (
                        None
                        if session.get("execution_environment") == "local_worker"
                        else _clip(redact_artifact(task.final_answer), MESSAGE_CONTENT_MAX)
                    ),
                    "stop_reason": task.stop_reason,
                    "error_stage": task.error_stage,
                    "error_code": task.error_code,
                    "error_retryable": task.error_retryable,
                    "error_attempts": task.error_attempts,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "model_id": task.model_id,
                    "reasoning_effort": task.reasoning_effort,
                    "run_index": list(task.run_index),
                }
                for task in tasks
            ]
        if session.get("execution_environment") == "local_worker":
            message_total = int(session.get("local_message_total", 0) or 0)
            messages = []
            if message_total > 0 or task_total > 0:
                if self._worker_hub is None:
                    raise WorkerOfflineError("local Worker history is unavailable")
                result = await self._worker_hub.request_session_history(
                    device_id=str(session.get("device_id", "")),
                    session_id=session_id,
                    message_limit=message_limit,
                    owner_id=owner_id,
                )
                messages = result["messages"]
                message_total = result["message_total"]
        else:
            recent = history[-message_limit:] if message_limit else []
            messages = [
                {
                    "role": item.get("role", ""),
                    "name": item.get("name", ""),
                    "content": _clip(
                        redact_artifact(item.get("content", "")), MESSAGE_CONTENT_MAX
                    ),
                    "created_at": item.get("created_at", ""),
                }
                for item in recent
            ]
            message_total = len(history)
        return {
            "session_id": session.get("id"),
            "workspace_id": session.get("workspace_id"),
            "title": redact_artifact(session.get("title", "")),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at", session.get("created_at", "")),
            "display_name_source": session.get("display_name_source", "auto"),
            "display_name_updated_at": session.get(
                "display_name_updated_at", session.get("created_at", "")
            ),
            "has_started": self._has_started(session),
            "execution_environment": session.get("execution_environment", "backend_process"),
            "device_id": session.get("device_id", ""),
            "message_total": message_total,
            "has_more": message_limit is not None and message_total > message_limit,
            "messages": messages,
            "task_total": task_total,
            "tasks": task_items,
        }

    def load_raw(self, session_id: str, owner_id: str) -> dict:
        return self._load(session_id, owner_id)

    def initialize_from_first_request(self, session: dict, request_text: str) -> dict:
        if self._has_started(session):
            return session
        timestamp = utc_now()
        if session.get("display_name_source", "auto") != "user":
            session["title"] = self._automatic_title(request_text)
            session["display_name_source"] = "auto"
            session["display_name_updated_at"] = timestamp
        session["first_request_at"] = timestamp
        session["updated_at"] = timestamp
        try:
            self._session_store.save(session)
        except OSError as exc:
            raise PersistenceUnavailableError("session storage unavailable") from exc
        return session

    def rename_session(
        self,
        session_id: str,
        display_name: str,
        owner_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> dict:
        display_name = str(display_name).strip()
        if not display_name or len(display_name) > 200:
            raise ValueError("invalid session display name")
        session = self._load(session_id, owner_id)
        display_name_updated_at = str(
            session.get("display_name_updated_at", session.get("created_at", ""))
        )
        if expected_updated_at and expected_updated_at != display_name_updated_at:
            raise RenameConflictError("session display name changed on another client")
        session["title"] = display_name
        session["display_name_source"] = "user"
        session["display_name_updated_at"] = utc_now()
        session["updated_at"] = session["display_name_updated_at"]
        self._session_store.save(session)
        return self._summary(session)

    def session_ids_for_workspace(
        self,
        workspace_id: str,
        device_id: str,
        owner_id: str,
    ) -> list[str]:
        owner_id = canonical_owner_id(owner_id)
        session_ids: list[str] = []
        for session_id in self._session_store.list_ids():
            try:
                session = self._load(session_id)
            except SessionNotFoundError:
                continue
            if (
                session.get("owner_id") == owner_id
                and session.get("device_id") == device_id
                and session.get("workspace_id") == workspace_id
                and session.get("execution_environment") == "local_worker"
            ):
                session_ids.append(session_id)
        return session_ids

    def delete_sessions_for_device(self, device_id: str, owner_id: str) -> dict:
        """解绑设备时级联删除该设备名下全部会话，清理孤儿会话索引。"""
        owner_id = canonical_owner_id(owner_id)
        session_ids: list[str] = []
        for session_id in self._session_store.list_ids():
            try:
                session = self._load(session_id)
            except SessionNotFoundError:
                continue
            if (
                session.get("owner_id") == owner_id
                and session.get("device_id") == device_id
                and session.get("execution_environment") == "local_worker"
            ):
                session_ids.append(session_id)
        if not session_ids:
            return {"status": "deleted", "deleted_session_ids": []}
        return self.delete_sessions(session_ids, owner_id)

    def ensure_sessions_deletable(self, session_ids: list[str], owner_id: str) -> None:
        if self._task_repo is None or not session_ids:
            return
        tasks = self._task_repo.list_for_sessions(set(session_ids), owner_id)
        active = next((task for task in tasks if not task.status.terminal), None)
        if active is not None:
            raise ActiveTaskExistsError(active.task_id)

    def run_ids_for_sessions(self, session_ids: list[str], owner_id: str) -> list[str]:
        if self._task_repo is None or not session_ids:
            return []
        self.ensure_sessions_deletable(session_ids, owner_id)
        return [
            task.run_id
            for task in self._task_repo.list_for_sessions(set(session_ids), owner_id)
            if task.run_id
        ]

    def delete_session(self, session_id: str, owner_id: str) -> dict:
        return self.delete_sessions([session_id], owner_id)

    def delete_sessions(self, session_ids: list[str], owner_id: str) -> dict:
        owner_id = canonical_owner_id(owner_id)
        unique_ids = list(dict.fromkeys(str(item) for item in session_ids))
        sessions = [self._load(session_id, owner_id) for session_id in unique_ids]
        self.ensure_sessions_deletable(unique_ids, owner_id)

        tasks = (
            self._task_repo.list_for_sessions(set(unique_ids), owner_id)
            if self._task_repo is not None
            else []
        )
        task_ids = {task.task_id for task in tasks}
        if self._approval_repo is not None:
            self._approval_repo.delete_for_tasks(task_ids, owner_id)
        self._delete_run_artifacts({task.run_id for task in tasks if task.run_id})
        if self._task_repo is not None:
            self._task_repo.delete_many(task_ids, owner_id)
        for session in sessions:
            try:
                self._session_store.delete(session["id"])
            except LegacySessionStoreUnavailableError:
                raise PersistenceUnavailableError("session storage unavailable") from None
        return {"status": "deleted", "deleted_session_ids": unique_ids}

    def _delete_run_artifacts(self, run_ids: set[str]) -> None:
        if self._runs_root is None:
            return
        for run_id in run_ids:
            run_dir = (self._runs_root / run_id).resolve()
            try:
                run_dir.relative_to(self._runs_root)
            except ValueError:
                raise PersistenceUnavailableError("run artifact path is invalid") from None
            try:
                shutil.rmtree(run_dir, ignore_errors=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PersistenceUnavailableError("run artifact storage unavailable") from exc

    @staticmethod
    def _summary(session: dict) -> dict:
        is_local = session.get("execution_environment") == "local_worker"
        return {
            "session_id": session.get("id"),
            "workspace_id": session.get("workspace_id"),
            "title": redact_artifact(session.get("title", "")),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at", session.get("created_at", "")),
            "display_name_source": session.get("display_name_source", "auto"),
            "display_name_updated_at": session.get(
                "display_name_updated_at", session.get("created_at", "")
            ),
            "has_started": SessionService._has_started(session),
            "message_total": (
                int(session.get("local_message_total", 0) or 0)
                if is_local
                else len(session.get("history", []))
            ),
            "execution_environment": session.get("execution_environment", "backend_process"),
            "device_id": session.get("device_id", ""),
        }

    @staticmethod
    def _automatic_title(request_text: str) -> str:
        normalized = " ".join(str(request_text).split())
        return normalized[:200] or "新会话"

    @staticmethod
    def _has_started(session: dict) -> bool:
        try:
            local_message_total = int(session.get("local_message_total", 0) or 0)
        except (TypeError, ValueError):
            local_message_total = 0
        return bool(
            str(session.get("first_request_at", "")).strip()
            or session.get("history")
            or local_message_total > 0
        )

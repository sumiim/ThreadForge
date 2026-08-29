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

_HISTORY_TERMINAL_MESSAGES = {
    "approval_denied": "所需工具操作未获批准，运行已停止。",
    "budget_exhausted": "本次运行已达到时间或令牌预算，请缩小任务范围后重试。",
    "convergence_guard_triggered": "模型未能通过审查或持续产生有效进展，本次运行已停止空转。",
    "retry_limit_reached": "模型输出未通过执行协议校验，达到重试上限后停止。",
    "review_retry_limit_reached": "审查阶段未能确认任务已经完成，达到重试上限后停止。",
    "step_limit_reached": "本次运行触发了旧版步骤保护，请缩小任务范围后重试。",
    "user_cancelled": "已停止当前任务。",
}


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _attach_run_detail(messages: list[dict], task_items: list[dict]) -> list[dict]:
    """§7.8.9 决策（2026-08-19）：对话历史回放——把运行审计还原挂到 assistant 消息。

    工具卡（参数/结果）、thinking、过程更新（commentary）、审查对抗
    （verdict/feedback/反驳）只存在于前端内存的流式状态里，刷新即丢。这里从
    持久化的 ``run_index`` 重建这些内容（含按模型轮分组的 turn 编号），挂到
    对应 assistant 消息上，让历史对话也能看到完整的执行过程。
    任务 ↔ 消息按时间顺序从尾部配对（历史可能被 message_limit 裁掉头部）。
    """
    if not messages or not task_items:
        return messages
    assistant_indices = [index for index, item in enumerate(messages) if item.get("role") == "assistant"]
    tasks = sorted(task_items, key=lambda task: str(task.get("created_at", "")))
    pairs = list(zip(reversed(assistant_indices), reversed(tasks)))
    for message_index, task in pairs:
        run = task.get("run_index") or []
        tool_by_id: dict[str, dict] = {}
        tool_calls: list[dict] = []
        thinking_parts: list[str] = []
        review_entries: list[dict] = []
        commentary_parts: list[str] = []
        review_skipped = False
        # §7.8.9 修正（2026-08-19）：按模型轮重建交替块（执行 → turn N → 思考/工具）。
        # 内联实现（不用闭包）：避免 ruff B023（嵌套函数引用循环中修改的绑定）。
        blocks: list[dict] = []
        current_turn = 0
        last_review_turn = 0
        pending_behavior: dict | None = None

        for item in run:
            event_type = str(item.get("type", ""))
            if event_type == "model.started":
                current_turn += 1
                if pending_behavior is not None:
                    blocks.append(pending_behavior)
                    pending_behavior = None
            elif event_type == "assistant.commentary":
                text = str(item.get("text", "")).strip()
                if text:
                    commentary_parts.append(text)
                    if pending_behavior is not None:
                        blocks.append(pending_behavior)
                        pending_behavior = None
                    blocks.append({"kind": "commentary", "text": text, "turn": current_turn or None})
            elif event_type == "assistant.thinking":
                text = str(item.get("text", "")).strip()
                if text:
                    thinking_parts.append(text)
                    if pending_behavior is None:
                        pending_behavior = {"kind": "behavior", "turn": current_turn or None}
                    previous = pending_behavior.get("thinking") or ""
                    pending_behavior["thinking"] = previous + ("\n" if previous else "") + text
            elif event_type == "tool.requested":
                call = {
                    "id": str(item.get("tool_call_id", "")),
                    "tool_name": str(item.get("tool_name", "")),
                    "args": item.get("args_preview"),
                    "status": "completed",
                }
                tool_by_id[call["id"]] = call
                tool_calls.append(call)
                if pending_behavior is None:
                    pending_behavior = {"kind": "behavior", "turn": current_turn or None}
                pending_behavior.setdefault("toolCalls", []).append(call)
            elif event_type in {"tool.completed", "tool.failed"}:
                call = tool_by_id.get(str(item.get("tool_call_id", "")))
                if call is None:
                    continue
                result = item.get("result_preview")
                if isinstance(result, str) and result:
                    call["result"] = result + ("\n\n[预览已截断]" if item.get("result_truncated") else "")
                call["status"] = "error" if event_type == "tool.failed" else "completed"
            elif event_type == "review.started":
                if pending_behavior is not None:
                    blocks.append(pending_behavior)
                    pending_behavior = None
                last_review_turn = current_turn
                review_entries.append({"side": "review", "action": str(item.get("trigger", ""))})
            elif event_type == "review.completed":
                if pending_behavior is not None:
                    blocks.append(pending_behavior)
                    pending_behavior = None
                last_review_turn = current_turn
                review_entries.append(
                    {
                        "side": "review",
                        "verdict": str(item.get("verdict", "")) or None,
                        "feedback": item.get("feedback"),
                        "reason": item.get("reason"),
                        "obstacles": item.get("obstacles") or None,
                    }
                )
            elif event_type == "main_loop_rebuttal":
                if pending_behavior is not None:
                    blocks.append(pending_behavior)
                    pending_behavior = None
                last_review_turn = current_turn
                review_entries.append(
                    {
                        "side": "main_loop",
                        "against_verdict": item.get("against_verdict"),
                        "action": item.get("action"),
                        "feedback": item.get("feedback"),
                    }
                )
            elif event_type == "review.skipped":
                if pending_behavior is not None:
                    blocks.append(pending_behavior)
                    pending_behavior = None
                last_review_turn = current_turn
                review_skipped = True
        if pending_behavior is not None:
            blocks.append(pending_behavior)
        if review_skipped and not review_entries:
            review_entries.append(
                {
                    "side": "review",
                    "verdict": "skipped",
                    "feedback": "只读任务,审查已跳过(重复动作/停滞窗口仍负责收敛)",
                    "reason": str(item.get("reason", "")) if item.get("reason") else "read_only_task",
                }
            )
        if review_entries:
            blocks.append({"kind": "review", "entries": review_entries, "turn": last_review_turn or None})
        message = dict(messages[message_index])
        if str(task.get("status", "")) != "completed":
            stop_reason = str(task.get("stop_reason", ""))
            message["content"] = _HISTORY_TERMINAL_MESSAGES.get(
                stop_reason,
                "运行未能正常完成，请在审计中查看具体原因后重试。",
            )
        if tool_calls:
            message["tool_calls"] = tool_calls
        if thinking_parts:
            message["thinking"] = "\n".join(thinking_parts)
        if commentary_parts:
            message["commentary"] = "\n".join(commentary_parts)
        if review_entries:
            message["review_entries"] = review_entries
        if blocks:
            message["blocks"] = blocks
        messages[message_index] = message
    return messages


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
                    "input": _clip(redact_artifact(task.input), MESSAGE_CONTENT_MAX),
                    "final_answer": _clip(redact_artifact(task.final_answer), MESSAGE_CONTENT_MAX),
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
        messages = _attach_run_detail(messages, task_items)
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

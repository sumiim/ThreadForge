"""Task / Approval JSON control-state repositories (single process, locked)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from ..domain.entities import Approval, Task
from ..domain.errors import (
    AppError,
    ApprovalNotFoundError,
    PersistenceUnavailableError,
    TaskNotFoundError,
)
from .jsonutil import JsonCorruptedError, read_json, secure_directory, write_json_atomic


class RecordNotFoundError(RuntimeError):
    pass


class StaleGenerationError(RuntimeError):
    pass


class RecordCorruptedError(AppError):
    http_status = 500
    code = "record_corrupted"


class RecordUnavailableError(PersistenceUnavailableError):
    pass


class _JsonRepoBase:
    def __init__(self, root: Path):
        self.root = Path(root)
        secure_directory(self.root)
        self._lock = threading.RLock()

    def _write_record(self, record_id: str, payload: dict) -> None:
        try:
            write_json_atomic(self.root / f"{record_id}.json", payload)
        except OSError as exc:
            raise RecordUnavailableError(f"storage unavailable for {record_id}") from exc

    def _read_record(self, record_id: str) -> dict:
        try:
            return read_json(self.root / f"{record_id}.json")
        except FileNotFoundError as exc:
            raise RecordNotFoundError(record_id) from exc
        except JsonCorruptedError as exc:
            raise RecordCorruptedError(f"corrupted JSON for {record_id}") from exc
        except OSError as exc:
            raise RecordUnavailableError(f"storage unavailable for {record_id}") from exc

    def list_stable(self):
        """Paths ordered by mtime DESC then id DESC."""
        items = []
        for path in self.root.glob("*.json"):
            try:
                mtime = str(path.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RecordUnavailableError(f"storage unavailable for {path.stem}") from exc
            items.append((mtime, path.stem))
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [stem for _, stem in items]


class JsonTaskRepository(_JsonRepoBase):
    def create(self, task: Task) -> Task:
        with self._lock:
            if (self.root / f"{task.task_id}.json").exists():
                raise RecordCorruptedError(f"task id collision: {task.task_id}")
            self._write_record(task.task_id, task.to_dict())
        return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            try:
                return _task_from_dict(self._read_record(task_id), task_id)
            except RecordNotFoundError:
                raise TaskNotFoundError(task_id) from None
            except RecordCorruptedError:
                raise

    def list(self, limit: int, offset: int) -> tuple[list[Task], int]:
        with self._lock:
            ids = self.list_stable()
            total = len(ids)
            tasks = []
            for record_id in ids[offset : offset + limit]:
                try:
                    tasks.append(_task_from_dict(self._read_record(record_id), record_id))
                except RecordNotFoundError:
                    continue
            return tasks, total

    def list_for_session(self, session_id: str, limit: int = 100) -> tuple[list[Task], int]:
        with self._lock:
            tasks: list[Task] = []
            total = 0
            for record_id in self.list_stable():
                try:
                    task = _task_from_dict(self._read_record(record_id), record_id)
                except RecordNotFoundError:
                    continue
                if task.session_id != session_id:
                    continue
                total += 1
                if len(tasks) < limit:
                    tasks.append(task)
            return tasks, total

    def update(
        self,
        task_id: str,
        fn: Callable[[Task], Task],
        *,
        expected_generation: int | None = None,
    ) -> Task:
        with self._lock:
            current = self._read_record(task_id)
            task = _task_from_dict(current, task_id)
            if expected_generation is not None and task.generation != expected_generation:
                raise StaleGenerationError()
            updated = fn(task)
            from ..domain.entities import utc_now

            updated.updated_at = utc_now()
            self._write_record(task_id, updated.to_dict())
            return updated


class JsonApprovalRepository(_JsonRepoBase):
    def create(self, approval: Approval) -> Approval:
        with self._lock:
            if (self.root / f"{approval.approval_id}.json").exists():
                raise RecordCorruptedError(f"approval id collision: {approval.approval_id}")
            self._write_record(approval.approval_id, approval.to_dict())
        return approval

    def get(self, approval_id: str) -> Approval:
        with self._lock:
            try:
                return _approval_from_dict(self._read_record(approval_id), approval_id)
            except RecordNotFoundError:
                raise ApprovalNotFoundError(approval_id) from None
            except RecordCorruptedError:
                raise

    def update(self, approval_id: str, fn: Callable[[Approval], Approval]) -> Approval:
        with self._lock:
            current = _approval_from_dict(self._read_record(approval_id), approval_id)
            updated = fn(current)
            self._write_record(approval_id, updated.to_dict())
            return updated

    def list_pending_for_task(self, task_id: str) -> list[Approval]:
        with self._lock:
            out = []
            for record_id in self.list_stable():
                try:
                    approval = _approval_from_dict(self._read_record(record_id), record_id)
                except RecordNotFoundError:
                    continue
                if approval.task_id == task_id and approval.status.value == "pending":
                    out.append(approval)
            return out


def _task_from_dict(payload: dict, record_id: str) -> Task:
    try:
        return Task.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordCorruptedError(f"corrupted Task record for {record_id}") from exc


def _approval_from_dict(payload: dict, record_id: str) -> Approval:
    try:
        return Approval.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordCorruptedError(f"corrupted Approval record for {record_id}") from exc

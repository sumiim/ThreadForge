"""SQLite-backed control-plane repositories (primary) with JSON mirror.

These replace the JSON repositories as the authoritative writer for tasks and
approvals while keeping a write-through JSON mirror under ``json_root`` so the
legacy reconciliation and cross-check paths keep working. On startup the
migrator imports the mirror into SQLite idempotently (upsert by primary key),
so an existing JSON data directory is preserved and re-running the migration
never produces duplicate rows.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from ..domain.entities import Approval, Task, utc_now
from ..domain.errors import (
    ActiveTaskExistsError,
    ApprovalNotFoundError,
    ProviderNotFoundError,
    RunNotFoundError,
    TaskNotFoundError,
)
from ..domain.identity import canonical_owner_id
from ..domain.providers import Provider
from .json_repositories import (
    RecordCorruptedError,
    RecordNotFoundError,
    RecordUnavailableError,
    StaleGenerationError,
)
from .jsonutil import JsonCorruptedError, read_json
from .sqlite_store import SqliteStore


def _json_dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, record_id: str) -> dict:
    if value is None:
        raise RecordNotFoundError(record_id)
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RecordCorruptedError(f"corrupted SQLite payload for {record_id}") from exc
    if not isinstance(data, dict):
        raise RecordCorruptedError(f"corrupted SQLite payload for {record_id}")
    return data


class _JsonMirror:
    """Write-through compatibility mirror of the JSON control-state files."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, record_id: str, payload: dict) -> None:
        # The mirror is a compatibility/cross-check copy, not the durability
        # source of truth (SQLite is). Use a fast atomic replace without fsync
        # so the control-plane write path stays on the same latency budget as
        # the legacy JSON store and does not widen any publish/slot race.
        path = self.root / f"{record_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temp_path.replace(path)
            temp_path = None
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    def remove(self, record_id: str) -> None:
        (self.root / f"{record_id}.json").unlink(missing_ok=True)

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.stem for path in self.root.glob("*.json"))

    def read(self, record_id: str) -> dict:
        try:
            return read_json(self.root / f"{record_id}.json")
        except FileNotFoundError as exc:
            raise RecordNotFoundError(record_id) from exc
        except JsonCorruptedError as exc:
            raise RecordCorruptedError(f"corrupted JSON for {record_id}") from exc


class SqliteTaskRepository:
    def __init__(self, store: SqliteStore, json_root: Path | None = None):
        self._store = store
        self.mirror = _JsonMirror(json_root) if json_root is not None else None

    @property
    def root(self) -> Path:
        return self.mirror.root if self.mirror is not None else self._store.root

    # ---- write ---------------------------------------------------------------

    def create(self, task: Task) -> Task:
        payload = task.to_dict()
        try:
            with self._store.transaction() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO tasks(
                        task_id, session_id, workspace_id, owner_id, run_id, status,
                        execution_environment, device_id, created_at, updated_at,
                        generation, payload
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task.task_id, task.session_id, task.workspace_id, task.owner_id,
                        task.run_id, task.status.value, task.execution_environment,
                        task.device_id, task.created_at, task.updated_at, task.generation,
                        _json_dumps(payload),
                    ),
                )
                if cur.rowcount == 0:
                    raise RecordCorruptedError(f"task id collision: {task.task_id}")
                _upsert_run(conn, task)
        except RecordCorruptedError:
            raise
        except sqlite3.Error as exc:
            raise RecordUnavailableError(f"storage unavailable for {task.task_id}") from exc
        self._mirror_write(task.task_id, payload)
        return task

    def update(self, task_id: str, fn: Callable[[Task], Task], *, expected_generation: int | None = None) -> Task:
        payload = self._read_payload("tasks", "task_id", task_id)
        task = _task_from_payload(payload, task_id)
        if expected_generation is not None and task.generation != expected_generation:
            raise StaleGenerationError()
        updated = fn(task)
        updated.updated_at = utc_now()
        self._write_task(updated)
        return updated

    def _write_task(self, task: Task) -> None:
        payload = task.to_dict()
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, session_id, workspace_id, owner_id, run_id, status,
                    execution_environment, device_id, created_at, updated_at,
                    generation, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    workspace_id=excluded.workspace_id,
                    owner_id=excluded.owner_id,
                    run_id=excluded.run_id,
                    status=excluded.status,
                    execution_environment=excluded.execution_environment,
                    device_id=excluded.device_id,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    generation=excluded.generation,
                    payload=excluded.payload
                """,
                (
                    task.task_id, task.session_id, task.workspace_id, task.owner_id,
                    task.run_id, task.status.value, task.execution_environment,
                    task.device_id, task.created_at, task.updated_at, task.generation,
                    _json_dumps(payload),
                ),
            )
            _upsert_run(conn, task)
        self._mirror_write(task.task_id, payload)

    def delete_many(self, task_ids: set[str], owner_id: str) -> list[str]:
        owner_id = canonical_owner_id(owner_id)
        if not task_ids:
            return []
        removed: list[str] = []
        for task_id in task_ids:
            try:
                task = self.get_for_owner(task_id, owner_id)
            except TaskNotFoundError:
                continue
            if not task.status.terminal:
                raise ActiveTaskExistsError(task.task_id)
            removed.append(task.task_id)
        with self._store.transaction() as conn:
            for task_id in removed:
                conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        if self.mirror is not None:
            for task_id in removed:
                self.mirror.remove(task_id)
        return removed

    def assign_legacy_owner(self, owner_id: str) -> int:
        owner_id = canonical_owner_id(owner_id)
        migrated = 0
        for record_id in self.list_stable():
            try:
                task = self.get(record_id)
            except (TaskNotFoundError, RecordCorruptedError):
                continue
            if task.owner_id:
                continue
            task.owner_id = owner_id
            task.schema_version = max(task.schema_version, 2)
            self._write_task(task)
            migrated += 1
        return migrated

    # ---- read ----------------------------------------------------------------

    def _read_payload(self, table: str, key: str, record_id: str) -> dict:
        row = self._store.query_one(
            f"SELECT payload FROM {table} WHERE {key}=?", (record_id,)
        )
        if row is None:
            raise RecordNotFoundError(record_id)
        return _json_loads(row["payload"], record_id)

    def get(self, task_id: str) -> Task:
        try:
            return _task_from_payload(self._read_payload("tasks", "task_id", task_id), task_id)
        except RecordNotFoundError as exc:
            raise TaskNotFoundError(task_id) from exc

    def get_for_owner(self, task_id: str, owner_id: str) -> Task:
        task = self.get(task_id)
        if task.owner_id != canonical_owner_id(owner_id):
            raise TaskNotFoundError(task_id)
        return task

    def get_by_run_for_owner(self, run_id: str, owner_id: str) -> Task:
        owner_id = canonical_owner_id(owner_id)
        row = self._store.query_one(
            "SELECT payload FROM tasks WHERE run_id=? AND owner_id=?",
            (run_id, owner_id),
        )
        if row is None:
            raise RunNotFoundError(run_id)
        return _task_from_payload(_json_loads(row["payload"], run_id), run_id)

    def list(self, limit: int, offset: int) -> tuple[list[Task], int]:
        ids = self.list_stable()
        total = len(ids)
        tasks = []
        for record_id in ids[offset : offset + limit]:
            try:
                tasks.append(self.get(record_id))
            except TaskNotFoundError:
                continue
        return tasks, total

    def list_for_session(self, session_id: str, owner_id: str, limit: int = 100) -> tuple[list[Task], int]:
        owner_id = canonical_owner_id(owner_id)
        rows = self._store.query(
            "SELECT payload FROM tasks WHERE session_id=? AND owner_id=? "
            "ORDER BY updated_at DESC, task_id DESC",
            (session_id, owner_id),
        )
        total = len(rows)
        tasks = []
        for row in rows[:limit]:
            tasks.append(_task_from_payload(_json_loads(row["payload"], session_id), session_id))
        return tasks, total

    def list_for_sessions(self, session_ids: set[str], owner_id: str) -> list[Task]:
        owner_id = canonical_owner_id(owner_id)
        if not session_ids:
            return []
        tasks: list[Task] = []
        for session_id in session_ids:
            rows = self._store.query(
                "SELECT payload FROM tasks WHERE session_id=? AND owner_id=?",
                (session_id, owner_id),
            )
            for row in rows:
                tasks.append(_task_from_payload(_json_loads(row["payload"], session_id), session_id))
        return tasks

    def list_stable(self) -> list[str]:
        rows = self._store.query(
            "SELECT task_id FROM tasks ORDER BY updated_at DESC, task_id DESC"
        )
        return [row["task_id"] for row in rows]

    # ---- mirror helpers ------------------------------------------------------

    def _mirror_write(self, record_id: str, payload: dict) -> None:
        if self.mirror is not None:
            self.mirror.write(record_id, payload)

    def cross_check(self) -> list[dict]:
        """Compare the JSON mirror against SQLite and return drift records."""
        if self.mirror is None:
            return []
        drift = []
        for record_id in self.mirror.ids():
            try:
                json_payload = self.mirror.read(record_id)
            except RecordCorruptedError:
                drift.append({"record_id": record_id, "reason": "json_corrupted"})
                continue
            try:
                sqlite_payload = self._read_payload("tasks", "task_id", record_id)
            except RecordNotFoundError:
                drift.append({"record_id": record_id, "reason": "missing_in_sqlite"})
                continue
            if _json_dumps(json_payload) != _json_dumps(sqlite_payload):
                drift.append({"record_id": record_id, "reason": "diverged"})
        return drift


class SqliteApprovalRepository:
    def __init__(self, store: SqliteStore, json_root: Path | None = None):
        self._store = store
        self.mirror = _JsonMirror(json_root) if json_root is not None else None

    @property
    def root(self) -> Path:
        return self.mirror.root if self.mirror is not None else self._store.root

    def create(self, approval: Approval) -> Approval:
        payload = approval.to_dict()
        with self._store.transaction() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO approvals(
                    approval_id, task_id, run_id, owner_id, tool_call_id, tool_name,
                    status, created_at, decided_at, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    approval.approval_id, approval.task_id, approval.run_id, approval.owner_id,
                    approval.tool_call_id, approval.tool_name, approval.status.value,
                    approval.created_at, approval.decided_at or "", _json_dumps(payload),
                ),
            )
            if cur.rowcount == 0:
                raise RecordCorruptedError(f"approval id collision: {approval.approval_id}")
        self._mirror_write(approval.approval_id, payload)
        return approval

    def update(self, approval_id: str, fn: Callable[[Approval], Approval]) -> Approval:
        payload = self._read_payload(approval_id)
        approval = _approval_from_payload(payload, approval_id)
        updated = fn(approval)
        self._write_approval(updated)
        return updated

    def _write_approval(self, approval: Approval) -> None:
        payload = approval.to_dict()
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    approval_id, task_id, run_id, owner_id, tool_call_id, tool_name,
                    status, created_at, decided_at, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    run_id=excluded.run_id,
                    owner_id=excluded.owner_id,
                    tool_call_id=excluded.tool_call_id,
                    tool_name=excluded.tool_name,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    decided_at=excluded.decided_at,
                    payload=excluded.payload
                """,
                (
                    approval.approval_id, approval.task_id, approval.run_id, approval.owner_id,
                    approval.tool_call_id, approval.tool_name, approval.status.value,
                    approval.created_at, approval.decided_at or "", _json_dumps(payload),
                ),
            )
        self._mirror_write(approval.approval_id, payload)

    def _read_payload(self, approval_id: str) -> dict:
        row = self._store.query_one(
            "SELECT payload FROM approvals WHERE approval_id=?", (approval_id,)
        )
        if row is None:
            raise RecordNotFoundError(approval_id)
        return _json_loads(row["payload"], approval_id)

    def get(self, approval_id: str) -> Approval:
        try:
            return _approval_from_payload(self._read_payload(approval_id), approval_id)
        except RecordNotFoundError as exc:
            raise ApprovalNotFoundError(approval_id) from exc

    def get_for_owner(self, approval_id: str, owner_id: str) -> Approval:
        approval = self.get(approval_id)
        if approval.owner_id != canonical_owner_id(owner_id):
            raise ApprovalNotFoundError(approval_id)
        return approval

    def list_pending_for_task(self, task_id: str) -> list[Approval]:
        rows = self._store.query(
            "SELECT payload FROM approvals WHERE task_id=? AND status='pending'",
            (task_id,),
        )
        return [_approval_from_payload(_json_loads(row["payload"], task_id), task_id) for row in rows]

    def delete_for_tasks(self, task_ids: set[str], owner_id: str) -> list[str]:
        owner_id = canonical_owner_id(owner_id)
        if not task_ids:
            return []
        removed: list[str] = []
        for task_id in task_ids:
            rows = self._store.query(
                "SELECT approval_id, payload FROM approvals WHERE task_id=? AND owner_id=?",
                (task_id, owner_id),
            )
            for row in rows:
                removed.append(row["approval_id"])
        with self._store.transaction() as conn:
            for approval_id in removed:
                conn.execute("DELETE FROM approvals WHERE approval_id=?", (approval_id,))
        if self.mirror is not None:
            for approval_id in removed:
                self.mirror.remove(approval_id)
        return removed

    def assign_legacy_owner(self, owner_id: str) -> int:
        owner_id = canonical_owner_id(owner_id)
        migrated = 0
        for approval_id in self.list_stable():
            try:
                approval = self.get(approval_id)
            except (ApprovalNotFoundError, RecordCorruptedError):
                continue
            if approval.owner_id:
                continue
            approval.owner_id = owner_id
            approval.schema_version = max(approval.schema_version, 2)
            self._write_approval(approval)
            migrated += 1
        return migrated

    def list_stable(self) -> list[str]:
        rows = self._store.query(
            "SELECT approval_id FROM approvals ORDER BY created_at DESC, approval_id DESC"
        )
        return [row["approval_id"] for row in rows]

    def _mirror_write(self, record_id: str, payload: dict) -> None:
        if self.mirror is not None:
            self.mirror.write(record_id, payload)

    def cross_check(self) -> list[dict]:
        if self.mirror is None:
            return []
        drift = []
        for record_id in self.mirror.ids():
            try:
                json_payload = self.mirror.read(record_id)
            except RecordCorruptedError:
                drift.append({"record_id": record_id, "reason": "json_corrupted"})
                continue
            try:
                sqlite_payload = self._read_payload(record_id)
            except RecordNotFoundError:
                drift.append({"record_id": record_id, "reason": "missing_in_sqlite"})
                continue
            if _json_dumps(json_payload) != _json_dumps(sqlite_payload):
                drift.append({"record_id": record_id, "reason": "diverged"})
        return drift


class SqliteRunRepository:
    """Control-plane index of Runs (artifacts stay in the legacy RunStore)."""

    def __init__(self, store: SqliteStore):
        self._store = store

    def upsert(self, run: dict) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, task_id, session_id, workspace_id, owner_id,
                    status, created_at, updated_at, payload)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    run["run_id"], run.get("task_id", ""), run.get("session_id", ""),
                    run.get("workspace_id", ""), run.get("owner_id", ""), run.get("status", ""),
                    run.get("created_at", ""), run.get("updated_at", ""), _json_dumps(run),
                ),
            )

    def list_for_workspace(self, workspace_id: str) -> list[dict]:
        rows = self._store.query(
            "SELECT payload FROM runs WHERE workspace_id=? ORDER BY updated_at DESC",
            (workspace_id,),
        )
        return [json.loads(row["payload"]) for row in rows]


class SqliteLeaseRepository:
    """Workspace write leases used by the workspace-isolation layer."""

    def __init__(self, store: SqliteStore):
        self._store = store

    def acquire(
        self,
        *,
        workspace_id: str,
        holder_task_id: str,
        holder_run_id: str,
        owner_id: str,
        mode: str,
        expires_at: str,
        now: str,
    ) -> tuple[str, bool]:
        """Atomically acquire the workspace lease.

        Returns ``(lease_token, acquired)``. ``acquired`` is False when another
        live lease already owns the workspace (caller must wait or reject).
        """
        lease_token = "lease_" + holder_task_id
        acquired = False
        with self._store.transaction() as conn:
            # Expire any stale lease held by a different task.
            conn.execute(
                "DELETE FROM workspace_leases WHERE workspace_id=? AND expires_at<=? AND holder_task_id!=?",
                (workspace_id, now, holder_task_id),
            )
            existing = conn.execute(
                "SELECT holder_task_id FROM workspace_leases WHERE workspace_id=? AND expires_at>?",
                (workspace_id, now),
            ).fetchone()
            if existing is not None and existing["holder_task_id"] != holder_task_id:
                acquired = False
            else:
                conn.execute(
                    """
                    INSERT INTO workspace_leases(
                        workspace_id, holder_task_id, holder_run_id, owner_id,
                        lease_token, mode, created_at, expires_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(workspace_id) DO UPDATE SET
                        holder_task_id=excluded.holder_task_id,
                        holder_run_id=excluded.holder_run_id,
                        owner_id=excluded.owner_id,
                        lease_token=excluded.lease_token,
                        mode=excluded.mode,
                        created_at=excluded.created_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        workspace_id, holder_task_id, holder_run_id, owner_id,
                        lease_token, mode, now, expires_at,
                    ),
                )
                acquired = True
        return lease_token, acquired

    def renew(self, workspace_id: str, lease_token: str, expires_at: str) -> bool:
        with self._store.transaction() as conn:
            cur = conn.execute(
                "UPDATE workspace_leases SET expires_at=? WHERE workspace_id=? AND lease_token=?",
                (expires_at, workspace_id, lease_token),
            )
            return cur.rowcount > 0

    def release(self, workspace_id: str, lease_token: str) -> bool:
        with self._store.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM workspace_leases WHERE workspace_id=? AND lease_token=?",
                (workspace_id, lease_token),
            )
            return cur.rowcount > 0

    def holder(self, workspace_id: str, now: str) -> dict | None:
        row = self._store.query_one(
            "SELECT * FROM workspace_leases WHERE workspace_id=? AND expires_at>?",
            (workspace_id, now),
        )
        return dict(row) if row is not None else None

    def expired(self, now: str) -> list[dict]:
        rows = self._store.query(
            "SELECT * FROM workspace_leases WHERE expires_at<=?", (now,)
        )
        return [dict(row) for row in rows]


class ControlPlaneMigrator:
    """Idempotent JSON -> SQLite import plus cross-check."""

    def __init__(self, task_repo: SqliteTaskRepository, approval_repo: SqliteApprovalRepository):
        self._task_repo = task_repo
        self._approval_repo = approval_repo

    def import_json(self, owner_id: str | None = None) -> dict:
        """Upsert every JSON mirror record into SQLite.

        Returns ``{"tasks": n, "approvals": m}``. Re-running is a no-op for
        already-imported records (no duplicates). Corrupted JSON raises and the
        whole import rolls back. Legacy records missing ``owner_id`` are claimed
        by ``owner_id`` (when supplied) before parsing, preserving the
        pre-V1.5 ownership-compat behavior.
        """
        imported_tasks = 0
        imported_approvals = 0
        tasks = self._read_all_json(self._task_repo, _task_from_payload, owner_id)
        approvals = self._read_all_json(self._approval_repo, _approval_from_payload, owner_id)
        for task in tasks:
            self._task_repo._write_task(task)
            imported_tasks += 1
        for approval in approvals:
            self._approval_repo._write_approval(approval)
            imported_approvals += 1
        return {"tasks": imported_tasks, "approvals": imported_approvals}

    @staticmethod
    def _read_all_json(repo, from_payload, owner_id: str | None):
        if repo.mirror is None:
            return []
        records = []
        for record_id in repo.mirror.ids():
            payload = repo.mirror.read(record_id)  # raises RecordCorruptedError on corruption
            if owner_id is not None and not payload.get("owner_id"):
                payload["owner_id"] = owner_id
            records.append(from_payload(payload, record_id))
        return records

    def cross_check(self) -> dict:
        return {
            "tasks": self._task_repo.cross_check(),
            "approvals": self._approval_repo.cross_check(),
        }


def _upsert_run(conn, task: Task) -> None:
    """Keep the control-plane ``runs`` index in lockstep with every Task write."""
    if not task.run_id:
        return
    conn.execute(
        """
        INSERT INTO runs(run_id, task_id, session_id, workspace_id, owner_id,
            status, created_at, updated_at, payload)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id) DO UPDATE SET
            status=excluded.status, updated_at=excluded.updated_at,
            payload=excluded.payload
        """,
        (
            task.run_id, task.task_id, task.session_id, task.workspace_id, task.owner_id,
            task.status.value, task.created_at, task.updated_at,
            _json_dumps(
                {
                    "run_id": task.run_id,
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "workspace_id": task.workspace_id,
                    "owner_id": task.owner_id,
                    "status": task.status.value,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                }
            ),
        ),
    )


def _task_from_payload(payload: dict, record_id: str) -> Task:
    try:
        return Task.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordCorruptedError(f"corrupted Task record for {record_id}") from exc


def _approval_from_payload(payload: dict, record_id: str) -> Approval:
    try:
        return Approval.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordCorruptedError(f"corrupted Approval record for {record_id}") from exc


def _provider_from_payload(payload: dict, record_id: str) -> Provider:
    try:
        return Provider.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordCorruptedError(f"corrupted Provider record for {record_id}") from exc


class SqliteProviderRepository:
    """Provider CRUD（2.7 供应商窗口配置面）。只存非秘密字段；api_key 不在实体里。"""

    def __init__(self, store: SqliteStore, json_root: Path | None = None):
        self._store = store
        self.mirror = _JsonMirror(json_root) if json_root is not None else None

    def create(self, provider: Provider) -> Provider:
        payload = provider.to_dict()
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO providers(
                    provider_id, owner_id, device_id, name, protocol, base_url,
                    model, models, reasoning_tier, timeout, concurrency, state,
                    is_default, last_test_at, last_error, schema_version, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider.provider_id,
                    provider.owner_id,
                    provider.device_id,
                    provider.name,
                    provider.protocol,
                    provider.base_url,
                    provider.model,
                    _json_dumps(provider.models),
                    provider.reasoning_tier,
                    provider.timeout,
                    provider.concurrency,
                    provider.state,
                    int(provider.is_default),
                    provider.last_test_at,
                    provider.last_error,
                    provider.schema_version,
                    _json_dumps(payload),
                ),
            )
        if self.mirror is not None:
            self.mirror.write(provider.provider_id, payload)
        return provider

    def get(self, provider_id: str, owner_id: str) -> Provider:
        row = self._store.query_one(
            "SELECT payload FROM providers WHERE provider_id=? AND owner_id=?",
            (provider_id, canonical_owner_id(owner_id)),
        )
        if row is None:
            raise ProviderNotFoundError(provider_id)
        return _provider_from_payload(_json_loads(row["payload"], provider_id), provider_id)

    def list(self, owner_id: str, device_id: str = "") -> list[Provider]:
        owner_id = canonical_owner_id(owner_id)
        if device_id:
            rows = self._store.query(
                "SELECT payload FROM providers WHERE owner_id=? AND device_id=? ORDER BY name",
                (owner_id, device_id),
            )
        else:
            rows = self._store.query(
                "SELECT payload FROM providers WHERE owner_id=? ORDER BY name", (owner_id,)
            )
        out = []
        for row in rows:
            payload = _json_loads(row["payload"], "provider")
            provider_id = str(payload.get("provider_id", ""))
            out.append(_provider_from_payload(payload, provider_id))
        return out

    def update(self, provider_id: str, owner_id: str, fn: Callable[[Provider], Provider]) -> Provider:
        owner_id = canonical_owner_id(owner_id)
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM providers WHERE provider_id=? AND owner_id=?",
                (provider_id, owner_id),
            ).fetchone()
            if row is None:
                raise ProviderNotFoundError(provider_id)
            current = _provider_from_payload(_json_loads(row["payload"], provider_id), provider_id)
            updated = fn(current)
            payload = updated.to_dict()
            conn.execute(
                """
                UPDATE providers SET name=?, protocol=?, base_url=?, model=?, models=?,
                    reasoning_tier=?, timeout=?, concurrency=?, state=?, is_default=?,
                    last_test_at=?, last_error=?, schema_version=?, payload=?
                WHERE provider_id=? AND owner_id=?
                """,
                (
                    updated.name, updated.protocol, updated.base_url, updated.model,
                    _json_dumps(updated.models), updated.reasoning_tier, updated.timeout,
                    updated.concurrency, updated.state, int(updated.is_default),
                    updated.last_test_at, updated.last_error, updated.schema_version,
                    _json_dumps(payload), provider_id, owner_id,
                ),
            )
        if self.mirror is not None:
            self.mirror.write(provider_id, payload)
        return updated

    def delete(self, provider_id: str, owner_id: str) -> None:
        owner_id = canonical_owner_id(owner_id)
        with self._store.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM providers WHERE provider_id=? AND owner_id=?",
                (provider_id, owner_id),
            )
        if cur.rowcount == 0:
            raise ProviderNotFoundError(provider_id)
        if self.mirror is not None:
            self.mirror.remove(provider_id)

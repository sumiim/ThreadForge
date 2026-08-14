"""SQLite control-plane migration: idempotent import, rollback, concurrency.

These tests exercise the primary SQLite repositories, their write-through JSON
mirror, the idempotent importer, and the workspace-lease table.
"""

from __future__ import annotations

import threading

import pytest

from threadforge_api.domain.entities import Approval, Task
from threadforge_api.domain.enums import ApprovalStatus, TaskStatus
from threadforge_api.domain.errors import (
    ApprovalNotFoundError,
    RunNotFoundError,
    TaskNotFoundError,
)
from threadforge_api.infrastructure.json_repositories import StaleGenerationError
from threadforge_api.infrastructure.jsonutil import write_json_atomic
from threadforge_api.infrastructure.sqlite_repositories import (
    ControlPlaneMigrator,
    SqliteApprovalRepository,
    SqliteLeaseRepository,
    SqliteTaskRepository,
)
from threadforge_api.infrastructure.sqlite_store import SqliteStore

OWNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER = "22222222-2222-4222-8222-222222222222"


def _task(task_id="task_1", status=TaskStatus.QUEUED, owner_id=OWNER_ID):
    return Task(
        task_id=task_id,
        session_id="ses_1",
        workspace_id="w1",
        owner_id=owner_id,
        run_id="run_" + task_id,
        input="hello",
        status=status,
    )


def _approval(approval_id="apr_1", task_id="task_1", owner_id=OWNER_ID):
    return Approval(
        approval_id=approval_id,
        task_id=task_id,
        run_id="run_" + task_id,
        owner_id=owner_id,
        tool_call_id="call_1",
        tool_name="write_file",
        args_digest="d" * 64,
        args_preview={},
    )


def _make_repos(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    tasks = SqliteTaskRepository(store, json_root=tmp_path / "tasks")
    approvals = SqliteApprovalRepository(store, json_root=tmp_path / "approvals")
    return store, tasks, approvals


def test_create_get_roundtrip(tmp_path):
    _, tasks, _ = _make_repos(tmp_path)
    tasks.create(_task())
    loaded = tasks.get("task_1")
    assert loaded.input == "hello"
    assert loaded.status is TaskStatus.QUEUED
    assert loaded.owner_id == OWNER_ID
    # Write-through mirror exists for legacy readers.
    assert (tmp_path / "tasks" / "task_1.json").is_file()


def test_owner_scoping_and_run_lookup(tmp_path):
    _, tasks, _ = _make_repos(tmp_path)
    tasks.create(_task())
    with pytest.raises(TaskNotFoundError):
        tasks.get_for_owner("task_1", OTHER_OWNER)
    with pytest.raises(RunNotFoundError):
        tasks.get_by_run_for_owner("run_task_1", OTHER_OWNER)
    assert tasks.get_by_run_for_owner("run_task_1", OWNER_ID).task_id == "task_1"


def test_update_generation_guard(tmp_path):
    _, tasks, _ = _make_repos(tmp_path)
    task = tasks.create(_task())
    gen = task.generation
    with pytest.raises(StaleGenerationError):
        tasks.update("task_1", lambda t: t, expected_generation=gen + 1)
    updated = tasks.update(
        "task_1",
        lambda t: _set_status(t, TaskStatus.RUNNING),
        expected_generation=gen,
    )
    assert updated.status is TaskStatus.RUNNING
    assert tasks.get("task_1").status is TaskStatus.RUNNING


def test_idempotent_import_no_duplicates(tmp_path):
    store, tasks, approvals = _make_repos(tmp_path)
    # Seed JSON mirror directly (simulating a pre-existing data dir).
    write_json_atomic(tmp_path / "tasks" / "task_1.json", _task().to_dict())
    write_json_atomic(tmp_path / "approvals" / "apr_1.json", _approval().to_dict())

    migrator = ControlPlaneMigrator(tasks, approvals)
    first = migrator.import_json()
    second = migrator.import_json()
    assert first == {"tasks": 1, "approvals": 1}
    assert second == {"tasks": 1, "approvals": 1}
    # No duplicate rows after re-running.
    assert len(tasks.list_stable()) == 1
    assert len(approvals.list_stable()) == 1


def test_import_cross_check_detects_drift(tmp_path):
    _, tasks, approvals = _make_repos(tmp_path)
    write_json_atomic(tmp_path / "tasks" / "task_1.json", _task().to_dict())
    ControlPlaneMigrator(tasks, approvals).import_json()
    assert tasks.cross_check() == []
    # Divergent mirror content is reported.
    payload = _task().to_dict()
    payload["input"] = "changed-in-json"
    write_json_atomic(tmp_path / "tasks" / "task_1.json", payload)
    drift = tasks.cross_check()
    assert drift and drift[0]["reason"] == "diverged"


def test_import_rolls_back_on_corrupted_json(tmp_path):
    _, tasks, approvals = _make_repos(tmp_path)
    write_json_atomic(tmp_path / "tasks" / "task_good.json", _task("task_good").to_dict())
    (tmp_path / "tasks" / "task_broken.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(Exception):
        ControlPlaneMigrator(tasks, approvals).import_json()
    # The valid record must not have been partially committed.
    assert tasks.list_stable() == []


def test_legacy_owner_claim_is_idempotent(tmp_path):
    _, tasks, approvals = _make_repos(tmp_path)
    payload = _task().to_dict()
    payload.pop("owner_id")
    payload["schema_version"] = 1
    write_json_atomic(tmp_path / "tasks" / "task_1.json", payload)
    migrator = ControlPlaneMigrator(tasks, approvals)
    migrator.import_json(owner_id=OWNER_ID)
    assert tasks.get("task_1").owner_id == OWNER_ID
    # Re-import with a different owner must not steal an already-owned record.
    migrator.import_json(owner_id=OTHER_OWNER)
    assert tasks.get("task_1").owner_id == OWNER_ID


def test_concurrent_reads_and_writes_are_consistent(tmp_path):
    store, tasks, _ = _make_repos(tmp_path)
    errors: list[BaseException] = []

    def writer(index: int):
        try:
            for _ in range(50):
                tasks.create(_task(f"task_w{index}_{_}", status=TaskStatus.QUEUED))
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    # 4 writers * 50 tasks = 200 unique rows, no lost updates.
    assert len(tasks.list_stable()) == 200


def test_lease_lifecycle_and_recovery(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    leases = SqliteLeaseRepository(store)
    now = "2026-08-14T00:00:00Z"
    later = "2026-08-14T00:10:00Z"
    token, acquired = leases.acquire(
        workspace_id="w1",
        holder_task_id="task_a",
        holder_run_id="run_a",
        owner_id=OWNER_ID,
        mode="write",
        expires_at=later,
        now=now,
    )
    assert acquired
    # A second writer cannot take the same workspace while the lease is live.
    _, acquired_again = leases.acquire(
        workspace_id="w1",
        holder_task_id="task_b",
        holder_run_id="run_b",
        owner_id=OWNER_ID,
        mode="write",
        expires_at=later,
        now=now,
    )
    assert not acquired_again
    # Renew extends, release frees.
    assert leases.renew("w1", token, "2026-08-14T00:20:00Z")
    assert leases.release("w1", token)
    _, acquired_after_release = leases.acquire(
        workspace_id="w1",
        holder_task_id="task_c",
        holder_run_id="run_c",
        owner_id=OWNER_ID,
        mode="write",
        expires_at=later,
        now=now,
    )
    assert acquired_after_release
    # Expired leases are recoverable.
    assert leases.expired("2026-08-14T01:00:00Z")


def test_event_insert_is_idempotent_and_advances_cursor(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    event = {
        "event_id": "evt_1",
        "sequence": 0,
        "task_id": "task_1",
        "run_id": "run_1",
        "type": "task.started",
        "phase": "system",
        "status": "running",
        "summary": "",
        "trace_id": "run_1",
        "parent_event_id": "",
        "attempt": None,
        "started_at": "",
        "ended_at": "",
        "timestamp": "2026-08-14T00:00:00Z",
        "attributes": {},
        "data": {},
    }
    assert store.insert_event(event) is True
    # Replay of the same event_id is a no-op.
    assert store.insert_event(event) is False
    assert store.cursor_for("run_1") == 0
    replay = store.events_after("run_1", -1)
    assert [item["event_id"] for item in replay] == ["evt_1"]


def test_approval_lifecycle_and_delete_for_tasks(tmp_path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    tasks = SqliteTaskRepository(store, json_root=tmp_path / "tasks")
    approvals = SqliteApprovalRepository(store, json_root=tmp_path / "approvals")
    tasks.create(_task("task_1", status=TaskStatus.COMPLETED))
    approvals.create(_approval("apr_1", "task_1"))
    assert approvals.list_pending_for_task("task_1")[0].approval_id == "apr_1"
    approvals.update("apr_1", lambda a: _decide(a))
    assert approvals.get("apr_1").status is ApprovalStatus.APPROVED
    removed = approvals.delete_for_tasks({"task_1"}, OWNER_ID)
    assert removed == ["apr_1"]
    with pytest.raises(ApprovalNotFoundError):
        approvals.get("apr_1")


def _set_status(task, status):
    task.status = status
    return task


def _decide(approval):
    approval.status = ApprovalStatus.APPROVED
    approval.decision = "approved"
    return approval

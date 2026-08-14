"""Workspace write isolation: snapshot, apply, conflict, cleanup, leases."""

from __future__ import annotations

from pathlib import Path

import pytest

from threadforge_api.infrastructure.sqlite_repositories import SqliteLeaseRepository
from threadforge_api.infrastructure.sqlite_store import SqliteStore
from threadforge_api.infrastructure.workspace_catalog import WorkspaceEntry
from threadforge_api.infrastructure.workspace_isolation import (
    WorkspaceIsolation,
    WorkspaceIsolationError,
    WorkspaceLeaseBusyError,
)

OWNER_ID = "11111111-1111-4111-8111-111111111111"


class _Catalog:
    def __init__(self, path: Path):
        self.entry = WorkspaceEntry(workspace_id="w1", name="W1", canonical_path=path)

    def recheck(self, workspace_id: str) -> WorkspaceEntry:
        if workspace_id != self.entry.workspace_id:
            raise WorkspaceIsolationError("not found")
        return self.entry


def _make_isolation(tmp_path, workspace: Path):
    store = SqliteStore(tmp_path / "control.sqlite3")
    leases = SqliteLeaseRepository(store)
    return WorkspaceIsolation(tmp_path / "data", leases, _Catalog(workspace))


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_snapshot_isolates_writes_and_applies_on_success(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, "keep.txt", "original")
    isolation = _make_isolation(tmp_path, workspace)

    handle = isolation.prepare(
        task_id="task_1", run_id="run_1", workspace_id="w1", owner_id=OWNER_ID
    )
    # The agent runs in the snapshot; writes must not leak before finalize.
    _write(handle.root, "new.txt", "created")
    _write(handle.root, "keep.txt", "modified")
    assert (workspace / "new.txt").exists() is False
    assert (workspace / "keep.txt").read_text() == "original"

    result = isolation.finalize(handle, apply=True)
    assert result["applied"] is True
    assert set(result["changed_paths"]) == {"keep.txt", "new.txt"}
    assert (workspace / "new.txt").read_text() == "created"
    assert (workspace / "keep.txt").read_text() == "modified"
    # Snapshot is cleaned up.
    assert not handle.root.exists()


def test_rejected_run_discards_snapshot_without_apply(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, "data.txt", "keep me")
    isolation = _make_isolation(tmp_path, workspace)

    handle = isolation.prepare(
        task_id="task_1", run_id="run_1", workspace_id="w1", owner_id=OWNER_ID
    )
    _write(handle.root, "data.txt", "changed")
    result = isolation.finalize(handle, apply=False)
    assert result["applied"] is False
    assert (workspace / "data.txt").read_text() == "keep me"


def test_conflict_is_detected_and_not_overwritten(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, "file.txt", "baseline")
    isolation = _make_isolation(tmp_path, workspace)

    handle = isolation.prepare(
        task_id="task_1", run_id="run_1", workspace_id="w1", owner_id=OWNER_ID
    )
    # The agent edits the snapshot...
    _write(handle.root, "file.txt", "agent-change")
    # ...but the real workspace changed underneath during the run.
    _write(workspace, "file.txt", "external-change")

    result = isolation.finalize(handle, apply=True)
    assert result["applied"] is False
    assert result["conflicts"] == ["file.txt"]
    assert (workspace / "file.txt").read_text() == "external-change"


def test_lease_serializes_same_workspace_writers(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    isolation = _make_isolation(tmp_path, workspace)

    handle = isolation.prepare(
        task_id="task_a", run_id="run_a", workspace_id="w1", owner_id=OWNER_ID
    )
    with pytest.raises(WorkspaceLeaseBusyError):
        isolation.prepare(
            task_id="task_b", run_id="run_b", workspace_id="w1", owner_id=OWNER_ID
        )
    isolation.finalize(handle, apply=False)
    # After release, a second writer can take the workspace.
    handle2 = isolation.prepare(
        task_id="task_b", run_id="run_b", workspace_id="w1", owner_id=OWNER_ID
    )
    isolation.finalize(handle2, apply=False)


def test_recovery_releases_expired_lease_and_cleans_orphans(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, "a.txt", "x")
    isolation = _make_isolation(tmp_path, workspace)

    handle = isolation.prepare(
        task_id="task_1", run_id="run_1", workspace_id="w1", owner_id=OWNER_ID
    )
    # Simulate a crash: the snapshot dir is left behind and the lease expires.
    snapshot = handle.root
    assert snapshot.is_dir()
    # Expire the lease directly.
    from threadforge_api.infrastructure.workspace_isolation import _expires_at

    expired = _expires_at("2000-01-01T00:00:00Z")
    assert isolation._leases.release(handle.workspace_id, handle.lease_token)
    isolation._leases.acquire(
        workspace_id="w1",
        holder_task_id="task_x",
        holder_run_id="run_x",
        owner_id=OWNER_ID,
        mode="write",
        expires_at=expired,
        now="2000-01-01T00:00:00Z",
    )
    removed = isolation.recover_expired()
    assert removed >= 0
    # The orphaned snapshot from the crash is gone after recovery.
    assert not snapshot.exists()

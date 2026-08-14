"""Workspace write isolation via per-run snapshot directories.

A write task never mutates the user's workspace directly. Instead it runs in a
snapshot copy under ``<data_dir>/isolation/<workspace_id>/<run_id>/ws``. On a
successful completion the changed files are applied back to the real workspace
with a conflict check (a file whose real-workspace content changed since the
snapshot is a conflict and is never overwritten); on any other terminal state
the snapshot is discarded. Read tasks share the same mechanism but simply
produce no diff.

Every operation is idempotent: lease acquisition uses ``INSERT ... ON
CONFLICT`` keyed on ``workspace_id``, cleanup uses ``shutil.rmtree(...,
ignore_errors=True)``, and recovery on startup releases expired leases and
removes orphaned isolation directories. Nothing here bypasses approval, budget
or audit — it only relocates where the already-authorized writes land.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.entities import utc_now
from ..domain.identity import canonical_owner_id
from .sqlite_repositories import SqliteLeaseRepository
from .workspace_catalog import WorkspaceEntry, WorkspaceNotFoundError

# Directories that are never copied into a snapshot. ``.git`` is excluded so the
# isolated working tree cannot corrupt the repository's object store or locks.
# Everything else (including vendored dependencies) is copied so tool commands
# keep working; this is the correctness-first P1 trade-off.
_ISOLATION_EXCLUDED = {
    ".git",
    ".pico",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    # node_modules is huge (369MB here) and re-installable via pnpm install.
    # Tasks that need dependencies must run pnpm/npm install inside the snapshot.
    "node_modules",
}

LEASE_TTL_SECONDS = 3600


class WorkspaceIsolationError(RuntimeError):
    pass


class WorkspaceLeaseBusyError(WorkspaceIsolationError):
    pass


@dataclass
class IsolationHandle:
    task_id: str
    run_id: str
    workspace_id: str
    owner_id: str
    original_path: Path
    root: Path
    baseline: dict[str, str]
    lease_token: str
    changed_paths: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    applied: bool = False


def _manifest(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _ISOLATION_EXCLUDED for part in path.relative_to(root).parts):
            continue
        try:
            out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def _diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    return sorted(changed)


class WorkspaceIsolation:
    """Creates, applies and cleans up snapshot isolation directories."""

    def __init__(
        self,
        data_dir: Path,
        lease_repo: SqliteLeaseRepository,
        workspace_catalog,
    ):
        self._data_dir = Path(data_dir).resolve()
        self._leases = lease_repo
        self._catalog = workspace_catalog

    def isolation_root(self) -> Path:
        return self._data_dir / "isolation"

    def _entry(self, workspace_id: str) -> WorkspaceEntry:
        try:
            return self._catalog.recheck(workspace_id)
        except WorkspaceNotFoundError:
            raise WorkspaceIsolationError(f"workspace not found: {workspace_id}") from None

    def prepare(
        self,
        *,
        task_id: str,
        run_id: str,
        workspace_id: str,
        owner_id: str,
    ) -> IsolationHandle:
        owner_id = canonical_owner_id(owner_id)
        entry = self._entry(workspace_id)
        now = utc_now()
        expires_at = _expires_at(now)
        lease_token, acquired = self._leases.acquire(
            workspace_id=workspace_id,
            holder_task_id=task_id,
            holder_run_id=run_id,
            owner_id=owner_id,
            mode="write",
            expires_at=expires_at,
            now=now,
        )
        if not acquired:
            raise WorkspaceLeaseBusyError(f"workspace is busy: {workspace_id}")

        root = self.isolation_root() / workspace_id / run_id / "ws"
        try:
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                entry.canonical_path,
                root,
                ignore=shutil.ignore_patterns(*_ISOLATION_EXCLUDED),
                symlinks=True,
                dirs_exist_ok=True,
            )
        except Exception as exc:
            self._leases.release(workspace_id, lease_token)
            raise WorkspaceIsolationError(f"failed to snapshot workspace: {workspace_id}") from exc

        baseline = _manifest(root)
        return IsolationHandle(
            task_id=task_id,
            run_id=run_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            original_path=entry.canonical_path,
            root=root,
            baseline=baseline,
            lease_token=lease_token,
        )

    def changed_paths(self, handle: IsolationHandle) -> list[str]:
        return _diff(handle.baseline, _manifest(handle.root))

    def finalize(self, handle: IsolationHandle, *, apply: bool) -> dict:
        """Apply (when ``apply`` and conflict-free) then clean up. Idempotent."""
        changed = self.changed_paths(handle)
        handle.changed_paths = changed
        if apply and changed:
            handle.conflicts = self._conflicts(handle, changed)
            if not handle.conflicts:
                self._apply(handle, changed)
                handle.applied = True
        self._cleanup(handle)
        return {
            "workspace_id": handle.workspace_id,
            "changed_paths": changed,
            "conflicts": handle.conflicts,
            "applied": handle.applied,
        }

    def _conflicts(self, handle: IsolationHandle, changed: list[str]) -> list[str]:
        conflicts = []
        for relative in changed:
            original = handle.original_path / relative
            baseline_hash = handle.baseline.get(relative)
            try:
                current_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            except OSError:
                current_hash = None
            if baseline_hash is None:
                # Newly created file: conflict only if it already exists.
                if original.exists():
                    conflicts.append(relative)
            elif current_hash is not None and current_hash != baseline_hash:
                conflicts.append(relative)
        return conflicts

    def _apply(self, handle: IsolationHandle, changed: list[str]) -> None:
        for relative in changed:
            snapshot_path = handle.root / relative
            target = handle.original_path / relative
            if not snapshot_path.is_file():
                # Deleted in the snapshot: remove from the real workspace.
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_atomic(snapshot_path, target)

    def _cleanup(self, handle: IsolationHandle) -> None:
        self._leases.release(handle.workspace_id, handle.lease_token)
        run_dir = handle.root.parent
        shutil.rmtree(run_dir, ignore_errors=True)

    def recover_expired(self) -> int:
        """Release expired leases and drop orphaned isolation dirs. Idempotent."""
        now = utc_now()
        removed = 0
        for lease in self._leases.expired(now):
            self._leases.release(lease["workspace_id"], lease["lease_token"])
            removed += 1
        base = self.isolation_root()
        if not base.is_dir():
            return removed
        # Iterate over a materialized list so removing directories during the
        # walk cannot trip the pathlib iterator.
        for run_dir in list(base.rglob("ws")):
            try:
                run_dir.relative_to(base)
            except ValueError:
                continue
            # A live lease holds the workspace; only reclaim orphaned dirs.
            workspace_id = run_dir.parent.parent.name if run_dir.parent.name else ""
            if workspace_id and self._leases.holder(workspace_id, now) is None:
                shutil.rmtree(run_dir.parent, ignore_errors=True)
                removed += 1
        return removed


def _expires_at(now: str) -> str:
    from datetime import datetime, timedelta

    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat().replace("+00:00", "Z")


def _copy_atomic(source: Path, target: Path) -> None:
    """Copy a file atomically so a crash cannot leave a half-written target."""
    temp = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temp)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)

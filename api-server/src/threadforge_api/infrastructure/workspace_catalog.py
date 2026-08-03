"""Workspace allowlist loading and canonical-path validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..domain.errors import AppError, NotFoundError


class WorkspaceConfigError(AppError):
    http_status = 503
    code = "workspace_config_error"


class WorkspaceNotFoundError(NotFoundError):
    code = "workspace_not_found"


def _contains(a: Path, b: Path) -> bool:
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class WorkspaceEntry:
    workspace_id: str
    name: str
    canonical_path: Path
    available: bool = True
    is_git: bool = False


class WorkspaceCatalog:
    def __init__(self, workspaces_file: Path, data_dir: Path):
        self._workspaces_file = Path(workspaces_file).resolve()
        self._data_dir = Path(data_dir).resolve()
        self._entries: dict[str, WorkspaceEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._workspaces_file.is_file():
            raise WorkspaceConfigError(f"workspaces file missing: {self._workspaces_file}")
        try:
            raw = json.loads(self._workspaces_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkspaceConfigError(f"unreadable workspaces file: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("workspaces"), list):
            raise WorkspaceConfigError("workspaces config must contain a list")
        raw_workspaces = raw["workspaces"]
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        entries: dict[str, WorkspaceEntry] = {}
        for item in raw_workspaces:
            if not isinstance(item, dict):
                raise WorkspaceConfigError("workspace entry must be an object")
            workspace_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip() or workspace_id
            path_text = str(item.get("path", "")).strip()
            if not workspace_id or not path_text:
                raise WorkspaceConfigError("workspace entry needs id and path")
            if workspace_id in seen_ids:
                raise WorkspaceConfigError(f"duplicate workspace id: {workspace_id}")
            try:
                path = Path(path_text).expanduser().resolve()
            except OSError as exc:
                raise WorkspaceConfigError(f"workspace path is unreadable: {path_text}") from exc
            if not path.is_dir():
                raise WorkspaceConfigError(f"workspace path is not a directory: {path}")
            canonical = str(path)
            if canonical in seen_paths:
                raise WorkspaceConfigError(f"duplicate real path: {path}")
            # data dir must not contain / be contained by a workspace.
            if _contains(self._data_dir, path) or _contains(path, self._data_dir):
                raise WorkspaceConfigError(f"data dir and workspace must be disjoint: {path}")
            # workspaces config file must not live inside a workspace.
            if _contains(path, self._workspaces_file):
                raise WorkspaceConfigError(f"workspaces file must not live inside a workspace: {path}")
            seen_ids.add(workspace_id)
            seen_paths.add(canonical)
            is_git = (path / ".git").exists()
            entries[workspace_id] = WorkspaceEntry(
                workspace_id=workspace_id,
                name=name,
                canonical_path=path,
                is_git=is_git,
            )
        if not entries:
            raise WorkspaceConfigError("workspaces allowlist is empty")
        self._entries = entries

    def get(self, workspace_id: str) -> WorkspaceEntry:
        entry = self._entries.get(workspace_id)
        if entry is None:
            raise WorkspaceNotFoundError(workspace_id)
        return entry

    def recheck(self, workspace_id: str) -> WorkspaceEntry:
        """Re-resolve and confirm the path still equals the startup canonical path
        and the directory still exists."""
        entry = self.get(workspace_id)
        if Path(entry.canonical_path).resolve() != entry.canonical_path:
            raise WorkspaceConfigError(f"workspace path changed since startup: {workspace_id}")
        if not entry.canonical_path.is_dir():
            raise WorkspaceConfigError(f"workspace directory removed: {workspace_id}")
        return entry

    def list(self) -> list[WorkspaceEntry]:
        return list(self._entries.values())

    def validate_all(self) -> None:
        for workspace_id in self._entries:
            self.recheck(workspace_id)

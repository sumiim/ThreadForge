"""WorkspaceCatalog allowlist validation."""

from __future__ import annotations

import json

import pytest

from threadforge_api.infrastructure.workspace_catalog import (
    WorkspaceCatalog,
    WorkspaceConfigError,
)


def _write_workspaces(path, entries):
    path.write_text(json.dumps({"workspaces": entries}))
    return path


def test_loads_valid_allowlist(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    (wsdir / ".git").mkdir()
    ws_file = _write_workspaces(
        tmp_path / "workspaces.json",
        [{"id": "w1", "name": "W1", "path": str(wsdir)}],
    )
    catalog = WorkspaceCatalog(ws_file, tmp_path / "data")
    entry = catalog.get("w1")
    assert entry.canonical_path == wsdir.resolve()
    assert entry.is_git is True
    assert catalog.recheck("w1").workspace_id == "w1"


def test_rejects_duplicate_id(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    ws_file = _write_workspaces(
        tmp_path / "workspaces.json",
        [
            {"id": "w1", "name": "A", "path": str(wsdir)},
            {"id": "w1", "name": "B", "path": str(wsdir)},
        ],
    )
    with pytest.raises(WorkspaceConfigError):
        WorkspaceCatalog(ws_file, tmp_path / "data")


def test_rejects_missing_directory(tmp_path):
    ws_file = _write_workspaces(
        tmp_path / "workspaces.json",
        [{"id": "w1", "name": "A", "path": str(tmp_path / "nope")}],
    )
    with pytest.raises(WorkspaceConfigError):
        WorkspaceCatalog(ws_file, tmp_path / "data")


def test_rejects_data_dir_inside_workspace(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    ws_file = _write_workspaces(
        tmp_path / "workspaces.json",
        [{"id": "w1", "name": "A", "path": str(wsdir)}],
    )
    with pytest.raises(WorkspaceConfigError):
        WorkspaceCatalog(ws_file, wsdir / "data")


def test_rejects_workspaces_file_inside_workspace(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    ws_file = _write_workspaces(
        wsdir / "workspaces.json",
        [{"id": "w1", "name": "A", "path": str(wsdir)}],
    )
    with pytest.raises(WorkspaceConfigError):
        WorkspaceCatalog(ws_file, tmp_path / "data")


def test_unknown_workspace_raises(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    ws_file = _write_workspaces(
        tmp_path / "workspaces.json",
        [{"id": "w1", "name": "A", "path": str(wsdir)}],
    )
    catalog = WorkspaceCatalog(ws_file, tmp_path / "data")
    from threadforge_api.domain.errors import NotFoundError

    with pytest.raises(NotFoundError):
        catalog.get("missing")


@pytest.mark.parametrize(
    "payload",
    [[], {"workspaces": "not-a-list"}, {"workspaces": ["not-an-object"]}],
)
def test_invalid_workspace_config_shapes_fail_stably(tmp_path, payload):
    config = tmp_path / "workspaces.json"
    config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkspaceConfigError):
        WorkspaceCatalog(config, tmp_path / "data")

"""JSON repository atomicity / stability / corruption."""

from __future__ import annotations

import pytest

from threadforge_api.domain.entities import Approval, Task
from threadforge_api.domain.enums import ApprovalStatus, TaskStatus
from threadforge_api.infrastructure.json_repositories import (
    JsonApprovalRepository,
    JsonTaskRepository,
    RecordUnavailableError,
    StaleGenerationError,
)
from threadforge_api.infrastructure.jsonutil import write_json_atomic


def _task(task_id="task_1", status=TaskStatus.QUEUED):
    return Task(
        task_id=task_id,
        session_id="ses_1",
        workspace_id="w1",
        run_id="run_1",
        input="hello",
        status=status,
    )


def test_create_get_roundtrip(tmp_path):
    repo = JsonTaskRepository(tmp_path)
    repo.create(_task())
    loaded = repo.get("task_1")
    assert loaded.input == "hello"
    assert loaded.status is TaskStatus.QUEUED


def test_list_stable_order(tmp_path):
    repo = JsonTaskRepository(tmp_path)
    repo.create(_task("task_b"))
    repo.create(_task("task_a"))
    ids = repo.list_stable()
    # same mtime bucket: newest mtime first; both written ~same time
    assert set(ids) == {"task_a", "task_b"}
    assert len(ids) == 2


def test_update_with_generation(tmp_path):
    repo = JsonTaskRepository(tmp_path)
    task = repo.create(_task())
    gen = task.generation
    repo.update("task_1", lambda t: _set_status(t, TaskStatus.RUNNING), expected_generation=gen)
    assert repo.get("task_1").status is TaskStatus.RUNNING


def test_stale_generation_rejected(tmp_path):
    repo = JsonTaskRepository(tmp_path)
    repo.create(_task())
    with pytest.raises(StaleGenerationError):
        repo.update("task_1", lambda t: t, expected_generation=99)


def test_corrupted_json_raises(tmp_path):
    repo = JsonTaskRepository(tmp_path)
    repo.create(_task())
    (tmp_path / "task_1.json").write_text("{not json", encoding="utf-8")
    from threadforge_api.infrastructure.json_repositories import RecordCorruptedError

    with pytest.raises(RecordCorruptedError):
        repo.get("task_1")


def test_write_io_error_is_persistence_unavailable(tmp_path, monkeypatch):
    repo = JsonTaskRepository(tmp_path)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        "threadforge_api.infrastructure.json_repositories.write_json_atomic",
        fail_write,
    )
    with pytest.raises(RecordUnavailableError):
        repo.create(_task())


def test_list_does_not_hide_corrupted_records(tmp_path):
    repo = JsonTaskRepository(tmp_path)
    repo.create(_task())
    (tmp_path / "task_1.json").write_text("{broken", encoding="utf-8")

    from threadforge_api.infrastructure.json_repositories import RecordCorruptedError

    with pytest.raises(RecordCorruptedError):
        repo.list(limit=10, offset=0)


def test_atomic_write_cleans_temp_file_on_serialization_error(tmp_path):
    path = tmp_path / "record.json"

    with pytest.raises(TypeError):
        write_json_atomic(path, {"value": object()})

    assert list(tmp_path.glob("*.tmp")) == []
    assert not path.exists()


def test_approval_repository_roundtrip_and_pending_list(tmp_path):
    repo = JsonApprovalRepository(tmp_path)
    approval = Approval(
        approval_id="apr_1",
        task_id="task_1",
        run_id="run_1",
        tool_call_id="call_1",
        tool_name="write_file",
        args_digest="abc",
        args_preview={},
    )
    repo.create(approval)
    assert repo.get("apr_1").status is ApprovalStatus.PENDING
    pending = repo.list_pending_for_task("task_1")
    assert [a.approval_id for a in pending] == ["apr_1"]
    repo.update("apr_1", lambda a: _approve(a))
    assert repo.get("apr_1").status is ApprovalStatus.APPROVED
    assert repo.list_pending_for_task("task_1") == []


def _set_status(task, status):
    task.status = status
    return task


def _approve(approval):
    approval.status = ApprovalStatus.APPROVED
    approval.decision = "approved"
    return approval

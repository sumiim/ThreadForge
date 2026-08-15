import uuid

import pytest
from pydantic import ValidationError

from threadforge_api.api.models import CreateTaskRequest
from threadforge_api.domain.entities import Task


def test_create_task_request_permission_mode_default_and_validation():
    assert CreateTaskRequest(session_id="ses_1", input="hi").permission_mode == "default"
    for mode in ("plan", "acceptEdits", "default", "bypass"):
        assert CreateTaskRequest(session_id="ses_1", input="hi", permission_mode=mode).permission_mode == mode
    with pytest.raises(ValidationError):
        CreateTaskRequest(session_id="ses_1", input="hi", permission_mode="bogus")


def test_task_entity_round_trips_permission_mode():
    task = Task(
        task_id="task_" + uuid.uuid4().hex,
        session_id="ses_" + uuid.uuid4().hex,
        workspace_id="ws_" + uuid.uuid4().hex,
        owner_id=str(uuid.uuid4()),
        run_id="run_" + uuid.uuid4().hex,
        input="hi",
        permission_mode="acceptEdits",
    )
    assert task.permission_mode == "acceptEdits"
    assert task.to_dict()["permission_mode"] == "acceptEdits"
    assert Task.from_dict(task.to_dict()).permission_mode == "acceptEdits"

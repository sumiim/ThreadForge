import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.delegates import (
    RESEARCH_ALLOWED_TOOLS,
    REVIEW_ALLOWED_TOOLS,
    RoleDelegateSpec,
    create_role_delegate,
    normalize_review_result,
)
from pico.event_sink import CompositeSink, EventCollector, NullSink
from pico.task_state import STATUS_FAILED, TaskState


def build_parent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    collector = EventCollector()
    parent = Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        event_sink=CompositeSink(collector, NullSink()),
    )
    state = TaskState.create(run_id="run_parent", task_id="task_parent", user_request="Coordinate")
    parent.current_task_state = state
    parent.run_store.start_run(state)
    return parent, collector


def test_research_role_is_read_only_isolated_and_observable(tmp_path):
    parent, collector = build_parent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"blocked.txt","content":"no"}}</tool>',
            "<final>Findings: README.md\nCandidate files: README.md\nSuggested action: inspect it</final>",
        ],
    )
    spec = RoleDelegateSpec(
        role="research",
        task="Find the relevant file.",
        allowed_tools=RESEARCH_ALLOWED_TOOLS,
    )

    child, result = create_role_delegate(parent, spec)

    assert "Findings:" in result
    assert child.allowed_tools == RESEARCH_ALLOWED_TOOLS
    assert child.allow_checkpoint is False
    assert child.allow_durable_memory_write is False
    assert child.session_path.as_posix().startswith(".memory-sessions/")
    assert not (tmp_path / "blocked.txt").exists()
    assert child.current_task_state.sandbox_violations == 1
    assert parent.child_task_states == [child.current_task_state]
    events = collector.snapshot()
    assert any(event["event"] == "delegate_started" and event["agent_role"] == "research" for event in events)
    assert any(event["event"] == "delegate_finished" and event["agent_role"] == "research" for event in events)


def test_review_role_uses_packet_and_normalizes_malformed_status(tmp_path):
    parent, collector = build_parent(tmp_path, ["<final>Looks incomplete.</final>"])
    parent.record({"role": "user", "content": "parent-history-must-not-reach-review", "created_at": "now"})
    spec = RoleDelegateSpec(
        role="review",
        task="Review the change.",
        allowed_tools=REVIEW_ALLOWED_TOOLS,
        focus_paths=("README.md",),
        acceptance="README must contain demo.",
        context_summary="Coordinator inspected README.md.",
    )

    child, result = create_role_delegate(parent, spec)

    assert result.startswith("status: needs_fix\nissue: malformed_review_status")
    assert parent.current_task_state.malformed_output_recovered == 1
    assert "Acceptance criteria: README must contain demo." in parent.model_client.prompts[0]
    assert "Execution context: Coordinator inspected README.md." in parent.model_client.prompts[0]
    assert "parent-history-must-not-reach-review" not in parent.model_client.prompts[0]
    assert child.current_task_state.checkpoint_id == ""
    events = collector.snapshot()
    assert any(event["event"] == "review_requested" for event in events)
    assert any(event["event"] == "review_failed" for event in events)


def test_role_delegate_failure_finalizes_child_and_preserves_failure_event(tmp_path):
    parent, collector = build_parent(tmp_path, [])
    spec = RoleDelegateSpec(
        role="research",
        task="Fail while researching.",
        allowed_tools=RESEARCH_ALLOWED_TOOLS,
    )

    with pytest.raises(RuntimeError, match="fake model ran out of outputs"):
        create_role_delegate(parent, spec)

    child_state = parent.child_task_states[0]
    assert child_state.status == STATUS_FAILED
    assert parent.run_store.report_path(child_state).exists()
    event = next(event for event in collector.snapshot() if event["event"] == "delegate_failed")
    assert event["error_type"] == "RuntimeError"
    assert "ran out of outputs" not in json.dumps(event)


def test_role_delegate_rejects_privilege_widening():
    spec = RoleDelegateSpec(
        role="review",
        task="Review.",
        allowed_tools=("read_file", "write_file"),
        focus_paths=("README.md",),
        acceptance="Correct.",
        context_summary="Changed README.",
    )

    with pytest.raises(ValueError, match="outside its allowlist"):
        create_role_delegate(object(), spec)


def test_review_normalizer_is_idempotent():
    first = normalize_review_result("unexpected")
    second = normalize_review_result(first["text"])

    assert first["recovered"] is True
    assert second["recovered"] is False
    assert second["issue_codes"] == ["malformed_review_status"]

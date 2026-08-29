from pico.task_state import (
    STOP_REASON_CONVERGENCE_GUARD_TRIGGERED,
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_RETRY_LIMIT_REACHED,
    STOP_REASON_STEP_LIMIT_REACHED,
    TaskState,
)


def test_task_state_starts_running_with_empty_progress():
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    assert state.task_id == "task_001"
    assert state.run_id == "run_001"
    assert state.user_request == "Inspect the repo."
    assert state.status == "running"
    assert state.tool_steps == 0
    assert state.attempts == 0
    assert state.last_tool == ""
    assert state.stop_reason == ""
    assert state.final_answer == ""
    assert state.phase == "UNDERSTAND_REQUEST"
    assert state.checklist
    assert state.max_tool_steps == 6
    assert state.max_read_files == 4


def test_task_state_records_success_and_final_answer():
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Fix the bug.")
    state.record_attempt()
    state.record_tool("read_file")
    state.finish_success("Done.")

    assert state.attempts == 1
    assert state.tool_steps == 1
    assert state.last_tool == "read_file"
    assert state.status == "completed"
    assert state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
    assert state.final_answer == "Done."


def test_task_state_records_step_limit_stop_reason():
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Try again.")

    state.stop_step_limit()

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_STEP_LIMIT_REACHED


def test_task_state_records_convergence_guard_separately_from_step_limit():
    state = TaskState.create(run_id="run_guard", task_id="task_guard", user_request="Inspect.")

    state.stop_convergence_guard("Best effort.")

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_CONVERGENCE_GUARD_TRIGGERED
    assert state.final_answer == "Best effort."


def test_task_state_records_retry_limit_stop_reason():
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Try again.")

    state.stop_retry_limit()

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_RETRY_LIMIT_REACHED


def test_task_state_snapshot_keeps_final_answer():
    state = TaskState.create(run_id="run_005", task_id="task_005", user_request="Return the answer.")
    state.finish_success("Final answer.")

    snapshot = state.to_dict()

    assert snapshot["final_answer"] == "Final answer."
    assert snapshot["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert snapshot["phase"] == "FINAL"


def test_task_state_persists_plan_revisions_and_safe_error_metadata():
    state = TaskState.create(run_id="run_plan", task_id="task_plan", user_request="plan")
    state.plan_id = "plan_1"
    state.plan_revision = 2
    state.plan_history = [{"plan_id": "plan_1", "revision": 1}]
    state.replan_reasons = ["review needs a fix"]
    state.record_error(stage="planning", code="model_timeout", retryable=True, attempts=3)

    snapshot = state.to_dict()

    assert snapshot["plan_history"] == [{"plan_id": "plan_1", "revision": 1}]
    assert snapshot["replan_reasons"] == ["review needs a fix"]
    assert snapshot["error_code"] == "model_timeout"
    assert TaskState.from_dict(snapshot).plan_revision == 2


def test_task_state_snapshot_keeps_checkpoint_reference_without_body():
    state = TaskState.create(run_id="run_006", task_id="task_006", user_request="Resume the task.")
    state.checkpoint_id = "ckpt_001"
    state.resume_status = "full-valid"

    snapshot = state.to_dict()

    assert snapshot["checkpoint_id"] == "ckpt_001"
    assert snapshot["resume_status"] == "full-valid"
    assert snapshot["phase"] == "UNDERSTAND_REQUEST"
    assert snapshot["next_step"] == "Understand the request and acceptance criteria"


def test_task_state_requires_post_tool_reasoning_before_next_decision():
    state = TaskState.create(run_id="run_008", task_id="task_008", user_request="Inspect files.")

    state.begin_post_tool_reasoning("read_file")
    assert state.requires_post_tool_reasoning is True
    assert state.phase == "ANALYZE_CONTEXT"
    assert "read_file" in state.next_step

    state.finish_post_tool_reasoning()
    assert state.requires_post_tool_reasoning is False
    assert state.phase == "ACT_OR_ANSWER"


def test_task_state_round_trips_harness_counters_and_affected_paths():
    state = TaskState.create(run_id="run_007", task_id="task_007", user_request="Patch files.")
    state.record_sandbox_violation()
    state.record_malformed_output_recovered()
    state.record_affected_paths(["b.py", "a.py", "b.py", ""])

    restored = TaskState.from_dict(state.to_dict())

    assert restored.sandbox_violations == 1
    assert restored.malformed_output_recovered == 1
    assert restored.affected_paths == ["a.py", "b.py"]

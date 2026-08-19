from types import SimpleNamespace

from threadforge_api.infrastructure.worker_hub import (
    _append_run_index,
    _sanitize_event_data,
)


def test_read_only_tool_event_keeps_allowlisted_arguments_and_preview():
    requested = _sanitize_event_data(
        "tool.requested",
        {
            "tool_call_id": "call_read",
            "tool_name": "read_file",
            "args_preview": {
                "path": "README.md",
                "start": 1,
                "end": 8,
                "content": "must not reach the frontend",
            },
        },
    )
    completed = _sanitize_event_data(
        "tool.completed",
        {
            "tool_call_id": "call_read",
            "tool_name": "read_file",
            "tool_status": "ok",
            "result_preview": "# README.md\nhello",
        },
    )

    assert requested["args_preview"] == {"path": "README.md", "start": 1, "end": 8}
    assert completed["result_preview"] == "# README.md\nhello"


def test_shell_event_keeps_command_and_result_preview():
    # §7.8.9 决策（2026-08-19）：审计/审批需要看到 run_shell 的具体命令，
    # 脱敏 + 限长后放行 command 字段。
    requested = _sanitize_event_data(
        "tool.requested",
        {
            "tool_call_id": "call_shell",
            "tool_name": "run_shell",
            "args_preview": {"command": "echo private", "timeout": 30},
        },
    )
    completed = _sanitize_event_data(
        "tool.completed",
        {
            "tool_call_id": "call_shell",
            "tool_name": "run_shell",
            "tool_status": "ok",
            "result_preview": "private command output",
        },
    )

    assert requested["args_preview"] == {"command": "echo private"}
    assert completed["result_preview"] == "private command output"


def test_agent_state_event_is_bounded_and_does_not_expose_evidence():
    state = _sanitize_event_data(
        "agent.state",
        {
            "phase": "ANALYZE_CONTEXT",
            "next_step": "read the next file",
            "checklist": ["one", "two"],
            "completed_items": ["one"],
            "tool_steps": "2",
            "read_files": 1,
            "secret_output": "must not reach the frontend",
        },
    )

    assert state["phase"] == "ANALYZE_CONTEXT"
    assert state["checklist"] == ["one", "two"]
    assert state["tool_steps"] == 2
    assert "secret_output" not in state


def test_streaming_events_keep_only_safe_public_fields():
    retrying = _sanitize_event_data(
        "model.retrying",
        {
            "stage": "planning",
            "attempt": 1,
            "max_attempts": 2,
            "error_code": "model_timeout",
            "retry_delay_seconds": "0.5",
            "private_error": "provider stack trace",
            "reset_stream": True,
        },
    )
    delta = _sanitize_event_data(
        "assistant.delta",
        {"text": "visible answer", "planning_json": "must not pass"},
    )
    protocol_retrying = _sanitize_event_data(
        "model.protocol_retrying",
        {
            "stage": "execute",
            "attempt": 1,
            "max_attempts": 2,
            "response_chars": 84,
            "detected_format": "json_object",
            "top_level_keys": ["answer", "private model response"],
            "response_hash": "0123456789abcdef",
            "raw_model_output": "must not pass",
        },
    )
    heartbeat = _sanitize_event_data(
        "model.heartbeat",
        {
            "stage": "execute",
            "elapsed_seconds": 3.4,
            "run_elapsed_seconds": 12.8,
            "round": 4,
            "raw_model_output": "must not pass",
        },
    )

    assert retrying == {
        "stage": "planning",
        "attempt": 1,
        "max_attempts": 2,
        "error_code": "model_timeout",
        "retry_delay_seconds": 0.5,
        "elapsed_seconds": 0.0,
        "reset_stream": True,
    }
    assert delta == {"text": "visible answer"}
    assert protocol_retrying == {
        "stage": "execute",
        "attempt": 1,
        "max_attempts": 2,
        "error_code": "model_protocol_invalid",
        "response_chars": 84,
        "detected_format": "json_object",
        "top_level_keys": ["answer"],
        "response_hash": "0123456789abcdef",
        "reset_stream": True,
    }
    assert heartbeat == {
        "stage": "execute",
        "elapsed_seconds": 3.4,
        "run_elapsed_seconds": 12.8,
        "round": 4,
    }


def test_run_index_keeps_tool_command_and_result_for_audit():
    task = SimpleNamespace(run_index=[], updated_at="")
    _append_run_index(
        task,
        {
            "event_id": "evt_tool_req",
            "run_id": "run_1",
            "type": "tool.requested",
            "timestamp": "2026-08-14T00:00:01Z",
            "phase": "execute",
        },
        {
            "tool_call_id": "call_shell",
            "tool_name": "run_shell",
            "args_preview": {"command": "pytest -q"},
        },
    )
    _append_run_index(
        task,
        {
            "event_id": "evt_tool_done",
            "run_id": "run_1",
            "type": "tool.completed",
            "timestamp": "2026-08-14T00:00:02Z",
            "phase": "execute",
        },
        {
            "tool_call_id": "call_shell",
            "tool_name": "run_shell",
            "tool_status": "ok",
            "result_preview": "1 passed",
            "result_truncated": False,
        },
    )

    assert task.run_index[0]["args_preview"] == {"command": "pytest -q"}
    assert task.run_index[1]["result_preview"] == "1 passed"
    assert "result_truncated" not in task.run_index[1]
    assert task.run_index[1]["tool_call_id"] == "call_shell"


def test_run_index_marks_truncated_result():
    task = SimpleNamespace(run_index=[], updated_at="")
    _append_run_index(
        task,
        {
            "event_id": "evt_tool_done",
            "run_id": "run_1",
            "type": "tool.completed",
            "timestamp": "2026-08-14T00:00:02Z",
        },
        {
            "tool_call_id": "call_1",
            "tool_name": "run_shell",
            "tool_status": "ok",
            "result_preview": "big output",
            "result_truncated": True,
        },
    )

    assert task.run_index[0]["result_preview"] == "big output"
    assert task.run_index[0]["result_truncated"] is True


def test_run_index_keeps_chronology_and_public_usage_for_audit():
    task = SimpleNamespace(run_index=[], updated_at="")
    _append_run_index(
        task,
        {
            "event_id": "evt_model_done",
            "run_id": "run_1",
            "type": "model.completed",
            "timestamp": "2026-08-14T00:00:05Z",
            "started_at": "2026-08-14T00:00:01Z",
            "ended_at": "2026-08-14T00:00:05Z",
            "attempt": 2,
            "summary": "输入 120 · 输出 30",
        },
        {
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "secret": "must not pass",
            },
        },
    )

    assert task.run_index == [
        {
            "event_id": "evt_model_done",
            "run_id": "run_1",
            "type": "model.completed",
            "timestamp": "2026-08-14T00:00:05Z",
            "label": "模型完成",
            "phase": "model",
            "started_at": "2026-08-14T00:00:01Z",
            "ended_at": "2026-08-14T00:00:05Z",
            "summary": "输入 120 · 输出 30",
            "attempt": 2,
            "usage": {"input_tokens": 120, "output_tokens": 30},
        }
    ]


def test_model_completed_keeps_reply_text_and_heartbeat_skipped_in_run_index():
    task = SimpleNamespace(run_index=[], updated_at="")
    _append_run_index(
        task,
        {"event_id": "evt_model", "run_id": "run_1", "type": "model.completed", "timestamp": "t0"},
        {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "text": "本轮模型回复",
        },
    )
    _append_run_index(
        task,
        {"event_id": "evt_beat", "run_id": "run_1", "type": "model.heartbeat", "timestamp": "t1"},
        {"stage": "tool", "elapsed_seconds": 1.0},
    )

    assert len(task.run_index) == 1
    assert task.run_index[0]["type"] == "model.completed"
    assert task.run_index[0]["text"] == "本轮模型回复"
    assert task.run_index[0]["usage"]["input_tokens"] == 10


def test_model_completed_sanitizer_keeps_text():
    safe = _sanitize_event_data(
        "model.completed",
        {
            "usage": {"input_tokens": 10},
            "text": "可见回复",
            "secret": "must not pass",
        },
    )

    assert safe["text"] == "可见回复"
    assert "secret" not in safe


def test_run_index_keeps_review_battle_and_thinking_details():
    task = SimpleNamespace(run_index=[], updated_at="")
    _append_run_index(
        task,
        {"event_id": "evt_think", "run_id": "run_1", "type": "assistant.thinking", "timestamp": "t0"},
        {"text": "first think"},
    )
    _append_run_index(
        task,
        {"event_id": "evt_rev_start", "run_id": "run_1", "type": "review.started", "timestamp": "t1"},
        {"trigger": "final_before"},
    )
    _append_run_index(
        task,
        {"event_id": "evt_rev_done", "run_id": "run_1", "type": "review.completed", "timestamp": "t2"},
        {
            "status": "completed",
            "verdict": "redirect",
            "feedback": "direction wrong, verify with shell",
            "reason": "wrong_dir",
            "obstacles": ["checklist 未完成"],
            "tool_rounds": 1,
        },
    )
    _append_run_index(
        task,
        {"event_id": "evt_rebut", "run_id": "run_1", "type": "main_loop_rebuttal", "timestamp": "t3"},
        {"against_verdict": "redirect", "action": "tool:read_file", "feedback": "already verified"},
    )
    _append_run_index(
        task,
        {"event_id": "evt_skip", "run_id": "run_1", "type": "review.skipped", "timestamp": "t4"},
        {"reason": "read_only_task"},
    )

    items = task.run_index
    assert items[0]["label"] == "思考"
    assert items[0]["text"] == "first think"
    assert items[1]["trigger"] == "final_before"
    assert items[2]["verdict"] == "redirect"
    assert items[2]["feedback"] == "direction wrong, verify with shell"
    assert items[2]["obstacles"] == ["checklist 未完成"]
    assert items[2]["tool_rounds"] == 1
    assert items[3]["label"] == "主循环反驳"
    assert items[3]["action"] == "tool:read_file"
    assert items[4]["label"] == "审查跳过"
    assert items[4]["reason"] == "read_only_task"


def test_review_skipped_event_is_sanitized():
    safe = _sanitize_event_data(
        "review.skipped",
        {"reason": "read_only_task", "trigger": "final_before", "secret": "must not pass"},
    )

    assert safe == {"reason": "read_only_task"}

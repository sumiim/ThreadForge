from threadforge_api.infrastructure.worker_hub import _sanitize_event_data


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


def test_risky_tool_event_drops_arguments_and_result_preview():
    requested = _sanitize_event_data(
        "tool.requested",
        {
            "tool_call_id": "call_shell",
            "tool_name": "run_shell",
            "args_preview": {"command": "echo private"},
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

    assert "args_preview" not in requested
    assert "result_preview" not in completed


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
            "attempt": 2,
            "max_attempts": 9,
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
        "attempt": 2,
        "max_attempts": 9,
        "error_code": "model_protocol_invalid",
        "reset_stream": True,
    }
    assert heartbeat == {
        "stage": "execute",
        "elapsed_seconds": 3.4,
        "run_elapsed_seconds": 12.8,
        "round": 4,
    }

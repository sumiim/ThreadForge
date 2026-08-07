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

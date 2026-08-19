from pico.tool_executor import ToolExecutionResult

from threadforge_api.infrastructure.cancellation import CancellationToken
from threadforge_api.infrastructure.execution_boundary import ExecutionBoundary
from threadforge_api.infrastructure.run_gate import RunGate


class CapturingPublisher:
    def __init__(self):
        self.events = []

    def publish(self, task_id, run_id, event_type, data, **metadata):
        self.events.append(
            {
                "task_id": task_id,
                "run_id": run_id,
                "type": event_type,
                "data": data,
                **metadata,
            }
        )


def _boundary():
    publisher = CapturingPublisher()
    return publisher, ExecutionBoundary(
        publisher=publisher,
        task_id="task_1",
        run_id="run_1",
        gate=RunGate(),
        token=CancellationToken(),
    )


def test_read_only_tool_events_publish_allowlisted_previews():
    publisher, boundary = _boundary()

    boundary.tool_requested(
        None,
        {
            "id": "call_read",
            "name": "read_file",
            "args": {"path": "README.md", "start": 1, "end": 8, "content": "private"},
        },
    )
    boundary.before_tool(None, {"id": "call_read", "name": "read_file"})
    boundary.after_tool(None, ToolExecutionResult(content="# README.md\nhello", metadata={}))

    assert publisher.events[0]["data"] == {
        "tool_call_id": "call_read",
        "tool_name": "read_file",
        "args_preview": {"path": "README.md", "start": 1, "end": 8},
    }
    assert publisher.events[-1]["type"] == "tool.completed"
    assert publisher.events[-1]["data"]["tool_call_id"] == "call_read"
    assert publisher.events[-1]["data"]["result_preview"] == "# README.md\nhello"


def test_shell_tool_events_publish_command_and_result_preview():
    # §7.8.9 决策（2026-08-19）：审批/审计需要看到 run_shell 具体命令。
    publisher, boundary = _boundary()

    boundary.tool_requested(
        None,
        {"id": "call_shell", "name": "run_shell", "args": {"command": "echo private"}},
    )
    boundary.before_tool(None, {"id": "call_shell", "name": "run_shell"})
    boundary.after_tool(None, ToolExecutionResult(content="private output", metadata={}))

    assert publisher.events[0]["data"]["args_preview"] == {"command": "echo private"}
    assert publisher.events[-1]["data"]["result_preview"] == "private output"


def test_unified_event_contract_lifts_phase_parent_and_interval():
    publisher, boundary = _boundary()

    boundary.before_model(None)
    boundary.after_model(None, {"input_tokens": 10, "output_tokens": 3})
    boundary.tool_requested(None, {"id": "call_read", "name": "read_file", "args": {"path": "README.md"}})
    boundary.before_tool(None, {"id": "call_read", "name": "read_file"})
    boundary.after_tool(None, ToolExecutionResult(content="ok", metadata={"tool_status": "ok"}))

    model_started = publisher.events[0]
    assert model_started["type"] == "model.started"
    assert model_started["phase"] == "model"
    assert model_started["attempt"] == 1
    assert model_started["started_at"]

    model_completed = publisher.events[1]
    assert model_completed["phase"] == "model"
    assert model_completed["status"] == "completed"
    assert model_completed["ended_at"]

    tool_requested = publisher.events[2]
    assert tool_requested["phase"] == "execute"
    assert tool_requested["parent_event_id"].startswith("model_round_")

    tool_completed = publisher.events[-1]
    assert tool_completed["type"] == "tool.completed"
    assert tool_completed["phase"] == "execute"
    assert tool_completed["status"] == "ok"
    assert tool_completed["parent_event_id"].startswith("model_round_")
    assert tool_completed["ended_at"]

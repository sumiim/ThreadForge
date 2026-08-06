from pico.tool_executor import ToolExecutionResult

from threadforge_api.infrastructure.cancellation import CancellationToken
from threadforge_api.infrastructure.execution_boundary import ExecutionBoundary
from threadforge_api.infrastructure.run_gate import RunGate


class CapturingPublisher:
    def __init__(self):
        self.events = []

    def publish(self, task_id, run_id, event_type, data):
        self.events.append(
            {
                "task_id": task_id,
                "run_id": run_id,
                "type": event_type,
                "data": data,
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


def test_risky_tool_events_do_not_publish_arguments_or_results():
    publisher, boundary = _boundary()

    boundary.tool_requested(
        None,
        {"id": "call_shell", "name": "run_shell", "args": {"command": "echo private"}},
    )
    boundary.before_tool(None, {"id": "call_shell", "name": "run_shell"})
    boundary.after_tool(None, ToolExecutionResult(content="private output", metadata={}))

    assert "args_preview" not in publisher.events[0]["data"]
    assert "result_preview" not in publisher.events[-1]["data"]

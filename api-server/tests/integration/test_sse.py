"""SSE contract and session-refresh integration."""

from __future__ import annotations

import json
import threading

from ..conftest import wait_for_terminal


def _collect_sse(client, url):
    events = []

    def reader():
        with client.stream("GET", url) as response:
            current = {}
            for line in response.iter_lines():
                if line.startswith("event:"):
                    current["event"] = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    current["data"] = json.loads(line[len("data:") :].strip())
                    events.append(dict(current))
                    current = {}

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    return events, thread


def test_sse_snapshot_first_then_lifecycle(client, session_id, model_outputs):
    model_outputs[:] = ["<final>streamed answer</final>"]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
    tid = task["task_id"]
    events, thread = _collect_sse(client, f"/api/v1/tasks/{tid}/events")
    wait_for_terminal(client, tid)
    thread.join(timeout=5)

    assert events, "expected at least one SSE event"
    assert events[0]["event"] == "task.snapshot"
    types = [event["event"] for event in events]
    assert "model.started" in types
    assert "model.completed" in types
    assert "message.completed" in types
    assert "task.completed" in types
    # terminal is the last event
    assert types[-1] == "task.completed"


def test_sse_terminal_ends_stream(client, session_id, model_outputs):
    model_outputs[:] = ["<final>end</final>"]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
    tid = task["task_id"]
    events, thread = _collect_sse(client, f"/api/v1/tasks/{tid}/events")
    wait_for_terminal(client, tid)
    thread.join(timeout=5)
    assert events[-1]["event"] == "task.completed"


def test_sse_tool_events_identify_exact_call(client, session_id, model_outputs):
    """Tool terminal events identify the exact call, not only its tool name."""
    model_outputs[:] = [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>',
        "<final>done</final>",
    ]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "read README"}).json()
    tid = task["task_id"]
    events = []
    with client.stream("GET", f"/api/v1/tasks/{tid}/events") as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
                events.append(data)
    for event in events:
        if event["type"] in ("tool.completed", "tool.failed"):
            assert event["data"].get("tool_name"), f"tool event without name: {event}"
            assert event["data"].get("tool_call_id"), f"tool event without call id: {event}"


def test_session_refresh_shows_messages_after_run(client, session_id, model_outputs):
    model_outputs[:] = ["<final>persisted answer</final>"]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "remember me"}).json()
    wait_for_terminal(client, task["task_id"])
    detail = client.get(f"/api/v1/sessions/{session_id}?message_limit=100").json()
    assert detail["message_total"] >= 1
    contents = " ".join(message["content"] for message in detail["messages"])
    assert "remember me" in contents
    assert "persisted answer" in contents

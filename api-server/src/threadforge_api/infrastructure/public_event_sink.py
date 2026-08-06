"""pico EventSink that forwards only the public events not already emitted by
the ExecutionBoundary / TaskRunner (per the execution doc §10.5)."""

from __future__ import annotations

from pico.event_sink import EventSink


class PublicEventSink(EventSink):
    def __init__(self, publisher, task_id: str, run_id: str, gate, token):
        self._publisher = publisher
        self._task_id = task_id
        self._run_id = run_id
        self._gate = gate
        self._token = token

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        if event_type == "agent_state_changed":
            # State projections are intentionally allow-listed and contain no
            # prompt, file contents, tool arguments, or model output.
            with self._gate:
                if self._gate.closed or self._token.is_cancelled():
                    return payload
                self._publisher.publish(
                    self._task_id,
                    self._run_id,
                    "agent.state",
                    {
                        "phase": str(payload.get("phase", ""))[:64],
                        "next_step": str(payload.get("next_step", ""))[:300],
                        "checklist": [str(item)[:300] for item in payload.get("checklist", [])[:20]],
                        "done_when": [str(item)[:300] for item in payload.get("done_when", [])[:20]],
                        "completed_items": [str(item)[:300] for item in payload.get("completed_items", [])[:20]],
                        "tool_steps": max(0, int(payload.get("tool_steps", 0))),
                        "read_files": max(0, int(payload.get("read_files", 0))),
                        "max_tool_steps": max(0, int(payload.get("max_tool_steps", 0))),
                        "max_read_files": max(0, int(payload.get("max_read_files", 0))),
                        "max_total_steps": max(0, int(payload.get("max_total_steps", 0))),
                        "reason": str(payload.get("reason", ""))[:100],
                    },
                )
            return payload
        if event_type == "sandbox_violation":
            with self._gate:
                if self._gate.closed or self._token.is_cancelled():
                    return payload
                self._publisher.publish(
                    self._task_id,
                    self._run_id,
                    "policy.violation",
                    {
                        "tool": payload.get("tool", ""),
                        "tool_error_code": payload.get("tool_error_code", ""),
                        "security_event_type": payload.get("security_event_type", ""),
                    },
                )
        # model.started/model.completed/tool.* are published by ExecutionBoundary;
        # task lifecycle by TaskRunner. Nothing else leaks legacy names.
        return payload

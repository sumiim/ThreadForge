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

"""ExecutionHooks implementation: cancel check + public events in one gate."""

from __future__ import annotations

from typing import Any

from pico.execution_hooks import RunCancelled

from .run_gate import RunGate


class ExecutionBoundary:
    """Web backend hooks. Every check-and-publish happens under ``run.gate`` so
    a persisted cancellation can never be followed by a new model/tool event."""

    def __init__(self, *, publisher, task_id: str, run_id: str, gate: RunGate, token):
        self._publisher = publisher
        self._task_id = task_id
        self._run_id = run_id
        self._gate = gate
        self._token = token

    @property
    def gate(self) -> RunGate:
        return self._gate

    def _check(self):
        if self._gate.closed or self._token.is_cancelled():
            raise RunCancelled()

    def before_model(self, task_state) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(self._task_id, self._run_id, "model.started", {})

    def after_model(self, task_state, metadata: dict) -> None:
        with self._gate:
            self._check()
            summary = {key: value for key, value in (metadata or {}).items() if key in {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}}
            self._publisher.publish(self._task_id, self._run_id, "model.completed", {"usage": summary})

    def tool_requested(self, task_state, tool_call: dict) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "tool.requested",
                {"tool_call_id": tool_call.get("id", ""), "tool_name": tool_call.get("name", "")},
            )

    def before_tool(self, task_state, tool_call: dict) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "tool.started",
                {"tool_call_id": tool_call.get("id", ""), "tool_name": tool_call.get("name", "")},
            )

    def after_tool(self, task_state, result: Any) -> None:
        metadata = dict(getattr(result, "metadata", {}) or {})
        tool_status = metadata.get("tool_status", "ok")
        event_type = "tool.completed" if tool_status in {"ok", "partial_success"} else "tool.failed"
        # tool_name is NOT in ToolExecutionResult.metadata — capture it from the
        # last tool recorded in task_state so SSE consumers know *which* tool.
        tool_name = getattr(task_state, "last_tool", "") or ""
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                event_type,
                {
                    "tool_name": tool_name,
                    "tool_status": tool_status,
                    "tool_error_code": metadata.get("tool_error_code", ""),
                    "affected_paths": metadata.get("affected_paths", []),
                },
            )

"""ExecutionHooks implementation: cancel check + public events in one gate."""

from __future__ import annotations

from typing import Any

from pico.execution_hooks import RunCancelled
from pico.security import public_tool_args_preview, public_tool_result_preview

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
        self._active_tool_call_id = ""
        self._active_tool_name = ""

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
            tool_name = str(tool_call.get("name", ""))
            payload = {
                "tool_call_id": tool_call.get("id", ""),
                "tool_name": tool_name,
            }
            args_preview = public_tool_args_preview(tool_name, tool_call.get("args", {}))
            if args_preview:
                payload["args_preview"] = args_preview
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "tool.requested",
                payload,
            )

    def before_tool(self, task_state, tool_call: dict) -> None:
        with self._gate:
            self._check()
            self._active_tool_call_id = str(tool_call.get("id", ""))
            self._active_tool_name = str(tool_call.get("name", ""))
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
        # ToolExecutionResult does not carry call identity, so retain the call
        # accepted by before_tool until its matching result is published.
        tool_name = self._active_tool_name or getattr(task_state, "last_tool", "") or ""
        tool_call_id = self._active_tool_call_id
        result_preview, result_truncated = public_tool_result_preview(
            tool_name, getattr(result, "content", "")
        )
        payload = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_status": tool_status,
            "tool_error_code": metadata.get("tool_error_code", ""),
            "affected_paths": metadata.get("affected_paths", []),
        }
        if result_preview:
            payload["result_preview"] = result_preview
            payload["result_truncated"] = result_truncated
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                event_type,
                payload,
            )
            self._active_tool_call_id = ""
            self._active_tool_name = ""

    def commentary(self, task_state, text: str) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "assistant.commentary",
                {"text": str(text)[:1000]},
            )

    def model_retrying(self, task_state, stage: str, details: dict) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "model.retrying",
                {"stage": str(stage), **dict(details or {})},
            )

    def model_protocol_retrying(self, task_state, stage: str, details: dict) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "model.protocol_retrying",
                {
                    "stage": str(stage),
                    "attempt": int(details.get("attempt", 1)),
                    "max_attempts": int(details.get("max_attempts", 1)),
                    "error_code": "model_protocol_invalid",
                    "reset_stream": True,
                },
            )

    def model_text_delta(self, task_state, stage: str, text: str) -> None:
        # Native server execution keeps protocol output private. Local Worker
        # execution applies the final-answer projector before publishing deltas.
        return None

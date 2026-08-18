"""ExecutionHooks implementation: cancel check + public events in one gate."""

from __future__ import annotations

from typing import Any

from pico.execution_hooks import RunCancelled
from pico.security import public_tool_args_preview, public_tool_result_preview

from ..domain.entities import utc_now
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
        # §7.8.9 并发批：并发只读工具可能同时 before/after_tool，单槽属性无法
        # 把结果归属到正确工具 → 改为按 tool_call_id 分槽。
        self._active_tools: dict[str, dict] = {}
        self._model_round = 0
        self._model_round_id = ""
        self._model_started_wall = ""

    @property
    def gate(self) -> RunGate:
        return self._gate

    def _check(self):
        if self._gate.closed or self._token.is_cancelled():
            raise RunCancelled()

    def before_model(self, task_state) -> None:
        with self._gate:
            self._check()
            self._model_round += 1
            self._model_round_id = f"model_round_{self._model_round}"
            self._model_started_wall = utc_now()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "model.started",
                {
                    "round": self._model_round,
                    "round_id": self._model_round_id,
                    "started_at": self._model_started_wall,
                },
                phase="model",
                attempt=self._model_round,
                started_at=self._model_started_wall,
                summary=f"模型第 {self._model_round} 轮请求",
            )

    def after_model(self, task_state, metadata: dict) -> None:
        with self._gate:
            self._check()
            summary = {key: value for key, value in (metadata or {}).items() if key in {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}}
            ended_at = utc_now()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "model.completed",
                {
                    "usage": summary,
                    "round_id": self._model_round_id,
                    "started_at": self._model_started_wall,
                    "ended_at": ended_at,
                },
                phase="model",
                attempt=self._model_round,
                started_at=self._model_started_wall,
                ended_at=ended_at,
                status="completed",
                summary=_usage_summary(summary),
            )

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
                phase="execute",
                parent_event_id=self._model_round_id,
            )

    def before_tool(self, task_state, tool_call: dict) -> None:
        with self._gate:
            self._check()
            tool_call_id = str(tool_call.get("id", ""))
            started_wall = utc_now()
            # 按 tool_call_id 分槽：after_tool 靠 metadata.tool_call_id 取回
            # 自己的槽，并发批里归属不串位。
            self._active_tools[tool_call_id] = {
                "tool_call_id": tool_call_id,
                "tool_name": str(tool_call.get("name", "")),
                "started_wall": started_wall,
            }
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "tool.started",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": str(tool_call.get("name", "")),
                    "parent_event_id": self._model_round_id,
                    "started_at": started_wall,
                },
                phase="execute",
                parent_event_id=self._model_round_id,
                started_at=started_wall,
                status="running",
            )

    def after_tool(self, task_state, result: Any) -> None:
        metadata = dict(getattr(result, "metadata", {}) or {})
        tool_status = metadata.get("tool_status", "ok")
        event_type = "tool.completed" if tool_status in {"ok", "partial_success"} else "tool.failed"
        # ToolExecutionResult 的 metadata 带 tool_call_id（tool_executor 写入）；
        # 取回 before_tool 登记的槽，归属正确工具。兼容旧路径（无 id 时取最近活跃槽）。
        tool_call_id = str(metadata.get("tool_call_id", ""))
        if not tool_call_id and self._active_tools:
            tool_call_id = next(iter(self._active_tools))
        slot = self._active_tools.pop(tool_call_id, None) or {}
        tool_name = slot.get("tool_name") or getattr(task_state, "last_tool", "") or ""
        started_wall = slot.get("started_wall", "")
        result_preview, result_truncated = public_tool_result_preview(
            tool_name, getattr(result, "content", "")
        )
        ended_at = utc_now()
        payload = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_status": tool_status,
            "tool_error_code": metadata.get("tool_error_code", ""),
            "affected_paths": metadata.get("affected_paths", []),
            "parent_event_id": self._model_round_id,
            "started_at": started_wall,
            "ended_at": ended_at,
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
                phase="execute",
                parent_event_id=self._model_round_id,
                started_at=started_wall,
                ended_at=ended_at,
                status=tool_status,
                summary=tool_name,
            )

    def commentary(self, task_state, text: str) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "assistant.commentary",
                {"text": str(text)[:1000]},
                phase="talk",
                summary=str(text)[:1000],
            )

    def model_retrying(self, task_state, stage: str, details: dict) -> None:
        with self._gate:
            self._check()
            self._publisher.publish(
                self._task_id,
                self._run_id,
                "model.retrying",
                {"stage": str(stage), **dict(details or {})},
                phase="model",
                attempt=int(details.get("attempt", 1)),
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
                    "response_chars": max(0, int(details.get("response_chars", 0))),
                    "detected_format": str(details.get("detected_format", ""))[:32],
                    "top_level_keys": [
                        str(key)[:64] for key in details.get("top_level_keys", [])[:20]
                    ],
                    "response_hash": str(details.get("response_hash", ""))[:64],
                    "reset_stream": True,
                },
                phase="model",
                attempt=int(details.get("attempt", 1)),
            )

    def model_text_delta(self, task_state, stage: str, text: str) -> None:
        # Native server execution keeps protocol output private. Local Worker
        # execution applies the final-answer projector before publishing deltas.
        return None


def _usage_summary(usage: dict) -> str:
    if not usage:
        return ""
    parts = []
    for key, label in (
        ("input_tokens", "输入"),
        ("output_tokens", "输出"),
        ("cached_tokens", "缓存"),
    ):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            parts.append(f"{label} {value}")
    return "，".join(parts) if parts else ""


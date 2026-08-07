"""Cancellation token, RunCancelled and pluggable linearization hooks.

These are runtime-agnostic minimal contracts. The CLI keeps using the no-op
implementations; the Web backend injects an `ExecutionBoundary` that checks
cancellation and publishes public SSE events inside a per-run gate.
"""

from __future__ import annotations

from typing import Any, Protocol


class RunCancelled(RuntimeError):
    """Raised when a cancellation is observed at a step boundary."""


class ProcessCleanupFailed(RuntimeError):
    """Raised when a managed child process cannot be proven terminated."""


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class NeverCancelledToken:
    """Default token used when no cancellation is injected."""

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class ExecutionHooks(Protocol):
    def before_model(self, task_state) -> None: ...

    def after_model(self, task_state, metadata: dict) -> None: ...

    def tool_requested(self, task_state, tool_call: dict) -> None: ...

    def before_tool(self, task_state, tool_call: dict) -> None: ...

    def after_tool(self, task_state, result: Any) -> None: ...

    def commentary(self, task_state, text: str) -> None: ...


class NoopExecutionHooks:
    """Default hooks; CLI behavior is unchanged."""

    def before_model(self, task_state) -> None:
        return None

    def after_model(self, task_state, metadata: dict) -> None:
        return None

    def tool_requested(self, task_state, tool_call: dict) -> None:
        return None

    def before_tool(self, task_state, tool_call: dict) -> None:
        return None

    def after_tool(self, task_state, result: Any) -> None:
        return None

    def commentary(self, task_state, text: str) -> None:
        return None

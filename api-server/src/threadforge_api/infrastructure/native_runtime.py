"""NativeRuntimeAdapter: builds a fresh Pico per Run with Web-scoped contracts."""

from __future__ import annotations

import time

from pico import Pico
from pico.event_sink import CompositeSink, EventCollector, JsonlSink
from pico.run_lifecycle import finalize_failed_run
from pico.task_state import STATUS_RUNNING

from .public_event_sink import PublicEventSink

WEB_V1_ALLOWED_TOOLS = (
    "list_files",
    "read_file",
    "search",
    "run_shell",
    "write_file",
    "patch_file",
)


class NativeRuntimeAdapter:
    """One adapter per Run; never shared across concurrent requests."""

    def __init__(
        self,
        *,
        settings,
        workspace_entry,
        session_data: dict,
        session_store,
        fenced_run_store,
        model_client,
        approval_strategy,
        token,
        execution_hooks,
        publisher,
        task_id: str,
        run_id: str,
        max_steps: int,
    ):
        self._settings = settings
        self._workspace_entry = workspace_entry
        self._session_data = session_data
        self._session_store = session_store
        self._fenced_run_store = fenced_run_store
        self._model_client = model_client
        self._approval_strategy = approval_strategy
        self._token = token
        self._execution_hooks = execution_hooks
        self._publisher = publisher
        self._task_id = task_id
        self._run_id = run_id
        self._max_steps = max_steps
        self._pico: Pico | None = None

    def run(self, user_message: str):
        from pico.workspace import WorkspaceContext as _W

        workspace = _W.build(str(self._workspace_entry.canonical_path))
        event_sink = CompositeSink(
            EventCollector(),
            JsonlSink(self._fenced_run_store),
            PublicEventSink(
                self._publisher,
                self._task_id,
                self._run_id,
                self._execution_hooks.gate,
                self._token,
            ),
        )
        self._pico = Pico(
            model_client=self._model_client,
            workspace=workspace,
            session_store=self._session_store,
            run_store=self._fenced_run_store,
            session=dict(self._session_data),
            approval_strategy=self._approval_strategy,
            cancellation_token=self._token,
            execution_hooks=self._execution_hooks,
            allowed_tools=WEB_V1_ALLOWED_TOOLS,
            max_steps=self._max_steps,
            max_new_tokens=self._settings.max_new_tokens,
            event_sink=event_sink,
            shell_output_max_bytes=self._settings.shell_output_max_bytes,
            shell_cleanup_grace_seconds=self._settings.shell_cleanup_grace_seconds,
            allow_durable_memory_write=False,  # Web path must not write Workspace .pico/memory/
        )
        started = time.monotonic()
        try:
            self._pico.ask(user_message, task_id=self._task_id, run_id=self._run_id)
        except Exception as exc:
            if self._pico.current_task_state is not None and self._pico.current_task_state.status == STATUS_RUNNING:
                finalize_failed_run(
                    self._pico,
                    self._pico.current_task_state,
                    error_type=type(exc).__name__,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            raise
        return self._pico.current_task_state

    def terminate_shell(self) -> bool:
        if self._pico is not None:
            return bool(self._pico.tool_context().terminate_active_shell(self._settings.shell_cleanup_grace_seconds))
        return True

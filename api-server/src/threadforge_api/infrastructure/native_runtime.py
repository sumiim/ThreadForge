"""NativeRuntimeAdapter: builds a fresh Pico per Run with Web-scoped contracts."""

from __future__ import annotations

import time
import urllib.error

from langgraph_pico import run_native
from pico import Pico
from pico.event_sink import CompositeSink, EventCollector, JsonlSink
from pico.providers.clients import ModelProviderError
from pico.run_lifecycle import finalize_failed_run
from pico.task_state import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_RUNTIME_ERROR,
)

from .public_event_sink import PublicEventSink

WEB_V1_ALLOWED_TOOLS = (
    "delegate",
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
        isolation=None,
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
        self._isolation = isolation
        self._pico: Pico | None = None
        self._shell_factory = self._build_sandbox_shell_factory()

    def _build_sandbox_shell_factory(self):
        """Fail-closed: return a sandbox shell factory or None (legacy CLI path).

        When the sandbox is enabled this returns a Docker-backed factory and
        raises ``SandboxError`` if Docker or the safety config is unavailable,
        so the Run fails closed before any shell executes. When the sandbox is
        disabled (CLI/评测兼容 path) this returns None and the legacy host
        ``ShellProcess`` remains in use.
        """
        if not getattr(self._settings, "sandbox_enabled", False):
            return None
        from threadforge_sandbox import (
            DockerSandboxBackend,
            SandboxConfig,
            SandboxLifecycle,
        )

        config = SandboxConfig(
            image=self._settings.sandbox_image,
            user=self._settings.sandbox_user,
            network=self._settings.sandbox_network,
            cpu_limit=self._settings.sandbox_cpu_limit,
            memory_limit=self._settings.sandbox_memory_limit,
            pids_limit=self._settings.sandbox_pids_limit,
        )
        lifecycle = SandboxLifecycle(on_event=self._sandbox_event)
        backend = DockerSandboxBackend(config, lifecycle=lifecycle)
        return backend.make_shell

    def _sandbox_event(self, kind: str, payload: dict) -> None:
        self._publisher.publish(
            self._task_id,
            self._run_id,
            kind,
            dict(payload or {}),
            phase="execute",
            status="running" if kind == "sandbox.started" else "completed",
            summary=str(payload.get("reason", "") or payload.get("container", ""))[:100],
        )

    def run(self, user_message: str):
        from pico.workspace import WorkspaceContext as _W

        handle = (
            self._isolation.prepare(
                task_id=self._task_id,
                run_id=self._run_id,
                workspace_id=self._workspace_entry.workspace_id,
                owner_id=self._session_data.get("owner_id", ""),
            )
            if self._isolation is not None
            else None
        )
        run_root = handle.root if handle is not None else self._workspace_entry.canonical_path
        workspace = _W.build(str(run_root))
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
            shell_factory=self._shell_factory,
            # §7.8.9 阶段 3：真实模型开启 review subagent（程序强制）。
            # 离线 FakeModelClient 没有独立 review 输出流，必须显式关闭，
            # 否则会消费脚本化主循环输出并把任务误判为 model_error。
            feature_flags={
                "review_subagent": bool(
                    getattr(self._model_client, "supports_review_subagent", True)
                )
            },
        )
        started = time.monotonic()
        try:
            try:
                run_native(
                    self._pico,
                    user_message,
                    task_mode="auto",
                    task_id=self._task_id,
                    run_id=self._run_id,
                )
            except Exception as exc:
                if self._pico.current_task_state is not None and self._pico.current_task_state.status == STATUS_RUNNING:
                    error_type, stop_reason = _classify_public_error(exc)
                    finalize_failed_run(
                        self._pico,
                        self._pico.current_task_state,
                        error_type=error_type,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        stop_reason=stop_reason,
                    )
                raise
        finally:
            if handle is not None:
                state = getattr(self._pico, "current_task_state", None)
                apply = getattr(state, "status", "") == STATUS_COMPLETED
                result = self._isolation.finalize(handle, apply=apply)
                if result.get("applied") or result.get("conflicts"):
                    self._publisher.publish(
                        self._task_id,
                        self._run_id,
                        "workspace.applied" if result.get("applied") else "workspace.conflict",
                        {
                            "workspace_id": handle.workspace_id,
                            "changed_paths": result.get("changed_paths", []),
                            "conflicts": result.get("conflicts", []),
                        },
                        phase="execute",
                        status="completed" if result.get("applied") else "conflict",
                        summary=f"{len(result.get('changed_paths', []))} paths changed",
                    )
        return self._pico.current_task_state

    def terminate_shell(self) -> bool:
        if self._pico is not None:
            return bool(self._pico.tool_context().terminate_active_shell(self._settings.shell_cleanup_grace_seconds))
        return True


_MODEL_PROVIDER_ERROR_CODES = frozenset(
    {
        "model_rate_limited",
        "model_timeout",
        "model_server_error",
        "model_auth_error",
        "model_request_rejected",
        "model_connection_error",
        "model_response_invalid",
        "model_provider_error",
    }
)


def _classify_public_error(exc: Exception) -> tuple[str, str]:
    """Expose actionable provider failures without persisting response bodies."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ModelProviderError) and current.code in _MODEL_PROVIDER_ERROR_CODES:
            return current.code, STOP_REASON_MODEL_ERROR
        if isinstance(current, urllib.error.HTTPError):
            return f"model_http_{current.code}", STOP_REASON_MODEL_ERROR
        if isinstance(current, (urllib.error.URLError, ConnectionError, TimeoutError)):
            return "model_connection_error", STOP_REASON_MODEL_ERROR
        current = current.__cause__

    message = str(exc).lower()
    if "openai-compatible" in message:
        if "non-json" in message or "could not extract text" in message:
            return "model_invalid_response", STOP_REASON_MODEL_ERROR
        return "model_provider_error", STOP_REASON_MODEL_ERROR
    return type(exc).__name__, STOP_REASON_RUNTIME_ERROR

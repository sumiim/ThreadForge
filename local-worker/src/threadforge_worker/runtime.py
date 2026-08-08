"""One-task local Pico runtime with remote approval and public event callbacks."""

from __future__ import annotations

import hashlib
import json
import queue
import secrets
import threading
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from langgraph_pico import run_agent
from pico import Pico
from pico.approval import ApprovalOutcome, ApprovalRequest, ApprovalStrategy
from pico.event_sink import CompositeSink, EventCollector, EventSink, JsonlSink
from pico.execution_hooks import RunCancelled
from pico.providers.clients import OpenAICompatibleModelClient
from pico.run_lifecycle import finalize_failed_run
from pico.run_store import RunStore
from pico.security import (
    public_tool_args_preview,
    public_tool_result_preview,
    redact_artifact,
)
from pico.session_store import SessionStore
from pico.task_state import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_RUNTIME_ERROR,
    STOP_REASON_USER_CANCELLED,
)
from pico.workspace import WorkspaceContext

ALLOWED_TOOLS = (
    "delegate",
    "list_files",
    "read_file",
    "search",
    "run_shell",
    "write_file",
    "patch_file",
)


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise RunCancelled()


class CancellableModelClient:
    """Make a blocking provider call observable by the Worker cancellation token."""

    def __init__(self, delegate, token: CancellationToken, poll_interval: float = 0.05):
        self._delegate = delegate
        self._token = token
        self._poll_interval = max(0.01, float(poll_interval))

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def complete(self, prompt, max_new_tokens, **kwargs):
        self._token.raise_if_cancelled()
        outcome = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcome.put((True, self._delegate.complete(prompt, max_new_tokens, **kwargs)))
            except BaseException as exc:
                outcome.put((False, exc))

        threading.Thread(
            target=invoke,
            name="worker-model-request",
            daemon=True,
        ).start()
        while True:
            try:
                succeeded, value = outcome.get(timeout=self._poll_interval)
                break
            except queue.Empty:
                self._token.raise_if_cancelled()
        self._token.raise_if_cancelled()
        if succeeded:
            return value
        raise value


class RemoteApprovalStrategy(ApprovalStrategy):
    def __init__(self, send: Callable[[dict], None], task_id: str, token: CancellationToken):
        self._send = send
        self._task_id = task_id
        self._token = token
        self._condition = threading.Condition()
        self._decisions: dict[str, str] = {}
        self._pending_digests: dict[str, str] = {}

    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        args_digest = _args_digest(request.args)
        with self._condition:
            self._pending_digests[request.tool_call_id] = args_digest
        self._send(
            {
                "type": "approval.requested",
                "task_id": self._task_id,
                "tool_call_id": request.tool_call_id,
                "tool_name": request.name,
                "args": request.args,
                "args_digest": args_digest,
            }
        )
        with self._condition:
            while request.tool_call_id not in self._decisions:
                if self._token.is_cancelled():
                    raise RunCancelled()
                self._condition.wait(timeout=0.25)
            decision = self._decisions.pop(request.tool_call_id)
            self._pending_digests.pop(request.tool_call_id, None)
        return ApprovalOutcome.APPROVED if decision == "approved" else ApprovalOutcome.REJECTED

    def resolve(self, tool_call_id: str, decision: str, args_digest: str) -> None:
        with self._condition:
            expected = self._pending_digests.get(tool_call_id)
            if expected is None or not secrets.compare_digest(expected, args_digest):
                raise RuntimeError("approval decision does not match the pending tool arguments")
            self._decisions[tool_call_id] = decision
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()


class RemoteExecutionHooks:
    def __init__(self, send_event: Callable[[str, dict], None], token: CancellationToken):
        self._send = send_event
        self._token = token
        self._active_tool_call_id = ""
        self._active_tool_name = ""

    def _check(self) -> None:
        if self._token.is_cancelled():
            raise RunCancelled()

    def before_model(self, task_state) -> None:
        self._check()
        self._send("model.started", {})

    def after_model(self, task_state, metadata: dict) -> None:
        self._check()
        usage = {
            key: value
            for key, value in (metadata or {}).items()
            if key in {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
        }
        self._send("model.completed", {"usage": usage})

    def tool_requested(self, task_state, tool_call: dict) -> None:
        self._check()
        tool_name = str(tool_call.get("name", ""))
        payload = {
            "tool_call_id": tool_call.get("id", ""),
            "tool_name": tool_name,
        }
        args_preview = public_tool_args_preview(tool_name, tool_call.get("args", {}))
        if args_preview:
            payload["args_preview"] = args_preview
        self._send(
            "tool.requested",
            payload,
        )

    def before_tool(self, task_state, tool_call: dict) -> None:
        self._check()
        self._active_tool_call_id = str(tool_call.get("id", ""))
        self._active_tool_name = str(tool_call.get("name", ""))
        self._send(
            "tool.started",
            {"tool_call_id": self._active_tool_call_id, "tool_name": self._active_tool_name},
        )

    def after_tool(self, task_state, result) -> None:
        self._check()
        metadata = dict(getattr(result, "metadata", {}) or {})
        status = metadata.get("tool_status", "ok")
        event_type = "tool.completed" if status in {"ok", "partial_success"} else "tool.failed"
        result_preview, result_truncated = public_tool_result_preview(
            self._active_tool_name, getattr(result, "content", "")
        )
        payload = {
            "tool_call_id": self._active_tool_call_id,
            "tool_name": self._active_tool_name,
            "tool_status": status,
            "tool_error_code": metadata.get("tool_error_code", ""),
            "affected_paths": metadata.get("affected_paths", []),
        }
        if result_preview:
            payload["result_preview"] = result_preview
            payload["result_truncated"] = result_truncated
        self._send(
            event_type,
            payload,
        )
        self._active_tool_call_id = ""
        self._active_tool_name = ""

    def commentary(self, task_state, text: str) -> None:
        self._check()
        self._send("assistant.commentary", {"text": str(text)[:1000]})


class RemoteAgentStateSink(EventSink):
    """Forward the bounded Agent state projection to the control plane."""

    def __init__(self, send_event: Callable[[str, dict], None]):
        self._send_event = send_event

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        if event_type == "agent_state_changed":
            self._send_event(
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
        public_type = {
            "plan_created": "plan.created",
            "plan_skipped": "plan.skipped",
            "review_started": "review.started",
            "review_completed": "review.completed",
        }.get(event_type)
        if public_type:
            self._send_event(public_type, dict(payload))
        return payload


@dataclass
class ActiveRun:
    task_id: str
    token: CancellationToken
    approval: RemoteApprovalStrategy
    thread: threading.Thread | None = None
    pico: Pico | None = None
    session_id: str = ""
    workspace_id: str = ""

    def cancel(self, cleanup_grace: float) -> None:
        self.token.cancel()
        self.approval.wake()
        if self.pico is not None:
            self.pico.tool_context().terminate_active_shell(cleanup_grace)


def run_task(
    *,
    task: dict,
    workspace_path: Path,
    data_dir: Path,
    send: Callable[[dict], None],
    active: ActiveRun,
    model_client_factory: Callable[[], object] | None = None,
) -> None:
    settings = task.get("settings", {})
    incoming_session = dict(task["session"])
    session_store = SessionStore(data_dir / "sessions")
    session_id = str(incoming_session["id"])
    if session_store.exists(session_id):
        session = session_store.load(session_id)
        if session.get("workspace_id") != task.get("workspace_id"):
            raise RuntimeError("local session workspace does not match the task")
        # Ownership belongs to the currently paired control plane. History and
        # memory remain local when a user switches API servers.
        for key in (
            "owner_id",
            "title",
            "display_name_source",
            "display_name_updated_at",
            "first_request_at",
            "device_id",
            "execution_environment",
        ):
            if key in incoming_session:
                session[key] = incoming_session[key]
    else:
        session = incoming_session
    session["workspace_root"] = str(workspace_path)
    run_store = RunStore(data_dir / "runs")
    import os

    configured_model = os.environ.get("PICO_OPENAI_MODEL", "gpt-5.4").strip() or "gpt-5.4"
    requested_model = str(settings.get("model_id", configured_model)).strip() or configured_model
    if requested_model != configured_model:
        raise RuntimeError("requested model is not configured on the local Worker")
    supported_efforts = _supported_reasoning_efforts()
    requested_effort = str(settings.get("reasoning_effort", "none")).strip().lower() or "none"
    if requested_effort not in supported_efforts:
        raise RuntimeError("requested reasoning effort is not supported by the local Worker")
    provider_model_client = (
        model_client_factory()
        if model_client_factory is not None
        else OpenAICompatibleModelClient(
            model=requested_model,
            base_url=_required_env("PICO_OPENAI_API_BASE", "https://api.openai.com/v1"),
            api_key=_required_env("PICO_OPENAI_API_KEY"),
            temperature=0.2,
            timeout=int(settings.get("model_timeout_seconds", 120)),
            max_attempts=max(1, min(5, int(settings.get("model_max_attempts", 3)))),
            reasoning_effort=requested_effort,
            supported_reasoning_efforts=supported_efforts,
        )
    )
    model_client = CancellableModelClient(provider_model_client, active.token)
    def send_runtime_event(event_type: str, data: dict) -> None:
        send(
            {
                "type": "event",
                "task_id": task["task_id"],
                "event_type": event_type,
                "data": redact_artifact(data),
            }
        )

    hooks = RemoteExecutionHooks(send_runtime_event, active.token)
    pico = Pico(
        model_client=model_client,
        workspace=WorkspaceContext.build(workspace_path),
        session_store=session_store,
        run_store=run_store,
        session=session,
        approval_strategy=active.approval,
        cancellation_token=active.token,
        execution_hooks=hooks,
        allowed_tools=ALLOWED_TOOLS,
        max_steps=int(task.get("max_steps", 6)),
        max_new_tokens=int(settings.get("max_new_tokens", 512)),
        event_sink=CompositeSink(EventCollector(), JsonlSink(run_store), RemoteAgentStateSink(send_runtime_event)),
        shell_output_max_bytes=int(settings.get("shell_output_max_bytes", 1048576)),
        shell_cleanup_grace_seconds=float(settings.get("shell_cleanup_grace_seconds", 5)),
        max_read_files=int(settings.get("max_read_files", 4)),
        max_total_steps=int(settings.get("max_total_steps", max(int(task.get("max_steps", 6)) * 3, int(task.get("max_steps", 6)) + 4))),
        allow_durable_memory_write=False,
    )
    active.pico = pico
    started = time.monotonic()
    try:
        run_agent(
            pico,
            task["input"],
            task_mode="auto",
            enable_planning=True,
            task_id=task["task_id"],
            run_id=task["run_id"],
            workspace_id=task.get("workspace_id", ""),
        )
    except Exception as exc:
        if pico.current_task_state is not None and pico.current_task_state.status == STATUS_RUNNING:
            error_type, stop_reason = _classify_error(exc)
            finalize_failed_run(
                pico,
                pico.current_task_state,
                error_type=error_type,
                duration_ms=int((time.monotonic() - started) * 1000),
                stop_reason=stop_reason,
            )
    state = pico.current_task_state
    stop_reason = getattr(state, "stop_reason", "") or STOP_REASON_RUNTIME_ERROR
    if state is not None and state.status == STATUS_COMPLETED and stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED:
        status = "completed"
    elif state is not None and state.status == STATUS_STOPPED and stop_reason == STOP_REASON_USER_CANCELLED:
        status = "cancelled"
    elif stop_reason in {"service_restarted", "service_shutdown_timeout"}:
        status = "interrupted"
    elif stop_reason in {
        "approval_denied",
        "budget_exhausted",
        "no_changes_to_review",
        "retry_limit_reached",
        "review_retry_limit_reached",
        "step_limit_reached",
    }:
        status = "blocked"
    else:
        status = "failed"
    error = {
        "stage": getattr(state, "error_stage", ""),
        "code": getattr(state, "error_code", ""),
        "retryable": bool(getattr(state, "error_retryable", False)),
        "attempts": max(0, int(getattr(state, "error_attempts", 0))),
    }
    send(
        {
            "type": "terminal",
            "task_id": task["task_id"],
            "status": status,
            "stop_reason": stop_reason,
            "final_answer": redact_artifact(getattr(state, "final_answer", "") or ""),
            "message_total": len(pico.session.get("history", [])),
            "session_updated_at": pico.session.get("updated_at", ""),
            "session_persisted": True,
            "error": error if error["code"] else {},
        }
    )


def _required_env(name: str, default: str = "") -> str:
    import os

    value = os.environ.get(name, default).strip()
    if not value:
        raise RuntimeError(f"{name} is not configured on the local Worker")
    return value


def _supported_reasoning_efforts() -> tuple[str, ...]:
    import os
    import urllib.parse

    configured = os.environ.get("PICO_REASONING_EFFORTS", "").strip()
    if configured:
        values = tuple(
            value.strip().lower()
            for value in configured.split(",")
            if value.strip()
        )
    else:
        hostname = urllib.parse.urlsplit(
            _required_env("PICO_OPENAI_API_BASE", "https://api.openai.com/v1")
        ).hostname
        model_id = os.environ.get("PICO_OPENAI_MODEL", "").strip().lower().rsplit("/", 1)[-1]
        reasoning_model = model_id.startswith(("gpt-5", "o1", "o3", "o4"))
        values = (
            ("none", "minimal", "low", "medium", "high", "xhigh")
            if hostname == "api.openai.com" or reasoning_model
            else ("none",)
        )
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    normalized = tuple(dict.fromkeys(value for value in values if value in allowed))
    return normalized or ("none",)


def _classify_error(exc: Exception) -> tuple[str, str]:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, urllib.error.HTTPError):
            return f"model_http_{current.code}", STOP_REASON_MODEL_ERROR
        if isinstance(current, (urllib.error.URLError, ConnectionError, TimeoutError)):
            return "model_connection_error", STOP_REASON_MODEL_ERROR
        current = current.__cause__
    return type(exc).__name__, STOP_REASON_RUNTIME_ERROR


def _args_digest(args: dict) -> str:
    encoded = json.dumps(
        args,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

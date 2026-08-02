"""Internal role-specific child agents used by the LangGraph wrapper."""

from dataclasses import dataclass
import time

from .run_lifecycle import finalize_failed_run
from .session_store import InMemorySessionStore
from .task_state import STOP_REASON_RUNTIME_ERROR
from .workspace import clip

RESEARCH_ALLOWED_TOOLS = ("list_files", "read_file", "search")
REVIEW_ALLOWED_TOOLS = ("read_file", "search")

RESEARCH_PROMPT_PREFIX = (
    "You are a read-only research delegate. Do not modify files.\n"
    "Use only evidence from the workspace.\n"
    "Return Findings, Candidate files, and Suggested action.\n\n"
)

REVIEW_PROMPT_TEMPLATE = (
    "You are a read-only review delegate. Do not modify files.\n"
    "Review only these focus paths: {focus_paths}\n"
    "Acceptance criteria: {acceptance}\n"
    "Execution context: {context_summary}\n"
    "The first non-empty line must be exactly status: pass or status: needs_fix.\n"
    "Then return issues and verify_targets.\n\n"
)


@dataclass(frozen=True)
class RoleDelegateSpec:
    role: str
    task: str
    allowed_tools: tuple[str, ...]
    focus_paths: tuple[str, ...] = ()
    acceptance: str = ""
    context_summary: str = ""
    max_steps: int = 3


def validate_role_delegate_spec(spec):
    if spec.role not in {"research", "review"}:
        raise ValueError("role must be research or review")
    if not str(spec.task).strip():
        raise ValueError("task must not be empty")
    if not isinstance(spec.max_steps, int) or isinstance(spec.max_steps, bool) or not 1 <= spec.max_steps <= 12:
        raise ValueError("role delegate max_steps must be in [1, 12]")
    allowed = tuple(str(name).strip() for name in spec.allowed_tools)
    expected = RESEARCH_ALLOWED_TOOLS if spec.role == "research" else REVIEW_ALLOWED_TOOLS
    if not allowed or any(name not in expected for name in allowed):
        raise ValueError(f"{spec.role} role requested tools outside its allowlist")
    focus_paths = tuple(str(path).strip() for path in spec.focus_paths)
    if any(not path for path in focus_paths):
        raise ValueError("focus_paths must contain non-empty strings")
    if spec.role == "review":
        if not focus_paths:
            raise ValueError("review role requires focus_paths")
        if not str(spec.acceptance).strip():
            raise ValueError("review role requires acceptance")
        if not str(spec.context_summary).strip():
            raise ValueError("review role requires context_summary")
    return spec


def normalize_review_result(text):
    raw = str(text or "").lstrip()
    lines = raw.splitlines()
    first_line = lines[0].strip().lower() if lines else ""
    issue_codes = [
        "malformed_review_status"
        for line in lines
        if line.strip().lower() in {
            "issue: malformed_review_status",
            "issues: malformed_review_status",
        }
    ]
    if first_line == "status: pass":
        return {"status": "pass", "text": raw, "issue_codes": issue_codes, "recovered": False}
    if first_line == "status: needs_fix":
        return {"status": "needs_fix", "text": raw, "issue_codes": issue_codes, "recovered": False}

    normalized = "status: needs_fix\nissue: malformed_review_status"
    if raw:
        normalized += "\n" + raw
    return {
        "status": "needs_fix",
        "text": normalized,
        "issue_codes": ["malformed_review_status"],
        "recovered": True,
    }


def _emit(parent, event, payload):
    if parent.current_task_state is None:
        return
    parent.emit_trace(parent.current_task_state, event, payload)


def _collect_child_state(parent, child):
    if child is None:
        return
    if child.current_task_state is not None:
        parent.child_task_states.append(child.current_task_state)
    parent.child_task_states.extend(child.child_task_states)


def create_role_delegate(parent, spec):
    from .runtime import Pico

    validate_role_delegate_spec(spec)
    started_at = time.monotonic()
    child = None
    error = None
    result = ""
    start_event = "review_requested" if spec.role == "review" else "delegate_started"
    _emit(
        parent,
        start_event,
        {
            "agent_role": spec.role,
            "task": clip(spec.task, 200),
            "focus_paths": list(spec.focus_paths),
            "max_steps": spec.max_steps,
            "allowed_tools": list(spec.allowed_tools),
            "status": "started",
        },
    )
    try:
        prompt = RESEARCH_PROMPT_PREFIX + spec.task
        if spec.role == "review":
            prompt = REVIEW_PROMPT_TEMPLATE.format(
                focus_paths=", ".join(spec.focus_paths),
                acceptance=spec.acceptance,
                context_summary=spec.context_summary,
            ) + spec.task
        child = Pico(
            model_client=parent.model_client,
            workspace=parent.workspace,
            session_store=InMemorySessionStore(),
            run_store=parent.run_store,
            approval_policy="never",
            max_steps=spec.max_steps,
            max_new_tokens=parent.max_new_tokens,
            depth=parent.depth + 1,
            max_depth=parent.max_depth,
            read_only=True,
            secret_env_names=parent.secret_env_names,
            shell_env_allowlist=parent.shell_env_allowlist,
            feature_flags=parent.feature_flags,
            allowed_tools=spec.allowed_tools,
            progress_callback=parent.progress_callback,
            event_sink=parent.event_sink,
            allow_checkpoint=False,
            allow_durable_memory_write=False,
        )
        child.agent_role = spec.role
        child.session["memory"]["task"] = spec.task
        if spec.role == "research":
            child.session["memory"]["notes"] = [clip(parent.history_text(), 300)]
        result = child.ask(prompt)
    except Exception as exc:
        error = exc
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        if error is not None and child is not None and child.current_task_state is not None:
            finalize_failed_run(
                child,
                child.current_task_state,
                error_type=type(error).__name__,
                duration_ms=duration_ms,
                stop_reason=getattr(error, "stop_reason", STOP_REASON_RUNTIME_ERROR),
            )
        _collect_child_state(parent, child)
        if error is not None:
            _emit(
                parent,
                "delegate_failed",
                {
                    "agent_role": spec.role,
                    "task": clip(spec.task, 200),
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "duration_ms": duration_ms,
                    "child_run_id": getattr(getattr(child, "current_task_state", None), "run_id", ""),
                },
            )
    if error is not None:
        raise error

    duration_ms = int((time.monotonic() - started_at) * 1000)
    if spec.role == "review":
        review = normalize_review_result(result)
        result = review["text"]
        if review["recovered"] and parent.current_task_state is not None:
            parent.current_task_state.record_malformed_output_recovered()
            parent.run_store.write_task_state(parent.current_task_state)
        _emit(
            parent,
            "review_passed" if review["status"] == "pass" else "review_failed",
            {
                "agent_role": spec.role,
                "status": review["status"],
                "focus_paths": list(spec.focus_paths),
                "issues": review["issue_codes"],
                "duration_ms": duration_ms,
                "child_run_id": child.current_task_state.run_id if child.current_task_state is not None else "",
            },
        )
    else:
        _emit(
            parent,
            "delegate_finished",
            {
                "agent_role": spec.role,
                "status": "completed",
                "duration_ms": duration_ms,
                "child_run_id": child.current_task_state.run_id if child.current_task_state is not None else "",
                "result_preview": clip(result, 300),
            },
        )
    return child, result

"""Native single-loop orchestration (§7.7.1 阶段 2).

目标形态：`Pico.ask()` → `AgentLoop.run()` 是唯一顶层循环；intent 路由降级为
入口 + 预算门；review 降级为收尾确定性 gate（写触发）；预算 = 单循环计数。
本模块提供 ``run_native()``：与 ``run_agent()`` 同形的原生路径，供
local-worker 切换与 benchmark 等价性验证。

设计：
- 复用 intent.py / planning.py 的纯函数（不依赖 LangGraph 图状态）。
- AgentLoop 顶层跑（已有单循环 + inbox + checkpoint + memory）。
- 收尾 review_gate（§7.8.7 方案 C：写触发 + checklist 派生预算）。
- 产出 BackendRunResult（与 run_agent 同形）。
"""

from __future__ import annotations

import time
from pathlib import Path, PureWindowsPath

from pico.agent_loop import AgentLoop, _best_effort_step_limit
from pico.evaluation.backends import (
    BackendRunResult,
    HarnessModelClientAdapter,
    default_event_sink_factory,
)
from pico.evaluation.evaluator import _apply_task_setup
from pico.evaluation.review_gate import run_review_gate
from pico.event_sink import CompositeSink, EventCollector
from pico.execution_hooks import RunCancelled
from pico.run_lifecycle import finalize_run
from pico.runtime import Pico
from pico.task_state import (
    STATUS_FAILED,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
    STOP_REASON_RUNTIME_ERROR,
    STOP_REASON_STEP_LIMIT_REACHED,
    STOP_REASON_USER_CANCELLED,
    TaskState,
)

from .intent import (
    INTENT_CODE_CHANGE,
    INTENT_CONVERSATION,
    INTENT_READ_ONLY,
    TASK_MODE_AUTO,
    normalize_task_mode,
)
from .planning import is_continuation_request, is_plain_conversation_request
from .review_gate import ReviewDecision  # noqa: F401  (re-export for callers)

# intent 硬顶预算（断路器；方案 C 的 soft 预算来自 planner 自报，原生路径
# 无 planner 时直接用 intent 硬顶作为单循环预算）。
INTENT_STEP_BUDGETS = {
    INTENT_CONVERSATION: 2,
    INTENT_READ_ONLY: 16,
    INTENT_CODE_CHANGE: 24,
}

RUN_METADATA_KEYS = (
    "requested_task_mode",
    "resolved_intent",
    "intent_source",
    "intent_attempts",
    "answer_attempts",
)

# 预算耗尽且「有证据」时走收敛（completed + budget_converged），零证据仍 blocked。
_BUDGET_CONVERGENCE_REASONS = frozenset({"budget_exhausted", "step_limit_reached"})


def _answer_candidate(agent, action):
    getattr(agent.execution_hooks, f"{action}_answer_candidate", lambda *_args: None)(
        agent.current_task_state
    )


def _initial_state_snapshot(agent):
    from pico.features import memory as memorylib

    memory_state = agent.memory.to_dict()
    return {
        "initial_history_empty": len(agent.session["history"]) == 0,
        "initial_memory_empty": memorylib.is_effectively_empty(memory_state),
        "initial_task_summary_empty": not str(memory_state["working"]["task_summary"]).strip(),
        "initial_episodic_notes_empty": not memory_state["episodic_notes"],
    }


def _intent_context(agent, max_messages=4, max_chars=2000):
    messages = [
        item
        for item in agent.session.get("history", [])
        if item.get("role") in {"user", "assistant"}
    ][-max_messages:]
    lines = [f"{item.get('role', '')}: {item.get('content', '')}" for item in messages]
    return agent.redact_text("\n".join(lines))[-max_chars:]


def _continuation_context(agent, task_input, max_chars=1200):
    """Return the preceding user task for a short explicit continuation request."""
    if not is_continuation_request(task_input):
        return ""
    for item in reversed(agent.session.get("history", [])):
        if item.get("role") != "user":
            continue
        previous = str(item.get("content", "")).strip()
        if previous and not is_continuation_request(previous):
            return agent.redact_text(previous)[-max_chars:]
    return ""


def _materialize_focus_paths(focus_paths):
    if focus_paths is None:
        return ()
    if isinstance(focus_paths, (str, bytes)):
        raise ValueError("focus_paths must be an iterable of relative path strings")
    try:
        values = tuple(focus_paths)
    except TypeError as exc:
        raise ValueError("focus_paths must be an iterable of relative path strings") from exc
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("focus_paths must contain non-empty strings")
    return values


def _normalized_focus_paths(agent, focus_paths):
    normalized = []
    for raw_path in focus_paths:
        raw = raw_path.strip()
        if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
            raise ValueError("focus_paths must be workspace-relative")
        path = agent.tool_context().path(raw)
        relative = path.relative_to(agent.root).as_posix()
        if relative in {"", "."}:
            raise ValueError("focus_paths must identify a file or subdirectory")
        if relative not in normalized:
            normalized.append(relative)
    return normalized


def _resolve_intent(agent, task_input, *, task_mode, router_model_client):
    """intent 路由（复用 intent.py 纯函数）。

    返回 (intent, requires_research, source, attempts)。
    task_mode 显式指定时直接采用；auto 时用 router 模型分类。
    """
    from .intent import (
        MAX_INTENT_ATTEMPTS,
        build_intent_prompt,
        parse_intent_output,
    )

    if task_mode in {INTENT_CONVERSATION, INTENT_READ_ONLY, INTENT_CODE_CHANGE}:
        return task_mode, False, "explicit", 0
    if is_plain_conversation_request(task_input):
        return INTENT_CONVERSATION, False, "plain_conversation", 0
    if is_continuation_request(task_input):
        # 继续请求：保持只读（原生无重路由，保守收敛）
        return INTENT_READ_ONLY, False, "continuation", 0

    if router_model_client is None:
        return INTENT_CODE_CHANGE, False, "default_code_change", 0

    context = _intent_context(agent)
    for attempt in range(1, MAX_INTENT_ATTEMPTS + 1):
        prompt = build_intent_prompt(task_input, context, retry=attempt > 1)
        agent.cancellation_token.raise_if_cancelled()
        agent.execution_hooks.before_model(agent.current_task_state)
        raw = router_model_client.complete(
            prompt,
            96,
            on_retry=lambda details: getattr(
                agent.execution_hooks, "model_retrying", lambda *_args: None
            )(agent.current_task_state, "intent", details),
            on_text_delta=lambda delta: getattr(
                agent.execution_hooks, "model_text_delta", lambda *_args: None
            )(agent.current_task_state, "intent", delta),
        )
        agent.current_task_state.record_attempt()
        try:
            intent, requires_research = parse_intent_output(str(raw))
            return intent, requires_research, "router", attempt
        except Exception:
            agent.current_task_state.record_malformed_output_recovered()
            continue
    return INTENT_CODE_CHANGE, False, "router_failed", MAX_INTENT_ATTEMPTS


def _run_native_loop(agent, task_input, *, task_id, run_id):
    """AgentLoop 顶层跑，返回 final_answer（原生循环内部已持久化）。"""
    loop = AgentLoop(agent)
    return loop.run(task_input, task_id=task_id, run_id=run_id)


def run_native(
    agent,
    task_input,
    *,
    acceptance=None,
    step_budget=None,
    requires_research=None,
    focus_paths=None,
    task_mode=INTENT_CODE_CHANGE,
    router_model_client=None,
    record_session=True,
    enable_planning=False,
    task_id=None,
    run_id=None,
    workspace_id="",
    planning_deadline_seconds=75,
    inbox=None,
):
    """Run the native single-loop workflow with review gate closure.

    §7.7.1 阶段 2：用 AgentLoop（原生 ReAct 循环）跑完整任务，收尾接
    review_gate 做确定性完成门禁（写触发 + checklist 派生预算）。
    产出与 run_agent 同形的 BackendRunResult。
    """
    task_input = str(task_input).strip()
    if not task_input:
        raise ValueError("task_input must not be empty")
    normalized_mode = normalize_task_mode(task_mode)
    raw_focus_paths = _materialize_focus_paths(focus_paths)
    has_focus = bool(raw_focus_paths)
    has_acceptance = acceptance is not None and bool(str(acceptance).strip())
    if normalized_mode in {INTENT_CONVERSATION, INTENT_READ_ONLY} and has_focus:
        raise ValueError(f"focus_paths are not valid for task_mode={normalized_mode}")
    if normalized_mode in {INTENT_CONVERSATION, INTENT_READ_ONLY} and has_acceptance:
        raise ValueError(f"acceptance is only valid for auto or {INTENT_CODE_CHANGE}")
    if normalized_mode == INTENT_CONVERSATION and requires_research is True:
        raise ValueError("conversation tasks cannot require research")
    if normalized_mode != TASK_MODE_AUTO and router_model_client is not None:
        raise ValueError("router_model_client is only valid for task_mode=auto")
    if isinstance(step_budget, bool):
        raise ValueError("step_budget must be a positive integer")
    step_budget_explicit = step_budget is not None
    step_budget = int(agent.max_steps if step_budget is None else step_budget)
    if step_budget < 1:
        raise ValueError("step_budget must be positive")
    review_paths = _normalized_focus_paths(agent, raw_focus_paths)
    initial_state = _initial_state_snapshot(agent)

    original_sink = agent.event_sink
    original_model_client = agent.model_client
    collector = EventCollector()
    agent.event_sink = CompositeSink(collector, original_sink)
    if not isinstance(original_model_client, HarnessModelClientAdapter):
        agent.model_client = HarnessModelClientAdapter(original_model_client)
    if router_model_client is not None and not isinstance(
        router_model_client, HarnessModelClientAdapter
    ):
        router_model_client = HarnessModelClientAdapter(router_model_client)

    task_state = None
    final_answer = ""
    run_metadata_collector = {
        "requested_task_mode": normalized_mode,
        "resolved_intent": "",
        "intent_source": "",
        "intent_attempts": 0,
        "answer_attempts": 0,
    }
    try:
        started_at = time.monotonic()
        if record_session:
            agent.memory.set_task_summary(task_input)
            agent.session["memory"] = agent.memory.to_dict()

        # intent 路由（入口 + 预算门）——在 AgentLoop 创建 TaskState 之前，
        # 这样 intent/预算能反映到 AgentLoop 创建的 state 上。
        # 先建一个临时 TaskState 供 intent 阶段的 hooks/attempt 记录使用；
        # AgentLoop 会创建正式 TaskState 并替换 agent.current_task_state。
        task_state = TaskState.create(
            run_id=str(run_id or agent.new_run_id()),
            task_id=str(task_id or agent.new_task_id()),
            user_request=task_input,
            max_tool_steps=agent.max_steps,
            max_read_files=agent.max_read_files,
            max_total_steps=agent.max_total_steps,
        )
        agent.current_task_state = task_state
        intent, requires_research, intent_source, intent_attempts = _resolve_intent(
            agent,
            task_input,
            task_mode=normalized_mode,
            router_model_client=router_model_client,
        )
        run_metadata_collector.update(
            {
                "resolved_intent": intent,
                "intent_source": intent_source,
                "intent_attempts": intent_attempts,
            }
        )
        # 显式 step_budget 优先；否则 intent 硬顶（方案 C 的断路器）。
        if not step_budget_explicit:
            hard_cap = int(INTENT_STEP_BUDGETS.get(intent, 0))
            if hard_cap:
                step_budget = min(step_budget, hard_cap)
        agent.max_steps = step_budget

        # 原生单循环跑（AgentLoop 自己创建 TaskState 并挂到 agent.current_task_state）。
        agent.emit_trace(
            agent.current_task_state,
            "native_loop_started",
            {"intent": intent, "step_budget": step_budget},
        )
        final_answer = _run_native_loop(
            agent,
            task_input,
            task_id=task_id,
            run_id=run_id,
        )
        task_state = agent.current_task_state
        if task_state is not None:
            task_state.intent = intent

        # 收尾 review gate（写触发 + checklist 派生预算）。
        if task_state is not None and task_state.status == "completed" and task_state.final_answer:
            decision = run_review_gate(
                task_state,
                intent=intent,
                step_budget=step_budget,
                coordinator_steps_used=int(getattr(task_state, "tool_steps", 0) or 0),
                step_budget_explicit=step_budget_explicit,
                hard_cap=int(INTENT_STEP_BUDGETS.get(intent, 0)),
            )
            if decision.status == "needs_fix":
                task_state.review_status = "needs_fix"
                task_state.record_error(
                    stage="completion_gate",
                    code="completion_gate_failed",
                )
                final_answer = task_state.final_answer
        if task_state is not None:
            run_metadata_collector["answer_attempts"] = int(getattr(task_state, "attempts", 0))
    except RunCancelled:
        if task_state is not None:
            task_state.stop_user_cancelled()
        final_answer = ""
    except Exception as exc:
        if task_state is not None:
            _answer_candidate(agent, "discard")
            error_code = str(getattr(exc, "code", "") or "runtime_error")
            stop_reason = getattr(exc, "stop_reason", STOP_REASON_RUNTIME_ERROR)
            if stop_reason not in {
                STOP_REASON_MODEL_ERROR,
                STOP_REASON_RUNTIME_ERROR,
            }:
                stop_reason = STOP_REASON_RUNTIME_ERROR
            task_state.record_error(
                stage=getattr(exc, "stage", "runtime"),
                code=error_code,
                retryable=getattr(exc, "retryable", False),
                attempts=getattr(exc, "attempts", 1),
            )
            task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
            final_answer = task_state.final_answer
    finally:
        if task_state is not None and task_state.status == "running":
            task_state.stop(
                STOP_REASON_RUNTIME_ERROR,
                status=STATUS_FAILED,
                final_answer=final_answer,
            )
        if task_state is not None:
            finalize_run(
                agent,
                task_state,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            final_answer = task_state.final_answer
        agent.event_sink = original_sink
        agent.model_client = original_model_client

    # AgentLoop.run() 已 record user + assistant（record_session 语义由 AgentLoop
    # 控制）；run_native 不再重复 record，避免 message_total 翻倍。
    return BackendRunResult(
        task_state=task_state,
        final_answer=final_answer,
        agent=agent,
        child_task_states=list(agent.child_task_states or []),
        budget_task_states=[task_state] if task_state is not None else [],
        initial_state=initial_state,
        events=collector.snapshot(),
        run_metadata=run_metadata_collector,
    )

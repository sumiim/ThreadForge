"""Pure-data LangGraph orchestration for Pico's routed three-role workflow."""

import json
import time
from copy import deepcopy
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pico.delegates import (
    RoleDelegateSpec,
    create_role_delegate,
    normalize_review_result,
)
from pico.run_lifecycle import finalize_failed_run
from pico.runtime import Pico
from pico.session_store import InMemorySessionStore
from pico.task_state import (
    STATUS_COMPLETED,
    STOP_REASON_PERSISTENCE_ERROR,
    STOP_REASON_RUNTIME_ERROR,
)

from .intent import (
    INTENT_CODE_CHANGE,
    INTENT_CONVERSATION,
    INTENT_READ_ONLY,
    MAX_CONVERSATION_ATTEMPTS,
    MAX_INTENT_ATTEMPTS,
    ROUTE_MODE_DIRECT,
    ROUTER_MAX_NEW_TOKENS,
    ROUTER_PLAN_MAX_NEW_TOKENS,
    TASK_MODE_AUTO,
    IntentDecision,
    build_conversation_prompt,
    build_intent_prompt,
    build_read_only_prompt,
    parse_conversation_output,
    parse_intent_output,
    parse_routed_task_output,
)
from .planning import (
    MAX_PLAN_ATTEMPTS,
    PLAN_MINIMUM_BUDGETS,
    PLANNER_MAX_NEW_TOKENS,
    PlanValidationError,
    build_plan_prompt,
    build_routed_planning_prompt,
    is_plain_conversation_request,
    parse_and_validate_plan,
)

MAX_FIX_ATTEMPTS = 2
MAX_REPLAN_ATTEMPTS = 2
MAX_REQUIRED_TOOL_ATTEMPTS = 2
PLANNING_DEADLINE_SECONDS = 75.0
READ_ONLY_TOOLS = ("list_files", "read_file", "search")
COMPLETION_METADATA_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_hit",
    "prompt_cache_supported",
    "prompt_cache_key",
    "prompt_cache_retention",
    "requested_reasoning_effort",
    "effective_reasoning_effort",
)

PLAN_MAXIMUM_BUDGETS = {
    "model_rounds": 64,
    "tool_calls": 25,
    "input_tokens": 1_000_000,
    "output_tokens": 64_000,
    "elapsed_seconds": 3_600,
}


class AgentState(TypedDict):
    task: str
    workspace_id: str
    acceptance: str
    requested_task_mode: str
    resolved_intent: str
    intent_source: str
    intent_attempts: int
    answer_attempts: int
    intent_context: str
    continuation_context: str
    completion_status: str
    step_budget: int
    coordinator_steps_used: int
    requires_research: bool | None
    research_result: str
    execution_result: str
    affected_paths: list[str]
    review_focus_paths: list[str]
    review_status: str
    review_issues: str
    fix_attempts: int
    terminal_reason: str
    delegate_failures: int
    final_result: str
    planning_enabled: bool
    plan: dict
    plan_attempts: int
    plan_error: str
    plan_history: list[dict]
    replan_requested: bool
    replan_reason: str
    replan_attempts: int
    router_direct_answer: bool
    started_monotonic: float


class GraphPersistenceError(RuntimeError):
    stop_reason = STOP_REASON_PERSISTENCE_ERROR


def _failed_state(state, reason, final_result):
    return {
        **state,
        "completion_status": "failed",
        "terminal_reason": reason,
        "final_result": final_result,
    }


def _write_graph_task_state(agent):
    try:
        agent.run_store.write_task_state(agent.current_task_state)
    except Exception as exc:
        raise GraphPersistenceError("graph task state persistence failed") from exc


def _safe_completion_metadata(agent, model_client):
    metadata = dict(getattr(model_client, "last_completion_metadata", {}) or {})
    filtered = {key: metadata[key] for key in COMPLETION_METADATA_KEYS if key in metadata}
    return agent.redact_artifact(filtered)


def _plan_limits(state, agent):
    """Return run-wide limits without conflating them with one agent loop."""
    limits = dict(PLAN_MAXIMUM_BUDGETS)
    limits["tool_calls"] = min(limits["tool_calls"], max(1, int(state["step_budget"])))
    return limits


def _run_budget_usage(state, config):
    configurable = config["configurable"]
    agent = configurable["agent"]
    states = [agent.current_task_state]
    states.extend(agent.child_task_states)
    states.extend(configurable["node_child_states"])
    unique = {item.run_id: item for item in states if item is not None}
    return {
        "model_rounds": sum(item.attempts for item in unique.values()),
        "tool_calls": sum(item.tool_steps for item in unique.values()),
        "input_tokens": sum(item.input_tokens for item in unique.values()),
        "output_tokens": sum(item.output_tokens for item in unique.values()),
        "elapsed_seconds": max(0, int(time.monotonic() - state["started_monotonic"])),
    }


def _plan_minimum_budgets(state, config, maximum):
    """Reserve budget for the current plan and mandatory downstream stages."""
    usage = _run_budget_usage(state, config)
    minimum = dict(PLAN_MINIMUM_BUDGETS)
    minimum["model_rounds"] = min(
        int(maximum["model_rounds"]),
        max(int(minimum["model_rounds"]), int(usage["model_rounds"]) + 2),
    )
    minimum["input_tokens"] = min(
        int(maximum["input_tokens"]),
        max(int(minimum["input_tokens"]), int(usage["input_tokens"]) + 20_000),
    )
    minimum["output_tokens"] = min(
        int(maximum["output_tokens"]),
        max(int(minimum["output_tokens"]), int(usage["output_tokens"]) + 2_000),
    )
    minimum["elapsed_seconds"] = min(
        int(maximum["elapsed_seconds"]),
        max(int(minimum["elapsed_seconds"]), int(usage["elapsed_seconds"]) + 120),
    )
    minimum["tool_calls"] = min(
        int(maximum["tool_calls"]),
        int(usage["tool_calls"]),
    )
    return minimum


def _budget_failure(state, config):
    if not state["planning_enabled"] or not state["plan"]:
        return None
    usage = _run_budget_usage(state, config)
    configurable = config["configurable"]
    agent = configurable["agent"]
    maximum = _plan_limits(state, agent)
    hard_exceeded = [key for key, value in usage.items() if value > int(maximum[key])]
    if hard_exceeded:
        agent.emit_trace(
            agent.current_task_state,
            "plan_budget_exhausted",
            {
                "budget_keys": hard_exceeded,
                "usage": usage,
                "maximum_budgets": maximum,
            },
        )
        return _failed_state(
            state,
            "budget_exhausted",
            "系统运行预算已耗尽："
            + "、".join(
                {
                    "model_rounds": "模型调用轮次",
                    "tool_calls": "工具调用次数",
                    "input_tokens": "输入 Token",
                    "output_tokens": "输出 Token",
                    "elapsed_seconds": "运行时间",
                }.get(key, key)
                for key in hard_exceeded
            ),
        )

    plan = state["plan"]
    planned = {key: int(plan.get("budgets", {}).get(key, 0)) for key in maximum}
    runtime = configurable.setdefault("plan_budget_runtime", {})
    identity = (str(plan.get("plan_id", "")), int(plan.get("revision", 0)))
    if runtime.get("identity") != identity:
        runtime.clear()
        runtime.update({"identity": identity, "effective": dict(planned)})
    effective = runtime["effective"]
    soft_exceeded = [key for key, value in usage.items() if value > int(effective[key])]
    if not soft_exceeded:
        return None

    minimum = _plan_minimum_budgets(state, config, maximum)
    previous = dict(effective)
    for key in maximum:
        effective[key] = min(
            int(maximum[key]),
            max(int(effective[key]), int(planned[key]), int(minimum[key]), int(usage[key])),
        )
    agent.emit_trace(
        agent.current_task_state,
        "plan_budget_extended",
        {
            "budget_keys": soft_exceeded,
            "usage": usage,
            "planned_budgets": planned,
            "previous_effective_budgets": previous,
            "effective_budgets": dict(effective),
        },
    )
    return None


def _complete_graph_model(
    agent,
    model_client,
    prompt,
    max_new_tokens,
    *,
    stage,
    deadline_monotonic=None,
):
    agent.cancellation_token.raise_if_cancelled()
    agent.execution_hooks.before_model(agent.current_task_state)
    try:
        raw = model_client.complete(
            prompt,
            max_new_tokens,
            deadline_monotonic=deadline_monotonic,
            on_retry=lambda details: getattr(
                agent.execution_hooks, "model_retrying", lambda *_args: None
            )(agent.current_task_state, stage, details),
            on_text_delta=lambda delta: getattr(
                agent.execution_hooks, "model_text_delta", lambda *_args: None
            )(agent.current_task_state, stage, delta),
        )
    except Exception as exc:
        if hasattr(exc, "at_stage"):
            exc.at_stage(stage)
        raise
    metadata = dict(getattr(model_client, "last_completion_metadata", {}) or {})
    agent.last_completion_metadata = metadata
    agent.current_task_state.record_model_usage(metadata)
    agent.cancellation_token.raise_if_cancelled()
    agent.execution_hooks.after_model(agent.current_task_state, metadata)
    return raw


def _record_graph_model_attempt(
    agent,
    metadata_collector,
    *,
    event,
    attempt,
    counter_key,
):
    agent.current_task_state.record_attempt()
    _write_graph_task_state(agent)
    metadata_collector[counter_key] = int(attempt)
    agent.emit_trace(agent.current_task_state, event, {"attempt": int(attempt)})


def _emit_route(agent, from_node, to_node, reason):
    agent.emit_trace(
        agent.current_task_state,
        "route_selected",
        {
            "from_node": str(from_node),
            "to_node": str(to_node),
            "reason": str(reason),
        },
    )
    agent.emit_progress(f"route: {from_node} -> {to_node}")


def _record_graph_delegate_call(agent):
    agent.current_task_state.record_tool("delegate")
    _write_graph_task_state(agent)


def _require_graph_delegate_permission(agent):
    if agent.allowed_tools is not None and "delegate" not in agent.allowed_tools:
        raise PermissionError("langgraph task does not allow delegate")


def _call_graph_role_delegate(agent, spec):
    _require_graph_delegate_permission(agent)
    _record_graph_delegate_call(agent)
    try:
        child, text = create_role_delegate(agent, spec)
        return {"ok": True, "text": text, "child": child}
    except Exception as exc:
        if getattr(exc, "stop_reason", "") == "model_error":
            if hasattr(exc, "at_stage"):
                exc.at_stage(str(spec.role))
            raise
        return {"ok": False, "text": "", "error_type": type(exc).__name__}


def _child_task_states(agent, config):
    """Return unique child states produced by delegates and isolated executors."""
    states = [*getattr(agent, "child_task_states", ())]
    states.extend(config["configurable"].get("node_child_states", ()))
    unique = {}
    for item in states:
        if item is not None and getattr(item, "run_id", None):
            unique[str(item.run_id)] = item
    return tuple(unique.values())


def _successful_read_tool_names(agent, config):
    names = set()
    for child in _child_task_states(agent, config):
        names.update(
            str(item.get("tool_name", ""))
            for item in getattr(child, "evidence", ())
            if item.get("tool_name") in READ_ONLY_TOOLS
            and item.get("status") in {"ok", "partial_success"}
        )
    return names


def _planned_read_tools(state, agent):
    if not state["planning_enabled"]:
        return ()
    required = []
    for step in state.get("plan", {}).get("steps", []):
        for name in step.get("required_tools", []):
            if name in READ_ONLY_TOOLS and name in agent.tools and name not in required:
                required.append(name)
    return tuple(required)


def _begin_answer_candidate(agent):
    getattr(agent.execution_hooks, "begin_answer_candidate", lambda *_args: None)(
        agent.current_task_state
    )


def _create_isolated_executor(
    agent,
    *,
    allowed_tools,
    read_only,
    approval_policy,
    max_steps,
):
    return Pico(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=InMemorySessionStore(),
        session=deepcopy(agent.session),
        run_store=agent.run_store,
        approval_policy=approval_policy,
        approval_strategy=agent.approval_strategy,
        cancellation_token=agent.cancellation_token,
        execution_hooks=agent.execution_hooks,
        max_steps=max_steps,
        max_total_steps=max(
            int(agent.max_total_steps or 0),
            max(max_steps * 3, max_steps + 4),
        ),
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth,
        max_depth=agent.max_depth,
        read_only=read_only,
        allowed_tools=allowed_tools,
        event_sink=agent.event_sink,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
        progress_callback=agent.progress_callback,
        shell_output_max_bytes=agent.shell_output_max_bytes,
        shell_cleanup_grace_seconds=agent.shell_cleanup_grace_seconds,
        max_read_files=agent.max_read_files,
        feature_flags=agent.feature_flags,
        allow_checkpoint=False,
        allow_durable_memory_write=False,
    )


def prepare_plan_node(state: AgentState, config: RunnableConfig) -> AgentState:
    if not state["planning_enabled"]:
        return state

    configurable = config["configurable"]
    agent = configurable["agent"]
    metadata_collector = configurable["run_metadata_collector"]
    available_tools = tuple(
        name for name in (agent.allowed_tools or agent.tools) if name != "delegate"
    )
    is_replan = bool(state["plan"] and state["replan_requested"])
    if is_replan and state["replan_attempts"] >= MAX_REPLAN_ATTEMPTS:
        return _failed_state(
            state,
            "review_retry_limit_reached",
            "Replanning retry limit was reached.",
        )
    expected_revision = int(state["plan"].get("revision", 0)) + 1 if is_replan else 1
    expected_plan_id = str(state["plan"].get("plan_id", "")) if is_replan else ""
    maximum_budgets = _plan_limits(state, agent)
    error_code = ""
    error_message = ""
    planning_deadline = time.monotonic() + float(
        configurable.get("planning_deadline_seconds", PLANNING_DEADLINE_SECONDS)
    )

    if not is_replan and is_plain_conversation_request(state["task"]):
        minimum_budgets = _plan_minimum_budgets(state, config, maximum_budgets)
        plan = {
            "schema_version": "1",
            "plan_id": "conversation_direct",
            "revision": 1,
            "intent": INTENT_CONVERSATION,
            "summary": "Respond directly without workspace tools.",
            "steps": [
                {
                    "id": "respond",
                    "goal": "Answer the current conversational request directly",
                    "dependencies": [],
                    "required_tools": [],
                    "required_evidence": [],
                    "done_when": ["a concise direct response is returned"],
                }
            ],
            "acceptance": ["a direct response is returned without workspace access"],
            "risk_level": "low",
            "budgets": dict(minimum_budgets),
        }
        task_state = agent.current_task_state
        task_state.plan_id = plan["plan_id"]
        task_state.plan_revision = plan["revision"]
        task_state.intent = plan["intent"]
        task_state.checklist = [step["goal"] for step in plan["steps"]]
        task_state.done_when = [item for step in plan["steps"] for item in step["done_when"]]
        task_state.completed_items = []
        _write_graph_task_state(agent)
        metadata_collector.update(
            {
                "resolved_intent": plan["intent"],
                "intent_source": "direct_conversation",
                "intent_attempts": 0,
            }
        )
        agent.emit_trace(
            task_state,
            "plan_skipped",
            {
                "reason": "plain_conversation",
                "intent": plan["intent"],
                "summary": plan["summary"],
            },
        )
        return {
            **state,
            "acceptance": "\n".join(plan["acceptance"]),
            "resolved_intent": plan["intent"],
            "intent_source": "direct_conversation",
            "plan": plan,
            "plan_attempts": 0,
        }

    for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
        _record_graph_model_attempt(
            agent,
            metadata_collector,
            event="plan_requested",
            attempt=attempt,
            counter_key="plan_attempts",
        )
        minimum_budgets = _plan_minimum_budgets(state, config, maximum_budgets)
        started_at = time.monotonic()
        raw = _complete_graph_model(
            agent,
            configurable["router_model_client"],
            build_plan_prompt(
                state["task"],
                state["intent_context"],
                available_tools,
                maximum_budgets,
                retry=attempt > 1,
                expected_revision=expected_revision,
                previous_plan=state["plan"] if is_replan else None,
                replan_reason=state["replan_reason"] if is_replan else "",
                validation_error=(
                    f"{error_code}: {error_message}" if attempt > 1 and error_code else ""
                ),
                minimum_budgets=minimum_budgets,
            ),
            PLANNER_MAX_NEW_TOKENS,
            stage="planning",
            deadline_monotonic=planning_deadline,
        )
        try:
            plan = parse_and_validate_plan(
                raw,
                available_tools=available_tools,
                maximum_budgets=maximum_budgets,
                expected_revision=expected_revision,
                expected_plan_id=expected_plan_id,
                minimum_budgets=minimum_budgets,
            )
        except PlanValidationError as exc:
            error_code = exc.code
            error_message = str(exc)
            agent.current_task_state.record_malformed_output_recovered()
            _write_graph_task_state(agent)
            agent.emit_trace(
                agent.current_task_state,
                "plan_rejected",
                {
                    "attempt": attempt,
                    "error_code": error_code,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "completion_metadata": _safe_completion_metadata(
                        agent, configurable["router_model_client"]
                    ),
                },
            )
            continue

        task_state = agent.current_task_state
        task_state.plan_id = plan["plan_id"]
        task_state.plan_revision = plan["revision"]
        task_state.intent = plan["intent"]
        metadata_collector.update(
            {
                "resolved_intent": plan["intent"],
                "intent_source": "plan",
                "intent_attempts": 0,
            }
        )
        task_state.checklist = [step["goal"] for step in plan["steps"]]
        task_state.done_when = [item for step in plan["steps"] for item in step["done_when"]]
        task_state.completed_items = []
        _write_graph_task_state(agent)
        agent.emit_trace(
            task_state,
            "plan_created",
            {
                "plan_id": plan["plan_id"],
                "revision": plan["revision"],
                "intent": plan["intent"],
                "summary": plan["summary"],
                "step_count": len(plan["steps"]),
                "risk_level": plan["risk_level"],
                "steps": [
                    {
                        "id": step["id"],
                        "goal": step["goal"],
                        "dependencies": list(step["dependencies"]),
                        "done_when": list(step["done_when"]),
                    }
                    for step in plan["steps"]
                ],
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "replan_reason": state["replan_reason"] if is_replan else "",
                "completion_metadata": _safe_completion_metadata(
                    agent, configurable["router_model_client"]
                ),
            },
        )
        return {
            **state,
            "acceptance": "\n".join(plan["acceptance"]),
            "resolved_intent": plan["intent"],
            "intent_source": "plan",
            "plan": plan,
            "plan_history": [
                *state["plan_history"],
                {
                    **deepcopy(state["plan"]),
                    "replan_reason": state["replan_reason"],
                },
            ]
            if is_replan
            else state["plan_history"],
            "plan_attempts": attempt,
            "plan_error": "",
            "replan_requested": False,
            "replan_reason": "",
            "replan_attempts": state["replan_attempts"] + (1 if is_replan else 0),
        }

    return _failed_state(
        {
            **state,
            "plan_attempts": MAX_PLAN_ATTEMPTS,
            "plan_error": error_code,
        },
        "retry_limit_reached",
        f"Planning failed ({error_code}): {error_message}",
    )


def _run_isolated_executor(executor, prompt, config, *, collect_answer_attempts=False):
    started_at = time.monotonic()
    error = None
    try:
        return executor.ask(prompt)
    except Exception as exc:
        error = exc
        if hasattr(exc, "at_stage"):
            exc.at_stage("execute")
        raise
    finally:
        task_state = executor.current_task_state
        if error is not None and task_state is not None:
            finalize_failed_run(
                executor,
                task_state,
                error_type=type(error).__name__,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                stop_reason=getattr(error, "stop_reason", STOP_REASON_RUNTIME_ERROR),
            )
        if task_state is not None:
            configurable = config["configurable"]
            configurable["node_child_states"].append(task_state)
            if collect_answer_attempts:
                configurable["run_metadata_collector"]["answer_attempts"] = task_state.attempts


def _resolve_research(intent, proposed, override):
    if intent == INTENT_CONVERSATION:
        return False
    if override is not None:
        return bool(override)
    return bool(proposed)


def _classify_auto_intent(agent, router_client, metadata_collector, task, context):
    malformed_attempts = 0
    for attempt in range(1, MAX_INTENT_ATTEMPTS + 1):
        _record_graph_model_attempt(
            agent,
            metadata_collector,
            event="intent_classification_requested",
            attempt=attempt,
            counter_key="intent_attempts",
        )
        started_at = time.monotonic()
        raw = _complete_graph_model(
            agent,
            router_client,
            build_intent_prompt(task, context, retry=attempt > 1),
            ROUTER_MAX_NEW_TOKENS,
            stage="intent",
        )
        protocol_status = "valid"
        try:
            intent, requires_research = parse_intent_output(raw)
        except ValueError:
            protocol_status = "malformed"
            intent = ""
            requires_research = False
        agent.emit_trace(
            agent.current_task_state,
            "intent_classification_completed",
            {
                "attempt": attempt,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "protocol_status": protocol_status,
                "completion_metadata": _safe_completion_metadata(agent, router_client),
            },
        )
        if protocol_status == "valid":
            return IntentDecision(
                intent=intent,
                requires_research=requires_research,
                source="router",
                attempts=attempt,
                malformed_attempts=malformed_attempts,
            )
        malformed_attempts += 1
        agent.current_task_state.record_malformed_output_recovered()
        _write_graph_task_state(agent)

    return IntentDecision(
        intent="",
        requires_research=False,
        source="router_failed",
        attempts=MAX_INTENT_ATTEMPTS,
        malformed_attempts=malformed_attempts,
    )


def _apply_initial_plan(
    state: AgentState,
    *,
    agent,
    model_client,
    metadata_collector,
    plan,
    attempt,
    source,
    intent_attempts,
    requires_research,
    duration_ms,
):
    task_state = agent.current_task_state
    task_state.plan_id = plan["plan_id"]
    task_state.plan_revision = plan["revision"]
    task_state.intent = plan["intent"]
    task_state.checklist = [step["goal"] for step in plan["steps"]]
    task_state.done_when = [item for step in plan["steps"] for item in step["done_when"]]
    task_state.completed_items = []
    _write_graph_task_state(agent)
    metadata_collector.update(
        {
            "resolved_intent": plan["intent"],
            "intent_source": source,
            "intent_attempts": intent_attempts,
        }
    )
    agent.emit_trace(
        task_state,
        "plan_created",
        {
            "plan_id": plan["plan_id"],
            "revision": plan["revision"],
            "intent": plan["intent"],
            "summary": plan["summary"],
            "step_count": len(plan["steps"]),
            "risk_level": plan["risk_level"],
            "steps": [
                {
                    "id": step["id"],
                    "goal": step["goal"],
                    "dependencies": list(step["dependencies"]),
                    "done_when": list(step["done_when"]),
                }
                for step in plan["steps"]
            ],
            "duration_ms": duration_ms,
            "replan_reason": "",
            "completion_metadata": _safe_completion_metadata(agent, model_client),
        },
    )
    return {
        **state,
        "acceptance": "\n".join(plan["acceptance"]),
        "resolved_intent": plan["intent"],
        "intent_source": source,
        "intent_attempts": intent_attempts,
        "requires_research": requires_research,
        "plan": plan,
        "plan_attempts": attempt,
        "plan_error": "",
        "replan_requested": False,
        "replan_reason": "",
        "router_direct_answer": False,
    }


def _route_and_plan_initial_task(state: AgentState, config: RunnableConfig) -> AgentState:
    """Route the request and, when needed, validate its initial execution plan in one call."""
    configurable = config["configurable"]
    agent = configurable["agent"]
    metadata_collector = configurable["run_metadata_collector"]
    mode = state["requested_task_mode"]
    available_tools = tuple(name for name in (agent.allowed_tools or agent.tools) if name != "delegate")
    maximum_budgets = _plan_limits(state, agent)
    minimum_budgets = _plan_minimum_budgets(state, config, maximum_budgets)
    error_message = ""
    planning_deadline = time.monotonic() + float(
        configurable.get("planning_deadline_seconds", PLANNING_DEADLINE_SECONDS)
    )

    for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
        _record_graph_model_attempt(
            agent,
            metadata_collector,
            event="intent_classification_requested",
            attempt=attempt,
            counter_key="intent_attempts",
        )
        started_at = time.monotonic()
        raw = _complete_graph_model(
            agent,
            configurable["router_model_client"],
            build_routed_planning_prompt(
                state["task"],
                state["intent_context"],
                available_tools,
                maximum_budgets,
                continuation_context=state["continuation_context"],
                requested_mode=mode,
                retry=attempt > 1,
                validation_error=error_message,
                minimum_budgets=minimum_budgets,
            ),
            ROUTER_PLAN_MAX_NEW_TOKENS,
            stage="planning",
            deadline_monotonic=planning_deadline,
        )
        duration_ms = int((time.monotonic() - started_at) * 1000)
        protocol_status = "valid"
        source = "router_plan"
        decision = None
        plan = None
        direct_answer = ""
        error_message = ""
        try:
            decision = parse_routed_task_output(raw)
            if decision.mode == ROUTE_MODE_DIRECT:
                if state["continuation_context"]:
                    raise ValueError("continuation requests must be planned")
                if mode != TASK_MODE_AUTO and mode != INTENT_CONVERSATION:
                    raise ValueError("direct route conflicts with explicit task mode")
            else:
                plan = parse_and_validate_plan(
                    json.dumps(decision.plan),
                    available_tools=available_tools,
                    maximum_budgets=maximum_budgets,
                    minimum_budgets=minimum_budgets,
                )
                if plan["intent"] != decision.intent:
                    raise PlanValidationError("route_plan_intent_mismatch", "route intent must match plan intent")
            if decision.mode == ROUTE_MODE_DIRECT:
                direct_answer = decision.answer
        except (ValueError, PlanValidationError) as route_error:
            # Existing stored and test fixtures use the pre-route bare plan contract. Keep it
            # readable during the migration, but all new model prompts use the routed contract.
            try:
                plan = parse_and_validate_plan(
                    raw,
                    available_tools=available_tools,
                    maximum_budgets=maximum_budgets,
                    minimum_budgets=minimum_budgets,
                )
                if state["continuation_context"] and plan["intent"] == INTENT_CONVERSATION:
                    raise PlanValidationError(
                        "continuation_plan_lost_capability",
                        "continuation plans must preserve workspace capability",
                    )
                decision = IntentDecision(
                    intent=plan["intent"],
                    requires_research=plan["intent"] != INTENT_CONVERSATION,
                    source="plan",
                )
                source = "plan"
            except PlanValidationError:
                try:
                    answer = parse_conversation_output(raw)
                except ValueError:
                    protocol_status = "malformed"
                    error_message = str(route_error)
                else:
                    if state["continuation_context"]:
                        protocol_status = "malformed"
                        error_message = "continuation requests must be planned"
                    elif mode != TASK_MODE_AUTO and mode != INTENT_CONVERSATION:
                        protocol_status = "malformed"
                        error_message = "direct route conflicts with explicit task mode"
                    else:
                        decision = IntentDecision(
                            intent=INTENT_CONVERSATION,
                            requires_research=False,
                            source="direct_conversation",
                        )
                        plan = None
                        direct_answer = answer
                        source = "direct_conversation"

        agent.emit_trace(
            agent.current_task_state,
            "intent_classification_completed",
            {
                "attempt": attempt,
                "duration_ms": duration_ms,
                "protocol_status": protocol_status,
                "completion_metadata": _safe_completion_metadata(
                    agent, configurable["router_model_client"]
                ),
            },
        )
        if protocol_status == "malformed":
            agent.current_task_state.record_malformed_output_recovered()
            _write_graph_task_state(agent)
            agent.emit_trace(
                agent.current_task_state,
                "intent_classification_rejected",
                {"attempt": attempt, "error_code": "invalid_route_contract"},
            )
            continue

        resolved_intent = decision.intent
        proposed_research = decision.requires_research
        if mode != TASK_MODE_AUTO and resolved_intent != mode:
            error_message = "route intent conflicts with the explicit task mode"
            agent.current_task_state.record_malformed_output_recovered()
            _write_graph_task_state(agent)
            continue

        if plan is None:
            answer = direct_answer
            task_state = agent.current_task_state
            task_state.intent = INTENT_CONVERSATION
            task_state.checklist = []
            task_state.done_when = []
            task_state.completed_items = []
            _write_graph_task_state(agent)
            metadata_collector.update(
                {
                    "resolved_intent": INTENT_CONVERSATION,
                    "intent_source": source,
                    "intent_attempts": attempt,
                    "answer_attempts": 1,
                }
            )
            agent.emit_trace(
                task_state,
                "intent_classified",
                {
                    "requested_mode": mode,
                    "resolved_intent": INTENT_CONVERSATION,
                    "source": source,
                    "attempts": attempt,
                    "requires_research": False,
                },
            )
            if is_plain_conversation_request(state["task"]):
                agent.emit_trace(
                    task_state,
                    "plan_skipped",
                    {"reason": "plain_conversation", "intent": INTENT_CONVERSATION},
                )
            agent.emit_trace(
                task_state,
                "answer_completed",
                {"intent": INTENT_CONVERSATION, "child_task_id": ""},
            )
            return {
                **state,
                "resolved_intent": INTENT_CONVERSATION,
                "intent_source": source,
                "intent_attempts": attempt,
                "answer_attempts": 1,
                "requires_research": False,
                "execution_result": answer,
                "completion_status": "success",
                "plan_attempts": 0,
                "router_direct_answer": True,
            }

        resolved_research = _resolve_research(
            resolved_intent,
            proposed_research,
            state["requires_research"],
        )
        execution_state = state
        if state["continuation_context"]:
            execution_state = {
                **state,
                "task": state["continuation_context"],
            }
        return _apply_initial_plan(
            execution_state,
            agent=agent,
            model_client=configurable["router_model_client"],
            metadata_collector=metadata_collector,
            plan=plan,
            attempt=attempt,
            source=source,
            intent_attempts=attempt,
            requires_research=resolved_research,
            duration_ms=duration_ms,
        )

    failed = {
        **state,
        "resolved_intent": "",
        "intent_source": "router_failed",
        "intent_attempts": MAX_PLAN_ATTEMPTS,
        "plan_attempts": MAX_PLAN_ATTEMPTS,
    }
    metadata_collector.update(
        {
            "resolved_intent": "",
            "intent_source": "router_failed",
            "intent_attempts": MAX_PLAN_ATTEMPTS,
        }
    )
    return _failed_state(
        failed,
        "retry_limit_reached",
        "Intent router did not return a valid route or execution plan.",
    )


def route_after_intent(state: AgentState):
    if state["terminal_reason"]:
        return "finalize"
    if state["router_direct_answer"]:
        return "finalize"
    intent = state["resolved_intent"]
    if intent == INTENT_CONVERSATION:
        return "answer"
    if state["requires_research"]:
        return "research"
    if intent == INTENT_READ_ONLY:
        return "answer"
    if intent == INTENT_CODE_CHANGE:
        return "execute_change"
    raise RuntimeError("unresolved task intent")


def intent_router_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config["configurable"]
    agent = configurable["agent"]
    metadata_collector = configurable["run_metadata_collector"]
    mode = state["requested_task_mode"]

    budget_failure = _budget_failure(state, config)
    if budget_failure is not None:
        route = route_after_intent(budget_failure)
        _emit_route(agent, "intent_router", route, "budget_exhausted")
        return budget_failure

    if state["terminal_reason"]:
        route = route_after_intent(state)
        _emit_route(agent, "intent_router", route, state["terminal_reason"])
        return state
    if state["planning_enabled"]:
        if not (state["plan"] and state["replan_attempts"]):
            next_state = _route_and_plan_initial_task(state, config)
            route = route_after_intent(next_state)
            _emit_route(
                agent,
                "intent_router",
                route,
                next_state["terminal_reason"] or next_state["resolved_intent"],
            )
            return next_state
        planned_intent = state["plan"].get("intent", "")
        decision = IntentDecision(
            intent=planned_intent,
            requires_research=planned_intent != INTENT_CONVERSATION,
            source=state["intent_source"] or "plan",
        )
    elif mode != TASK_MODE_AUTO:
        decision = IntentDecision(
            intent=mode,
            requires_research=mode != INTENT_CONVERSATION,
            source="explicit",
        )
    elif state["review_focus_paths"]:
        decision = IntentDecision(
            intent=INTENT_CODE_CHANGE,
            requires_research=True,
            source="focus_path",
        )
    else:
        decision = _classify_auto_intent(
            agent,
            configurable["router_model_client"],
            metadata_collector,
            state["task"],
            state["intent_context"],
        )

    resolved_research = False
    if decision.intent:
        resolved_research = _resolve_research(
            decision.intent,
            decision.requires_research,
            state["requires_research"],
        )
    next_state = {
        **state,
        "resolved_intent": decision.intent,
        "intent_source": decision.source,
        "intent_attempts": decision.attempts,
        "requires_research": resolved_research,
    }
    metadata_collector.update(
        {
            "resolved_intent": decision.intent,
            "intent_source": decision.source,
            "intent_attempts": decision.attempts,
        }
    )
    agent.current_task_state.intent = decision.intent
    _write_graph_task_state(agent)

    if not decision.intent:
        agent.emit_trace(
            agent.current_task_state,
            "intent_classification_failed",
            {"attempts": decision.attempts, "malformed_attempts": decision.malformed_attempts},
        )
        next_state = _failed_state(
            next_state,
            "retry_limit_reached",
            "Intent router did not return valid JSON; rerun with an explicit --task-mode.",
        )
    else:
        agent.emit_trace(
            agent.current_task_state,
            "intent_classified",
            {
                "requested_mode": mode,
                "resolved_intent": decision.intent,
                "source": decision.source,
                "attempts": decision.attempts,
                "requires_research": resolved_research,
            },
        )
        if decision.malformed_attempts:
            agent.emit_trace(
                agent.current_task_state,
                "intent_classification_recovered",
                {"malformed_attempts": decision.malformed_attempts},
            )
        agent.emit_progress(f"intent: {decision.intent} ({decision.source})")

    route = route_after_intent(next_state)
    _emit_route(
        agent,
        "intent_router",
        route,
        next_state["terminal_reason"] or next_state["resolved_intent"],
    )
    return next_state


def route_after_research(state: AgentState):
    if state["terminal_reason"]:
        return "finalize"
    if state["resolved_intent"] == INTENT_READ_ONLY:
        return "answer"
    if state["resolved_intent"] == INTENT_CODE_CHANGE:
        return "execute_change"
    raise RuntimeError("conversation must not enter research")


def research_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    budget_failure = _budget_failure(state, config)
    if budget_failure is not None:
        _emit_route(agent, "research_delegate", "finalize", "budget_exhausted")
        return budget_failure
    minimum_remaining = 2 if state["resolved_intent"] == INTENT_READ_ONLY else 3
    if state["step_budget"] - state["coordinator_steps_used"] < minimum_remaining:
        next_state = _failed_state(
            state,
            "budget_exhausted",
            "Coordinator step budget was exhausted.",
        )
    else:
        required_tools = _planned_read_tools(state, agent)
        observed_tools = _successful_read_tool_names(agent, config)
        missing_tools = tuple(name for name in required_tools if name not in observed_tools)
        call = {"ok": False, "text": "", "child": None}
        delegate_calls = 0
        for attempt in range(1, MAX_REQUIRED_TOOL_ATTEMPTS + 1):
            requirement = ""
            if missing_tools:
                requirement = (
                    "\nMANDATORY PLAN CONTRACT: call each required read-only tool before returning findings: "
                    + ", ".join(missing_tools)
                    + ". A prose-only response is invalid."
                )
            call = _call_graph_role_delegate(
                agent,
                RoleDelegateSpec(
                    role="research",
                    task=state["task"] + requirement,
                    allowed_tools=READ_ONLY_TOOLS,
                    # Leave one model round after the required tools so the
                    # delegate can package its findings in <final>. With
                    # list_files + read_file + search, a three-step child
                    # could exhaust its budget immediately after the last
                    # tool and be reported as malformed.
                    max_steps=min(12, max(4, len(missing_tools) + 1)),
                ),
            )
            delegate_calls += 1
            if not call["ok"] or not missing_tools:
                break
            observed_tools = _successful_read_tool_names(agent, config)
            missing_tools = tuple(name for name in required_tools if name not in observed_tools)
            if not missing_tools:
                break
            agent.emit_trace(
                agent.current_task_state,
                "required_tools_retry",
                {
                    "stage": "research",
                    "attempt": attempt,
                    "missing_tools": list(missing_tools),
                },
            )
        if not call["ok"]:
            next_state = {
                **state,
                "research_result": "research delegate failed; continue using workspace evidence",
                "delegate_failures": state["delegate_failures"] + 1,
                "coordinator_steps_used": state["coordinator_steps_used"] + delegate_calls,
            }
        else:
            next_state = {
                **state,
                "research_result": call["text"],
                "coordinator_steps_used": state["coordinator_steps_used"] + delegate_calls,
            }
    next_state = _budget_failure(next_state, config) or next_state
    route = route_after_research(next_state)
    _emit_route(
        agent,
        "research_delegate",
        route,
        next_state["terminal_reason"] or next_state["resolved_intent"],
    )
    return next_state


def _conversation_answer(state, config):
    configurable = config["configurable"]
    agent = configurable["agent"]
    metadata_collector = configurable["run_metadata_collector"]
    result = ""
    answer_attempts = 0

    for attempt in range(1, MAX_CONVERSATION_ATTEMPTS + 1):
        _begin_answer_candidate(agent)
        answer_attempts = attempt
        _record_graph_model_attempt(
            agent,
            metadata_collector,
            event="conversation_model_requested",
            attempt=attempt,
            counter_key="answer_attempts",
        )
        started_at = time.monotonic()
        raw = _complete_graph_model(
            agent,
            agent.model_client,
            build_conversation_prompt(
                state["task"],
                state["intent_context"],
                retry=attempt > 1,
            ),
            agent.max_new_tokens,
            stage="execute",
        )
        protocol_status = "valid"
        try:
            result = parse_conversation_output(raw)
        except ValueError:
            protocol_status = "malformed"
            result = ""
        agent.emit_trace(
            agent.current_task_state,
            "conversation_model_completed",
            {
                "attempt": attempt,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "protocol_status": protocol_status,
                "completion_metadata": _safe_completion_metadata(agent, agent.model_client),
            },
        )
        if result:
            break
        agent.current_task_state.record_malformed_output_recovered()
        _write_graph_task_state(agent)
        agent.emit_trace(
            agent.current_task_state,
            "conversation_protocol_rejected",
            {"attempt": attempt, "error_code": "invalid_answer_json"},
        )

    answer_state = {**state, "answer_attempts": answer_attempts}
    if not result:
        return _failed_state(
            answer_state,
            "retry_limit_reached",
            "Conversation model did not return a valid final answer.",
        )
    return {**answer_state, "execution_result": result, "completion_status": "success"}


def _read_only_answer(state, config):
    configurable = config["configurable"]
    agent = configurable["agent"]
    read_allowed = tuple(name for name in READ_ONLY_TOOLS if name in agent.tools)
    if not read_allowed:
        return _failed_state(
            state,
            "runtime_error",
            "No read-only tools are permitted by the parent agent.",
        )
    required_tools = _planned_read_tools(state, agent)
    initial_child_tool_steps = sum(
        child.tool_steps for child in _child_task_states(agent, config)
    )
    result = ""
    task_state = None
    for attempt in range(1, MAX_REQUIRED_TOOL_ATTEMPTS + 1):
        # coordinator_steps_used already includes tool calls from earlier answer
        # executors. Only reserve calls created by retries inside this invocation;
        # subtracting every historical child here charges the same tools twice.
        answer_tool_steps = max(
            0,
            sum(child.tool_steps for child in _child_task_states(agent, config))
            - initial_child_tool_steps,
        )
        remaining = (
            state["step_budget"]
            - state["coordinator_steps_used"]
            - answer_tool_steps
        )
        if remaining < 1:
            return _failed_state(
                state,
                "budget_exhausted",
                "Coordinator step budget was exhausted.",
            )
        observed_tools = _successful_read_tool_names(agent, config)
        missing_tools = tuple(name for name in required_tools if name not in observed_tools)
        require_evidence = state["planning_enabled"] and not observed_tools
        _begin_answer_candidate(agent)
        executor = _create_isolated_executor(
            agent,
            allowed_tools=read_allowed,
            read_only=True,
            approval_policy="never",
            max_steps=remaining,
        )
        result = _run_isolated_executor(
            executor,
            build_read_only_prompt(
                state["task"],
                state["intent_context"],
                state["research_result"],
                required_tools=missing_tools,
                require_tool_evidence=require_evidence,
                retry=attempt > 1,
                plan=state["plan"],
                review_feedback=(
                    state["review_issues"] if state["replan_attempts"] else ""
                ),
                previous_answer=(
                    state["execution_result"] if state["replan_attempts"] else ""
                ),
            ),
            config,
            collect_answer_attempts=True,
        )
        task_state = executor.current_task_state
        if task_state is not None and task_state.status != STATUS_COMPLETED:
            # A protocol/provider terminal is already authoritative. Retrying
            # the outer evidence gate would create a second hidden retry loop.
            break
        observed_tools = _successful_read_tool_names(agent, config)
        missing_tools = tuple(name for name in required_tools if name not in observed_tools)
        if not state["planning_enabled"] or (observed_tools and not missing_tools):
            break
        if attempt < MAX_REQUIRED_TOOL_ATTEMPTS:
            agent.emit_trace(
                agent.current_task_state,
                "required_tools_retry",
                {
                    "stage": "answer",
                    "attempt": attempt,
                    "missing_tools": list(missing_tools),
                    "evidence_found": bool(observed_tools),
                },
            )
    if task_state is None:
        return _failed_state(state, "runtime_error", "Read-only executor produced no task state.")
    answer_state = {
        **state,
        "answer_attempts": task_state.attempts,
        "coordinator_steps_used": state["coordinator_steps_used"]
        + sum(child.tool_steps for child in _child_task_states(agent, config))
        - initial_child_tool_steps,
    }
    if task_state.affected_paths:
        agent.emit_trace(
            agent.current_task_state,
            "capability_boundary_violated",
            {
                "intent": INTENT_READ_ONLY,
                "boundary": "affected_paths",
                "affected_paths": list(task_state.affected_paths),
            },
        )
        return _failed_state(
            answer_state,
            "runtime_error",
            "Read-only answer execution modified workspace state.",
        )
    if task_state.status != STATUS_COMPLETED:
        return _failed_state(
            answer_state,
            task_state.stop_reason or "runtime_error",
            result,
        )
    read_evidence = _successful_read_tool_names(agent, config)
    if state["planning_enabled"] and not read_evidence:
        return _failed_state(
            answer_state,
            "missing_current_run_evidence",
            "Read-only answer was rejected because this run produced no successful workspace evidence; "
            "the model did not execute any required read-only workspace tool.",
        )
    if state["planning_enabled"] and required_tools:
        missing_tools = [name for name in required_tools if name not in read_evidence]
        if missing_tools:
            return _failed_state(
                answer_state,
                "missing_current_run_evidence",
                "Required read-only tools were not executed: " + ", ".join(missing_tools),
            )
    if not str(result).strip():
        return _failed_state(answer_state, "runtime_error", "Answer executor returned an empty result.")
    return {
        **answer_state,
        "execution_result": result,
        "completion_status": "success",
    }


def answer_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    budget_failure = _budget_failure(state, config)
    if budget_failure is not None:
        return budget_failure
    if state["resolved_intent"] == INTENT_CONVERSATION:
        next_state = _conversation_answer(state, config)
    elif state["resolved_intent"] == INTENT_READ_ONLY:
        next_state = _read_only_answer(state, config)
    else:
        raise RuntimeError("answer node received code_change")

    next_state = _budget_failure(next_state, config) or next_state
    if next_state["completion_status"] == "success":
        child_task_id = ""
        if state["resolved_intent"] == INTENT_READ_ONLY:
            children = config["configurable"]["node_child_states"]
            child_task_id = children[-1].task_id if children else ""
        agent.emit_trace(
            agent.current_task_state,
            "answer_completed",
            {"intent": state["resolved_intent"], "child_task_id": child_task_id},
        )
        agent.emit_progress(f"answer completed: {state['resolved_intent']}")
    return next_state


def route_after_answer(state: AgentState):
    if state["terminal_reason"]:
        return "finalize"
    if state["resolved_intent"] == INTENT_CONVERSATION:
        return "finalize"
    if state["resolved_intent"] == INTENT_READ_ONLY:
        return "review"
    return "finalize"


def route_after_execute_change(state: AgentState):
    if state["replan_requested"] and not state["terminal_reason"]:
        return "replan"
    return "finalize" if state["terminal_reason"] else "review"


def execute_change_node(state: AgentState, config: RunnableConfig) -> AgentState:
    if state["resolved_intent"] != INTENT_CODE_CHANGE:
        raise RuntimeError("execute_change received a non-code intent")
    agent = config["configurable"]["agent"]
    budget_failure = _budget_failure(state, config)
    if budget_failure is not None:
        _emit_route(agent, "execute_change", "finalize", "budget_exhausted")
        return budget_failure
    remaining = state["step_budget"] - state["coordinator_steps_used"]
    if remaining <= 1:
        next_state = _failed_state(
            state,
            "budget_exhausted",
            "Coordinator step budget was exhausted.",
        )
    else:
        executor_budget = remaining - 1
        exec_allowed = tuple(name for name in agent.tools if name != "delegate")
        if not exec_allowed:
            next_state = _failed_state(
                state,
                "runtime_error",
                "No executable tools are permitted by the parent agent.",
            )
        else:
            _begin_answer_candidate(agent)
            executor = _create_isolated_executor(
                agent,
                allowed_tools=exec_allowed,
                read_only=False,
                approval_policy=agent.approval_policy,
                max_steps=executor_budget,
            )
            review_context = ""
            fix_attempts = state["fix_attempts"]
            if state["review_status"] == "needs_fix":
                fix_attempts += 1
                agent.emit_trace(
                    agent.current_task_state,
                    "review_retry_started",
                    {"backend": "langgraph", "attempt": fix_attempts},
                )
                review_context = "\n\nReview issues to fix:\n" + state["review_issues"]
            prompt = state["task"] + "\n\nResearch findings:\n" + state["research_result"]
            prompt += review_context
            result = _run_isolated_executor(executor, prompt, config)
            task_state = executor.current_task_state
            if task_state is None:
                raise RuntimeError("code-change executor produced no task state")
            affected = sorted(set(state["affected_paths"]) | set(task_state.affected_paths))
            review_focus_paths = affected or list(state["review_focus_paths"])
            updated = {
                **state,
                "execution_result": result,
                "affected_paths": affected,
                "review_focus_paths": review_focus_paths,
                "review_status": "",
                "review_issues": "",
                "fix_attempts": fix_attempts,
                "coordinator_steps_used": state["coordinator_steps_used"] + task_state.tool_steps,
            }
            write_evidence = [
                item
                for item in task_state.evidence
                if item.get("tool_name") in {"write_file", "patch_file", "run_shell"}
                and item.get("status") in {"ok", "partial_success"}
            ]
            if not state["planning_enabled"] and (affected or state["review_focus_paths"]):
                next_state = updated
            elif affected and write_evidence:
                next_state = updated
            else:
                no_change_result = str(result).strip()
                next_state = _failed_state(
                    updated,
                    "no_changes_to_review",
                    no_change_result
                    if no_change_result and not state["planning_enabled"]
                    else "No successful write evidence and reviewable path were produced.",
                )

    next_state = _budget_failure(next_state, config) or next_state
    route = route_after_execute_change(next_state)
    _emit_route(
        agent,
        "execute_change",
        route,
        next_state["terminal_reason"] or "review_path_ready",
    )
    return next_state


def route_finish_or_fix(state: AgentState):
    if state["terminal_reason"] or state["review_status"] == "pass":
        return "finalize"
    if state["review_status"] == "needs_fix":
        if state["planning_enabled"]:
            return "replan"
        if state["resolved_intent"] == INTENT_READ_ONLY:
            return "answer"
        return "execute_change"
    raise RuntimeError("review route received an unresolved status")


def _read_only_review_evidence(agent, config):
    evidence = []
    for child_state in _child_task_states(agent, config):
        for item in child_state.evidence:
            if item.get("tool_name") not in READ_ONLY_TOOLS:
                continue
            if item.get("status") not in {"ok", "partial_success"}:
                continue
            evidence.append(
                {
                    "tool_name": item.get("tool_name", ""),
                    "paths": list(item.get("relative_paths", [])),
                    "freshness": item.get("freshness", ""),
                    "summary": item.get("summary", ""),
                }
            )
    return evidence


def review_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    budget_failure = _budget_failure(state, config)
    if budget_failure is not None:
        _emit_route(agent, "review_delegate", "finalize", "budget_exhausted")
        return budget_failure
    agent.current_task_state.review_status = "running"
    _write_graph_task_state(agent)
    agent.emit_trace(
        agent.current_task_state,
        "review_started",
        {"attempt": state["fix_attempts"] + 1, "focus_paths": list(state["review_focus_paths"])},
    )
    if (
        state["resolved_intent"] == INTENT_CODE_CHANGE
        and state["step_budget"] - state["coordinator_steps_used"] < 1
    ):
        next_state = _failed_state(
            state,
            "budget_exhausted",
            "Coordinator step budget was exhausted.",
        )
    elif state["resolved_intent"] != INTENT_CODE_CHANGE:
        task_state = agent.current_task_state
        task_state.record_attempt()
        _write_graph_task_state(agent)
        evidence_prompt = ""
        if state["resolved_intent"] == INTENT_READ_ONLY:
            evidence_prompt = (
                "\nREAD_ONLY_EVIDENCE="
                + json.dumps(_read_only_review_evidence(agent, config), ensure_ascii=False)
                + "\nFor a read-only task, return status: needs_fix unless this current-run evidence "
                "is sufficient for the candidate answer."
            )
        raw = _complete_graph_model(
            agent,
            agent.model_client,
            (
                "Review this candidate answer against the request and acceptance criteria. "
                "Do not expose hidden reasoning. The first non-empty line must be exactly "
                "status: pass or status: needs_fix, followed by a concise issue summary.\n"
                f"REQUEST={state['task']}\n"
                f"ACCEPTANCE={state['acceptance']}\n"
                f"INTENT={state['resolved_intent']}\n"
                f"CANDIDATE={state['execution_result']}"
                + evidence_prompt
            ),
            agent.max_new_tokens,
            stage="review",
        )
        review = normalize_review_result(raw)
        updated = {
            **state,
            "review_status": review["status"],
            "review_issues": review["text"],
        }
        task_state.review_status = review["status"]
        if review["recovered"]:
            task_state.record_malformed_output_recovered()
        _write_graph_task_state(agent)
        agent.emit_trace(
            task_state,
            "review_completed",
            {
                "status": review["status"],
                "attempt": state["fix_attempts"] + 1,
                "issue_count": len(review.get("issue_codes", [])),
            },
        )
        review_attempt = state["fix_attempts"] + 1
        if review["status"] == "pass":
            next_state = {**updated, "completion_status": "success"}
        elif state["planning_enabled"] and state["replan_attempts"] >= MAX_REPLAN_ATTEMPTS:
            next_state = _failed_state(
                updated,
                "review_retry_limit_reached",
                "自动审查未通过，已达到重试上限。请查看运行详情中的审查记录后重试。",
            )
        elif not state["planning_enabled"] and review_attempt >= MAX_FIX_ATTEMPTS:
            next_state = _failed_state(
                {**updated, "fix_attempts": review_attempt},
                "review_retry_limit_reached",
                "Read-only review did not confirm that the requested result was complete.",
            )
        else:
            next_state = {
                **updated,
                "fix_attempts": review_attempt,
                "replan_requested": bool(state["planning_enabled"]),
                "replan_reason": review["text"] if state["planning_enabled"] else "",
            }
    else:
        call = _call_graph_role_delegate(
            agent,
            RoleDelegateSpec(
                role="review",
                task="Review whether the requested change is complete.",
                allowed_tools=("read_file", "search"),
                focus_paths=tuple(state["review_focus_paths"]),
                acceptance=state["acceptance"],
                context_summary=state["execution_result"],
                max_steps=3,
            ),
        )
        if not call["ok"]:
            next_state = _failed_state(
                {
                    **state,
                    "review_status": "",
                    "review_issues": "delegate_failed",
                    "delegate_failures": state["delegate_failures"] + 1,
                    "coordinator_steps_used": state["coordinator_steps_used"] + 1,
                },
                "delegate_failed",
                "Review delegate failed; result could not be verified.",
            )
        else:
            review = normalize_review_result(call["text"])
            if review["recovered"]:
                agent.current_task_state.record_malformed_output_recovered()
                _write_graph_task_state(agent)
            updated = {
                **state,
                "review_status": review["status"],
                "review_issues": review["text"],
                "coordinator_steps_used": state["coordinator_steps_used"] + 1,
            }
            agent.current_task_state.review_status = review["status"]
            _write_graph_task_state(agent)
            agent.emit_trace(
                agent.current_task_state,
                "review_completed",
                {
                    "status": review["status"],
                    "attempt": state["fix_attempts"] + 1,
                    "issue_count": len(review.get("issue_codes", [])),
                },
            )
            if review["status"] == "pass":
                next_state = {**updated, "completion_status": "success"}
            elif state["fix_attempts"] >= MAX_FIX_ATTEMPTS:
                next_state = _failed_state(
                    updated,
                    "review_retry_limit_reached",
                    "自动审查未通过，已达到重试上限。请查看运行详情中的审查记录后重试。",
                )
            else:
                next_state = {
                    **updated,
                    "replan_requested": bool(state["planning_enabled"]),
                    "replan_reason": review["text"] if state["planning_enabled"] else "",
                }

    if agent.current_task_state.review_status == "running":
        terminal_review_status = next_state["review_status"] or "failed"
        agent.current_task_state.review_status = terminal_review_status
        _write_graph_task_state(agent)
        agent.emit_trace(
            agent.current_task_state,
            "review_completed",
            {
                "status": terminal_review_status,
                "attempt": state["fix_attempts"] + 1,
                "issue_count": 0,
            },
        )

    next_state = _budget_failure(next_state, config) or next_state
    route = route_finish_or_fix(next_state)
    _emit_route(
        agent,
        "review_delegate",
        route,
        next_state["terminal_reason"] or next_state["review_status"],
    )
    return next_state


def finalize_node(state: AgentState) -> AgentState:
    if state["completion_status"] == "success":
        return {
            **state,
            "terminal_reason": "",
            "final_result": state["execution_result"],
        }
    if state["completion_status"] != "failed" or not state["terminal_reason"]:
        raise RuntimeError("finalize received a non-terminal graph state")
    if not state["final_result"]:
        raise RuntimeError("failed graph state has no final result")
    return state


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("prepare_plan", prepare_plan_node)
    builder.add_node("intent_router", intent_router_node)
    builder.add_node("research_delegate", research_node)
    builder.add_node("answer", answer_node)
    builder.add_node("execute_change", execute_change_node)
    builder.add_node("review_delegate", review_node)
    builder.add_node("replan", prepare_plan_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "intent_router")
    builder.add_conditional_edges(
        "intent_router",
        route_after_intent,
        {
            "answer": "answer",
            "research": "research_delegate",
            "execute_change": "execute_change",
            "finalize": "finalize",
        },
    )
    builder.add_conditional_edges(
        "research_delegate",
        route_after_research,
        {
            "answer": "answer",
            "execute_change": "execute_change",
            "finalize": "finalize",
        },
    )
    builder.add_conditional_edges(
        "answer",
        route_after_answer,
        {"review": "review_delegate", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "execute_change",
        route_after_execute_change,
        {"review": "review_delegate", "replan": "replan", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "review_delegate",
        route_finish_or_fix,
        {
            "answer": "answer",
            "execute_change": "execute_change",
            "replan": "replan",
            "finalize": "finalize",
        },
    )
    builder.add_edge("replan", "intent_router")
    builder.add_edge("finalize", END)
    return builder.compile()

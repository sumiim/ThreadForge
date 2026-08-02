"""Pure-data LangGraph orchestration for Pico's routed three-role workflow."""

from copy import deepcopy
import time
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from pico.delegates import RoleDelegateSpec, create_role_delegate, normalize_review_result
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
    ROUTER_MAX_NEW_TOKENS,
    TASK_MODE_AUTO,
    IntentDecision,
    build_conversation_prompt,
    build_intent_prompt,
    build_read_only_prompt,
    parse_conversation_output,
    parse_intent_output,
)


MAX_FIX_ATTEMPTS = 2
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
)


class AgentState(TypedDict):
    task: str
    acceptance: str
    requested_task_mode: str
    resolved_intent: str
    intent_source: str
    intent_attempts: int
    answer_attempts: int
    intent_context: str
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
        return {"ok": False, "text": "", "error_type": type(exc).__name__}


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
        max_steps=max_steps,
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth,
        max_depth=agent.max_depth,
        read_only=read_only,
        allowed_tools=allowed_tools,
        event_sink=agent.event_sink,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
        progress_callback=agent.progress_callback,
        feature_flags=agent.feature_flags,
        allow_checkpoint=False,
        allow_durable_memory_write=False,
    )


def _run_isolated_executor(executor, prompt, config, *, collect_answer_attempts=False):
    started_at = time.monotonic()
    error = None
    try:
        return executor.ask(prompt)
    except Exception as exc:
        error = exc
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
        raw = router_client.complete(
            build_intent_prompt(task, context, retry=attempt > 1),
            ROUTER_MAX_NEW_TOKENS,
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


def route_after_intent(state: AgentState):
    if state["terminal_reason"]:
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

    if mode != TASK_MODE_AUTO:
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
    minimum_remaining = 2 if state["resolved_intent"] == INTENT_READ_ONLY else 3
    if state["step_budget"] - state["coordinator_steps_used"] < minimum_remaining:
        next_state = _failed_state(
            state,
            "budget_exhausted",
            "Coordinator step budget was exhausted.",
        )
    else:
        call = _call_graph_role_delegate(
            agent,
            RoleDelegateSpec(
                role="research",
                task=state["task"],
                allowed_tools=READ_ONLY_TOOLS,
                max_steps=3,
            ),
        )
        if not call["ok"]:
            next_state = {
                **state,
                "research_result": "research delegate failed; continue using workspace evidence",
                "delegate_failures": state["delegate_failures"] + 1,
                "coordinator_steps_used": state["coordinator_steps_used"] + 1,
            }
        else:
            next_state = {
                **state,
                "research_result": call["text"],
                "coordinator_steps_used": state["coordinator_steps_used"] + 1,
            }
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
        answer_attempts = attempt
        _record_graph_model_attempt(
            agent,
            metadata_collector,
            event="conversation_model_requested",
            attempt=attempt,
            counter_key="answer_attempts",
        )
        started_at = time.monotonic()
        raw = agent.model_client.complete(
            build_conversation_prompt(
                state["task"],
                state["intent_context"],
                retry=attempt > 1,
            ),
            agent.max_new_tokens,
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
    remaining = state["step_budget"] - state["coordinator_steps_used"]
    if remaining < 1:
        return _failed_state(
            state,
            "budget_exhausted",
            "Coordinator step budget was exhausted.",
        )
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
        ),
        config,
        collect_answer_attempts=True,
    )
    task_state = executor.current_task_state
    if task_state is None:
        return _failed_state(state, "runtime_error", "Read-only executor produced no task state.")
    answer_state = {
        **state,
        "answer_attempts": task_state.attempts,
        "coordinator_steps_used": state["coordinator_steps_used"] + task_state.tool_steps,
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
    if not str(result).strip():
        return _failed_state(answer_state, "runtime_error", "Answer executor returned an empty result.")
    return {
        **answer_state,
        "execution_result": result,
        "completion_status": "success",
    }


def answer_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    if state["resolved_intent"] == INTENT_CONVERSATION:
        next_state = _conversation_answer(state, config)
    elif state["resolved_intent"] == INTENT_READ_ONLY:
        next_state = _read_only_answer(state, config)
    else:
        raise RuntimeError("answer node received code_change")

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


def route_after_execute_change(state: AgentState):
    return "finalize" if state["terminal_reason"] else "review"


def execute_change_node(state: AgentState, config: RunnableConfig) -> AgentState:
    if state["resolved_intent"] != INTENT_CODE_CHANGE:
        raise RuntimeError("execute_change received a non-code intent")
    agent = config["configurable"]["agent"]
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
            if review_focus_paths:
                next_state = updated
            else:
                next_state = _failed_state(
                    updated,
                    "no_changes_to_review",
                    str(result).strip() or "No reviewable path was produced.",
                )

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
        return "execute_change"
    raise RuntimeError("review route received an unresolved status")


def review_node(state: AgentState, config: RunnableConfig) -> AgentState:
    agent = config["configurable"]["agent"]
    if state["step_budget"] - state["coordinator_steps_used"] < 1:
        next_state = _failed_state(
            state,
            "budget_exhausted",
            "Coordinator step budget was exhausted.",
        )
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
            if review["status"] == "pass":
                next_state = {**updated, "completion_status": "success"}
            elif state["fix_attempts"] >= MAX_FIX_ATTEMPTS:
                next_state = _failed_state(
                    updated,
                    "review_retry_limit_reached",
                    review["text"],
                )
            else:
                next_state = updated

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
    builder.add_node("intent_router", intent_router_node)
    builder.add_node("research_delegate", research_node)
    builder.add_node("answer", answer_node)
    builder.add_node("execute_change", execute_change_node)
    builder.add_node("review_delegate", review_node)
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
    builder.add_edge("answer", "finalize")
    builder.add_conditional_edges(
        "execute_change",
        route_after_execute_change,
        {"review": "review_delegate", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "review_delegate",
        route_finish_or_fix,
        {"execute_change": "execute_change", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)
    return builder.compile()

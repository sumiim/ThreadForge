"""LangGraph backend adapter for Pico's public runtime and benchmark harness."""

import time
from pathlib import Path, PureWindowsPath

from pico.evaluation.backends import (
    BackendRunResult,
    HarnessModelClientAdapter,
    default_event_sink_factory,
)
from pico.evaluation.evaluator import _apply_task_setup
from pico.event_sink import CompositeSink, EventCollector
from pico.features import memory as memorylib
from pico.run_lifecycle import finalize_run
from pico.runtime import Pico
from pico.task_state import (
    STATUS_FAILED,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_DELEGATE_FAILED,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_NO_CHANGES_TO_REVIEW,
    STOP_REASON_PERSISTENCE_ERROR,
    STOP_REASON_RETRY_LIMIT_REACHED,
    STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
    STOP_REASON_RUNTIME_ERROR,
    STOP_REASON_STEP_LIMIT_REACHED,
    TaskState,
)
from pico.workspace import now

from .graph import build_graph
from .intent import (
    INTENT_CODE_CHANGE,
    INTENT_CONVERSATION,
    INTENT_READ_ONLY,
    TASK_MODE_AUTO,
    normalize_task_mode,
)

RUN_METADATA_KEYS = (
    "requested_task_mode",
    "resolved_intent",
    "intent_source",
    "intent_attempts",
    "answer_attempts",
)

STOP_REASON_MAP = {
    "budget_exhausted": STOP_REASON_BUDGET_EXHAUSTED,
    "delegate_failed": STOP_REASON_DELEGATE_FAILED,
    "no_changes_to_review": STOP_REASON_NO_CHANGES_TO_REVIEW,
    "review_retry_limit_reached": STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
    "retry_limit_reached": STOP_REASON_RETRY_LIMIT_REACHED,
    "step_limit_reached": STOP_REASON_STEP_LIMIT_REACHED,
    "runtime_error": STOP_REASON_RUNTIME_ERROR,
    "persistence_error": STOP_REASON_PERSISTENCE_ERROR,
    "missing_current_run_evidence": STOP_REASON_RUNTIME_ERROR,
    "completion_gate_failed": STOP_REASON_RUNTIME_ERROR,
}

MODEL_ERROR_MESSAGES = {
    "model_rate_limited": "模型服务当前请求过多，已自动重试，请稍后再试。",
    "model_timeout": "模型服务响应超时，已自动重试，请稍后再试。",
    "model_connection_error": "无法稳定连接模型服务，已自动重试，请检查网络后再试。",
    "model_server_error": "模型服务暂时不可用，已自动重试，请稍后再试。",
    "model_auth_error": "模型服务认证失败，请在 Worker 中重新配置 API 密钥。",
    "model_request_rejected": "模型服务拒绝了请求，请检查模型与推理强度配置。",
    "model_response_invalid": "模型服务返回了无法解析的响应，请稍后再试。",
    "model_provider_error": "模型服务返回错误，请检查供应商配置后再试。",
    "model_call_failed": "模型调用失败，请稍后再试。",
}


def _safe_execution_failure(exc):
    code = str(getattr(exc, "code", "") or "")
    if getattr(exc, "stop_reason", "") == STOP_REASON_MODEL_ERROR:
        code = code or "model_call_failed"
        return code, MODEL_ERROR_MESSAGES.get(code, MODEL_ERROR_MESSAGES["model_call_failed"])
    return "runtime_error", "Agent 运行失败，请稍后重试。"


def _initial_state_snapshot(agent):
    memory_state = agent.memory.to_dict()
    return {
        "initial_history_empty": len(agent.session["history"]) == 0,
        "initial_memory_empty": memorylib.is_effectively_empty(memory_state),
        "initial_task_summary_empty": not str(memory_state["working"]["task_summary"]).strip(),
        "initial_episodic_notes_empty": not memory_state["episodic_notes"],
    }


def _enrich_evidence(evidence, *, task_state, plan, intent, workspace_id, step_id=""):
    item = dict(evidence)
    item.update(
        {
            "run_id": task_state.run_id,
            "plan_id": str(plan.get("plan_id", "")),
            "step_id": step_id,
            "intent": str(intent),
            "workspace_id": str(workspace_id),
            "freshness": str(item.get("freshness", "current_run")),
            "sensitivity": str(item.get("sensitivity", "workspace")),
        }
    )
    item.setdefault("relative_paths", list(item.get("affected_paths", [])))
    return item


def _assign_evidence_steps(evidence_items, plan):
    observed_by_step = {
        str(step.get("id", "")): set()
        for step in plan.get("steps", [])
    }
    assigned = []
    for evidence in evidence_items:
        tool_name = str(evidence.get("tool_name", ""))
        step_id = ""
        for step in plan.get("steps", []):
            candidate = str(step.get("id", ""))
            if (
                tool_name in step.get("required_tools", [])
                and tool_name not in observed_by_step[candidate]
            ):
                step_id = candidate
                observed_by_step[candidate].add(tool_name)
                break
        assigned.append((evidence, step_id))
    return assigned


def _complete_satisfied_plan_steps(task_state, plan, *, final_answer, intent):
    successful = [
        item
        for item in task_state.evidence
        if item.get("status") in {"ok", "partial_success"}
    ]
    for step in plan.get("steps", []):
        required_tools = set(step.get("required_tools", []))
        observed_tools = {
            str(item.get("tool_name", ""))
            for item in successful
            if item.get("step_id") == step.get("id")
        }
        if (required_tools and required_tools <= observed_tools) or (
            not required_tools
            and intent == INTENT_CONVERSATION
            and str(final_answer).strip()
        ):
            task_state.complete_item(step.get("goal", ""))


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


def _intent_context(agent, max_messages=4, max_chars=2000):
    messages = [
        item
        for item in agent.session.get("history", [])
        if item.get("role") in {"user", "assistant"}
    ][-max_messages:]
    lines = [
        f"{item.get('role', '')}: {item.get('content', '')}"
        for item in messages
    ]
    return agent.redact_text("\n".join(lines))[-max_chars:]


def _validate_run_agent_args(
    *,
    task_mode,
    requires_research,
    raw_focus_paths,
    acceptance,
    router_model_client,
):
    if requires_research is not None and not isinstance(requires_research, bool):
        raise ValueError("requires_research must be a boolean or None")
    has_focus = bool(raw_focus_paths)
    has_acceptance = acceptance is not None and bool(str(acceptance).strip())
    if task_mode in {INTENT_CONVERSATION, INTENT_READ_ONLY} and has_focus:
        raise ValueError(f"focus_paths are not valid for task_mode={task_mode}")
    if task_mode in {INTENT_CONVERSATION, INTENT_READ_ONLY} and has_acceptance:
        raise ValueError(f"acceptance is only valid for auto or {INTENT_CODE_CHANGE}")
    if task_mode == INTENT_CONVERSATION and requires_research is True:
        raise ValueError("conversation tasks cannot require research")
    if task_mode != TASK_MODE_AUTO and router_model_client is not None:
        raise ValueError("router_model_client is only valid for task_mode=auto")


def run_agent(
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
):
    """Run the routed LangGraph workflow with an already configured Pico instance."""
    task_input = str(task_input).strip()
    if not task_input:
        raise ValueError("task_input must not be empty")
    normalized_mode = normalize_task_mode(task_mode)
    raw_focus_paths = _materialize_focus_paths(focus_paths)
    _validate_run_agent_args(
        task_mode=normalized_mode,
        requires_research=requires_research,
        raw_focus_paths=raw_focus_paths,
        acceptance=acceptance,
        router_model_client=router_model_client,
    )
    if isinstance(step_budget, bool):
        raise ValueError("step_budget must be a positive integer")
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

    if router_model_client is None or router_model_client is original_model_client:
        resolved_router_client = agent.model_client
    elif isinstance(router_model_client, HarnessModelClientAdapter):
        resolved_router_client = router_model_client
    else:
        resolved_router_client = HarnessModelClientAdapter(router_model_client)

    task_state = None
    node_child_states = []
    budget_task_states = []
    final_answer = ""
    result = None
    run_metadata_collector = {
        "requested_task_mode": normalized_mode,
        "resolved_intent": "",
        "intent_source": "",
        "intent_attempts": 0,
        "answer_attempts": 0,
    }
    if enable_planning:
        run_metadata_collector.update(
            {
                "planning_enabled": True,
                "plan_id": "",
                "plan_revision": 0,
                "plan_attempts": 0,
            }
        )
    try:
        if record_session:
            agent.memory.set_task_summary(task_input)
            agent.session["memory"] = agent.memory.to_dict()

        task_state = TaskState.create(
            run_id=str(run_id or agent.new_run_id()),
            task_id=str(task_id or agent.new_task_id()),
            user_request=task_input,
            max_tool_steps=agent.max_steps,
            max_read_files=agent.max_read_files,
            max_total_steps=agent.max_total_steps,
        )
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.child_task_states = []
        agent.emit_trace(
            task_state,
            "run_started",
            {"user_request": task_input, "backend": "langgraph"},
        )
        started_at = time.monotonic()
        budget_task_states = [task_state]
        graph_state = {
            "task": task_input,
            "workspace_id": str(workspace_id),
            "acceptance": str(acceptance or task_input),
            "requested_task_mode": normalized_mode,
            "resolved_intent": "",
            "intent_source": "",
            "intent_attempts": 0,
            "answer_attempts": 0,
            "intent_context": _intent_context(agent),
            "completion_status": "pending",
            "step_budget": step_budget,
            "coordinator_steps_used": 0,
            "delegate_failures": 0,
            "requires_research": requires_research,
            "research_result": "",
            "execution_result": "",
            "affected_paths": [],
            "review_focus_paths": review_paths,
            "review_status": "",
            "review_issues": "",
            "fix_attempts": 0,
            "terminal_reason": "",
            "final_result": "",
            "planning_enabled": bool(enable_planning),
            "plan": {},
            "plan_attempts": 0,
            "plan_error": "",
            "plan_history": [],
            "replan_requested": False,
            "replan_reason": "",
            "replan_attempts": 0,
            "started_monotonic": started_at,
        }

        try:
            result = build_graph().invoke(
                graph_state,
                config={
                    "configurable": {
                        "agent": agent,
                        "router_model_client": resolved_router_client,
                        "node_child_states": node_child_states,
                        "run_metadata_collector": run_metadata_collector,
                    }
                },
            )
            task_state.record_affected_paths(result["affected_paths"])
            all_children = []
            seen_child_runs = set()
            for child_state in [*agent.child_task_states, *node_child_states]:
                if child_state.run_id in seen_child_runs:
                    continue
                seen_child_runs.add(child_state.run_id)
                all_children.append(child_state)
            raw_evidence = [
                evidence
                for child_state in all_children
                for evidence in child_state.evidence
            ]
            for evidence, step_id in _assign_evidence_steps(raw_evidence, result["plan"]):
                task_state.record_evidence(
                    _enrich_evidence(
                        evidence,
                        task_state=task_state,
                        plan=result["plan"],
                        intent=result["resolved_intent"],
                        workspace_id=workspace_id,
                        step_id=step_id,
                    )
                )
            task_state.plan_id = str(result["plan"].get("plan_id", ""))
            task_state.plan_revision = int(result["plan"].get("revision", 0))
            task_state.plan_history = [
                *[dict(item) for item in result.get("plan_history", [])],
                dict(result["plan"]),
            ]
            task_state.replan_reasons = [
                str(item.get("replan_reason", ""))
                for item in task_state.plan_history
                if str(item.get("replan_reason", "")).strip()
            ]
            task_state.intent = result["resolved_intent"]
            task_state.review_status = result["review_status"]
            budget_task_states = [task_state, *node_child_states]
            measured_steps = sum(state.tool_steps for state in budget_task_states)
            if measured_steps != result["coordinator_steps_used"]:
                raise RuntimeError("graph budget counter drift")
            expected_metadata = {
                key: result[key]
                for key in RUN_METADATA_KEYS
                if key in result
            }
            if enable_planning:
                expected_metadata.update(
                    {
                        "planning_enabled": True,
                        "plan_id": task_state.plan_id,
                        "plan_revision": task_state.plan_revision,
                        "plan_attempts": result["plan_attempts"],
                    }
                )
                run_metadata_collector.update(
                    {
                        "plan_id": task_state.plan_id,
                        "plan_revision": task_state.plan_revision,
                        "plan_attempts": result["plan_attempts"],
                    }
                )
            if run_metadata_collector != expected_metadata:
                raise RuntimeError("graph run metadata drift")

            final_answer = result["final_result"]
            if enable_planning:
                _complete_satisfied_plan_steps(
                    task_state,
                    result["plan"],
                    final_answer=final_answer,
                    intent=result["resolved_intent"],
                )
            if result["completion_status"] == "success" and not result["terminal_reason"]:
                if (
                    enable_planning
                    and task_state.checklist
                    and set(task_state.completed_items) != set(task_state.checklist)
                ):
                    task_state.stop(
                        STOP_REASON_RUNTIME_ERROR,
                        status=STATUS_FAILED,
                        final_answer="任务未通过确定性完成门禁，请重试。",
                    )
                    task_state.record_error(
                        stage="completion_gate",
                        code="completion_gate_failed",
                    )
                else:
                    task_state.finish_success(final_answer)
            else:
                stop_reason = STOP_REASON_MAP[result["terminal_reason"]]
                task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
        except Exception as exc:
            error_code, final_answer = _safe_execution_failure(exc)
            stop_reason = getattr(exc, "stop_reason", STOP_REASON_RUNTIME_ERROR)
            if stop_reason not in {
                STOP_REASON_MODEL_ERROR,
                STOP_REASON_RUNTIME_ERROR,
                STOP_REASON_PERSISTENCE_ERROR,
            }:
                stop_reason = STOP_REASON_RUNTIME_ERROR
            task_state.record_error(
                stage=getattr(exc, "stage", "runtime"),
                code=error_code,
                retryable=getattr(exc, "retryable", False),
                attempts=getattr(exc, "attempts", 1),
            )
            task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
        finally:
            budget_task_states = [task_state, *node_child_states]
            if task_state.status == "running":
                task_state.stop(
                    STOP_REASON_RUNTIME_ERROR,
                    status=STATUS_FAILED,
                    final_answer=final_answer,
                )
            finalize_run(
                agent,
                task_state,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            final_answer = task_state.final_answer

        if record_session:
            agent.record({"role": "user", "content": task_input, "created_at": now()})
            agent.record({"role": "assistant", "content": final_answer, "created_at": now()})

        return BackendRunResult(
            task_state=task_state,
            final_answer=final_answer,
            agent=agent,
            child_task_states=[*agent.child_task_states, *node_child_states],
            budget_task_states=budget_task_states,
            initial_state=initial_state,
            events=collector.snapshot(),
            run_metadata=run_metadata_collector,
        )
    finally:
        agent.event_sink = original_sink
        agent.model_client = original_model_client


class LangGraphBackendRunner:
    def __init__(self, max_new_tokens=64, model_client_factory=None, event_sink_factory=None):
        self.max_new_tokens = int(max_new_tokens)
        self.model_client_factory = model_client_factory
        self.event_sink_factory = event_sink_factory or default_event_sink_factory

    def run_task(self, task, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        if "delegate" not in task["allowed_tools"]:
            raise ValueError("langgraph backend requires allowed_tools to include delegate")
        requires_research = task.get("requires_research", True)
        if not isinstance(requires_research, bool):
            raise ValueError("requires_research must be a boolean")
        if model_client is None and self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        if model_client is None:
            raise ValueError("LangGraphBackendRunner requires model_client or model_client_factory")

        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            allowed_tools=task["allowed_tools"],
            event_sink=self.event_sink_factory(run_store),
        )
        _apply_task_setup(agent, task, fixture_copy_root)
        explicit_focus = list(task.get("focus_paths") or [])
        explicit_artifact = str(task.get("artifact_path", "")).strip()
        review_paths = explicit_focus or ([explicit_artifact] if explicit_artifact else [])
        return run_agent(
            agent,
            task["prompt"],
            task_mode=INTENT_CODE_CHANGE,
            acceptance=task.get("acceptance", task["prompt"]),
            step_budget=int(task["step_budget"]),
            requires_research=requires_research,
            focus_paths=review_paths,
            record_session=False,
        )

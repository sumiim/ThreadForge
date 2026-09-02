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

import sys
import time
from pathlib import Path, PureWindowsPath

from pico.agent_loop import AgentLoop, _convergence_summary
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
    STATUS_STOPPED,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_NO_CHANGES_TO_REVIEW,
    STOP_REASON_REVIEW_RETRY_LIMIT_REACHED,
    STOP_REASON_RUNTIME_ERROR,
    STOP_REASON_USER_CANCELLED,
    TaskState,
)
from pico.workspace import now

from .intent import (
    INTENT_CODE_CHANGE,
    INTENT_CONVERSATION,
    INTENT_READ_ONLY,
    TASK_MODE_AUTO,
    normalize_task_mode,
)
from .planning import is_continuation_request, is_plain_conversation_request
from .review_gate import ReviewDecision  # noqa: F401  (re-export for callers)

# 与 backend.py 的 _safe_execution_failure 对齐的模型错误中文消息。
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

# §7.8.9 阶段 4：intent 硬顶预算已移除——步数预算由墙钟/token 硬顶接管，
# intent 分类（conversation/read_only/code_change）已取消（大取消最后一块）。

# §7.8.9 决策（2026-08-18）：无 max_steps turn 硬顶——收尾确定性 gate 的
# 预算传大值（保证 remaining_budget > 0，走停滞检测 + evidence/checklist
# 正常判定，而非「预算耗尽直接 pass」）。墙钟/token 硬顶才是真上限。
GATE_STEP_BUDGET_UNLIMITED = 100000

RUN_METADATA_KEYS = (
    "requested_task_mode",
    "resolved_intent",
    "intent_source",
    "intent_attempts",
    "answer_attempts",
)


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


def _run_native_loop(agent, task_input, *, task_id, run_id):
    """AgentLoop 顶层跑，返回 final_answer（原生循环内部已持久化）。"""
    loop = AgentLoop(agent)
    return loop.run(task_input, task_id=task_id, run_id=run_id)


def run_native(
    agent,
    task_input,
    *,
    acceptance=None,
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

    §7.8.9 修正（2026-08-18）：移除 ``step_budget`` 参数——行动预算由
    墙钟/token 硬顶 + AgentLoop 步数护栏（agent.max_steps）承接，外部不再
    注入步数预算。与 run_agent 同形契约中残留的预算传递链一并清理。
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
        plain_conversation = is_plain_conversation_request(task_input)
        if record_session and not plain_conversation:
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
            # 初始归属传入（仅作初始值；AgentLoop 会重建 task_state，最终归属见下方
            # loop 完成后对 agent.current_task_state 的回填）。
            session_id=str((agent.session or {}).get("id", "")),
            workspace_id=str(workspace_id or (agent.session or {}).get("workspace_id", "")),
        )
        agent.current_task_state = task_state
        # 明确的社交短句走无工作区、无工具的轻量路径。其余任务仍不做主观
        # intent 分类，由权限档、工具行为与证据决定执行方式。
        intent = INTENT_CONVERSATION if plain_conversation else normalized_mode
        run_metadata_collector.update(
            {
                "resolved_intent": intent,
                "intent_source": "removed",
                "intent_attempts": 0,
            }
        )
        # §7.8.9 阶段 4：步数预算由 AgentLoop 步数护栏（agent.max_steps）承接，
        # 外部不再注入；墙钟/token 硬顶兜底（AgentLoop 内）。
        # 不再有 step_budget 覆盖 agent.max_steps。

        # 原生单循环跑（AgentLoop 自己创建 TaskState 并挂到 agent.current_task_state）。
        agent.emit_trace(
            agent.current_task_state,
            "native_loop_started",
            {"intent": intent},
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
            # 归属回填：AgentLoop 会重建正式 TaskState，必须在此（而非 create 处）
            # 把 session_id/workspace_id 补到最终 task_state 上，否则落盘
            # （task_state.json/report.json）会丢失会话归属 → Web 历史接不上。
            session_id = str((agent.session or {}).get("id", "") or "")
            ws_id = str(
                workspace_id or (agent.session or {}).get("workspace_id", "") or ""
            )
            task_state.session_id = session_id
            task_state.workspace_id = ws_id

        # 收尾 review gate（写触发，确定性兜底）。
        # §7.8.9 决策（2026-08-18）：无 max_steps turn 硬顶——gate 不再用
        # max_steps 当预算（否则 remaining_budget 恒 0 直接 pass，门禁失效），
        # 改传大值让 gate 走正常判定（停滞检测 + evidence/checklist 检查）。
        # 墙钟/token 硬顶已在循环内兜底。
        if (
            intent != INTENT_CONVERSATION
            and task_state is not None
            and task_state.status == "completed"
            and task_state.final_answer
        ):
            decision = run_review_gate(
                task_state,
                intent=intent,
                step_budget=GATE_STEP_BUDGET_UNLIMITED,
                coordinator_steps_used=int(getattr(task_state, "tool_steps", 0) or 0),
                step_budget_explicit=False,
                hard_cap=0,
            )
            if decision.status == "needs_fix":
                # 确定性完成门禁未通过：标记 review_status + blocked 语义。
                # 按工具行为（有写/shell 但无验证）→ no_changes_to_review；
                # 其余（checklist 未完成等）→ review_retry_limit_reached。
                has_write_evidence = any(
                    item.get("status") in {"ok", "partial_success"}
                    and not item.get("read_only", True)
                    for item in (getattr(task_state, "evidence", None) or [])
                )
                task_state.review_status = "needs_fix"
                task_state.record_error(
                    stage="completion_gate",
                    code="completion_gate_failed",
                )
                stop_reason = (
                    STOP_REASON_NO_CHANGES_TO_REVIEW
                    if not has_write_evidence
                    else STOP_REASON_REVIEW_RETRY_LIMIT_REACHED
                )
                task_state.stop(
                    stop_reason,
                    status=STATUS_STOPPED,
                    final_answer=task_state.final_answer,
                )
                final_answer = task_state.final_answer
        if task_state is not None:
            run_metadata_collector["answer_attempts"] = int(getattr(task_state, "attempts", 0))
    except RunCancelled:
        # AgentLoop 可能已创建正式 TaskState；异常时取当前 state。
        if agent.current_task_state is not None:
            agent.current_task_state.stop_user_cancelled()
        final_answer = ""
    except Exception as exc:
        # 异常可能发生在 AgentLoop 内（它创建了自己的 TaskState）或 intent 阶段
        # （临时 TaskState）。统一用 agent.current_task_state 处理。
        task_state = agent.current_task_state
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
            # 模型错误给可读中文消息（对齐 run_agent 的 _safe_execution_failure）。
            if stop_reason == STOP_REASON_MODEL_ERROR:
                final_answer = MODEL_ERROR_MESSAGES.get(
                    error_code, MODEL_ERROR_MESSAGES["model_call_failed"]
                )
            elif not final_answer:
                # 运行时/内部错误兜底：若已收集证据或被 review 驳回过候选回答，
                # 先走收敛总结（LLM 整合证据），失败再回退 best-effort 证据列表，
                # 而不是只给裸的「Agent 运行失败，请稍后重试。」浪费中间产出。
                if task_state.evidence or task_state.rejected_finals:
                    final_answer = _convergence_summary(
                        agent,
                        task_state,
                        "运行时出现未预期错误（runtime_error）",
                    )
                else:
                    final_answer = "Agent 运行失败，请稍后重试。"
            task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final_answer)
            final_answer = task_state.final_answer
            # 持久化助手最终消息：与 AgentLoop 预算/收敛路径一致，让失败的 run
            # 在刷新后的历史里也保留可读的收尾（托底总结），而不是只剩 user 提问。
            if final_answer:
                agent.record(
                    {"role": "assistant", "content": final_answer, "created_at": now()}
                )
    finally:
        task_state = agent.current_task_state or task_state
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

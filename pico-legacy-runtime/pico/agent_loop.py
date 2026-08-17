"""Agent control loop extracted from the runtime facade."""

import time
import uuid

from .checkpoint import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from .evaluation.review_gate import STAGNATION_ROUNDS_LIMIT, _round_signature
from .execution_hooks import ProcessCleanupFailed, RunCancelled
from .task_state import (
    PHASE_ACT_OR_ANSWER,
    PHASE_FINAL,
    PHASE_GATHER_CONTEXT,
    PHASE_UNDERSTAND_REQUEST,
    PHASE_VERIFY,
    STATUS_FAILED,
    STOP_REASON_PROCESS_CLEANUP_FAILED,
    TaskState,
)
from .tool_executor import ToolExecutionResult
from .tools import provider_tool_definitions
from .workspace import clip, now


def _new_tool_call_id():
    return "call_" + uuid.uuid4().hex


def _best_effort_step_limit(task_state, reason):
    """预算/重试耗尽时的可读收尾：列出已收集证据，而非一句英文裸失败。"""
    evidence = list(task_state.evidence or [])
    header = f"⚠️ 运行中断：{reason}，未能在预算内产出最终结论。"
    if not evidence:
        return header + " 本轮未成功读取任何工作区证据，建议缩小请求范围后重试。"
    lines = []
    for item in evidence[-12:]:
        tool = str(item.get("tool_name", "tool"))
        paths = item.get("relative_paths") or []
        path_text = "、".join(str(path) for path in paths[:4])
        lines.append(f"- {tool}：{path_text or '（无路径）'}")
    return (
        header
        + " 本轮已收集的部分证据：\n"
        + "\n".join(lines)
        + "\n\n请缩小范围、换一个更具体的请求，或提高预算后重试。"
    )


MAX_CONSECUTIVE_TALKS = 2
MAX_PROTOCOL_REPAIRS = 1


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message, *, task_id=None, run_id=None):
        agent = self.agent
        token = agent.cancellation_token
        hooks = agent.execution_hooks
        agent.child_task_states = []
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(
            run_id=run_id or agent.new_run_id(),
            task_id=task_id or agent.new_task_id(),
            user_request=user_message,
            max_tool_steps=agent.max_steps,
            max_read_files=agent.max_read_files,
            max_total_steps=agent.max_total_steps,
        )
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.emit_progress(f"run {task_state.run_id} started")
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )
        task_state.set_phase(
            PHASE_UNDERSTAND_REQUEST,
            next_step="Gather the minimum workspace context",
            completed_item="Understand the request and acceptance criteria",
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "run_started")
        task_state.set_phase(PHASE_GATHER_CONTEXT, next_step="Inspect the workspace only when evidence is needed")
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "context_requested")

        try:
            return self._run_loop(
                task_state,
                user_message,
                run_started_at=run_started_at,
                token=token,
                hooks=hooks,
            )
        except ProcessCleanupFailed:
            task_state.stop(
                STOP_REASON_PROCESS_CLEANUP_FAILED,
                status=STATUS_FAILED,
                final_answer="agent run failed: shell process cleanup could not be confirmed",
            )
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": task_state.final_answer,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            return task_state.final_answer
        except RunCancelled:
            # 取消收敛：不向调用方抛出，最终回答一律以 TaskState.final_answer 为准。
            task_state.stop_user_cancelled()
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": "",
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            agent.emit_progress(f"run {task_state.run_id} cancelled")
            return ""

    def _run_loop(self, task_state, user_message, *, run_started_at, token, hooks):
        agent = self.agent
        tool_steps = 0
        attempts = 0
        consecutive_talks = 0
        protocol_repairs = 0
        protocol_feedback = ""
        protocol_failed = False
        finalization_attempted = False
        tool_definitions = provider_tool_definitions(agent.tools)
        max_attempts = min(
            max(agent.max_steps * 3, agent.max_steps + 4),
            task_state.max_total_steps,
        )
        # §7.8.7-③ 停滞检测：连续 K 轮「证据/checklist/工具集」三信号零增量 → 强制收敛。
        # 轮 = 一次模型决策 turn（含其后的工具执行）。签名取自本轮执行完后的
        # task_state，下一轮开头比较：同签名 → 停滞轮 +1，否则归零。
        last_round_signature = ""
        stagnation_rounds = 0

        # 边界 1：进入控制循环前。
        token.raise_if_cancelled()

        while attempts < max_attempts and (
            tool_steps < agent.max_steps or not finalization_attempted
        ):
            # 边界 2：每轮开始、构建 prompt 前。
            token.raise_if_cancelled()
            # 停滞检测（滞后一轮比较：本轮开始时的签名 = 上一轮执行完后的状态）。
            # 达到阈值 → 强制进入 finalization，不再允许新工具。
            current_signature = _round_signature(task_state)
            if last_round_signature and current_signature == last_round_signature:
                stagnation_rounds += 1
            else:
                stagnation_rounds = 0
            last_round_signature = current_signature
            if stagnation_rounds >= STAGNATION_ROUNDS_LIMIT:
                task_state.record_malformed_output_recovered()
                task_state.set_phase(
                    PHASE_ACT_OR_ANSWER,
                    next_step="Converge on a grounded final answer using the collected evidence",
                )
                agent.emit_trace(
                    task_state,
                    "stagnation_detected",
                    {
                        "error_code": "stagnation",
                        "limit": STAGNATION_ROUNDS_LIMIT,
                        "signature": current_signature,
                    },
                )
                finalization_only = True
                finalization_attempted = True
            else:
                finalization_only = tool_steps >= agent.max_steps
                if finalization_only:
                    finalization_attempted = True
            attempts += 1
            task_state.record_attempt()
            task_state.set_phase(PHASE_ACT_OR_ANSWER, next_step="Choose a tool or prepare a final answer")
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "model_decision")
            agent.emit_progress(f"step {attempts}: building prompt")
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            if protocol_feedback:
                prompt += (
                    "\n\nRuntime control feedback:\n"
                    f"{protocol_feedback}\n"
                    "This feedback is control-plane input, not an assistant message."
                )
                prompt_metadata["runtime_protocol_feedback"] = True
                prompt_metadata["prompt_chars"] = len(prompt)
            if finalization_only:
                prompt += (
                    "\n\nRuntime control feedback:\n"
                    "The tool-call budget is exhausted. Do not call another tool. "
                    "Use the evidence already collected and return a grounded final answer now."
                )
                prompt_metadata["runtime_finalization_only"] = True
                prompt_metadata["prompt_chars"] = len(prompt)
            agent.emit_progress(f"step {attempts}: prompt ready ({prompt_metadata.get('prompt_chars', len(prompt))} chars)")
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if agent.allow_checkpoint and prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif agent.allow_checkpoint and prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if agent.allow_checkpoint and prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "finalization_only": finalization_only,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            # 边界 3：模型调用前（hook 在 RunGate 内检查取消并发布 model.started）。
            token.raise_if_cancelled()
            hooks.before_model(task_state)
            agent.emit_progress(f"step {attempts}: waiting for model response")
            raw = agent.model_client.complete(
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                on_retry=lambda details: getattr(
                    hooks, "model_retrying", lambda *_args: None
                )(task_state, "execute", details),
                on_text_delta=lambda delta: getattr(
                    hooks, "model_text_delta", lambda *_args: None
                )(task_state, "execute", delta),
                tool_definitions=() if finalization_only else tool_definitions,
            )
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            task_state.record_model_usage(completion_metadata)
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            # 边界 4：模型返回后、解析/执行工具前；取消时丢弃迟到响应。
            token.raise_if_cancelled()
            hooks.after_model(task_state, completion_metadata)
            kind, payload = agent.parse(raw)
            if kind == "final" and agent.is_deferred_action_answer(payload):
                kind = "retry"
                payload = agent.retry_notice(
                    "model announced future work instead of performing it"
                )
            response_diagnostics = agent.diagnose_response_shape(raw) if kind == "retry" else {}
            if kind != "retry":
                protocol_feedback = ""
            agent.emit_progress(f"step {attempts}: model returned {kind}")
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                    **response_diagnostics,
                },
            )

            if finalization_only and kind != "final":
                task_state.record_malformed_output_recovered()
                agent.emit_trace(
                    task_state,
                    "finalization_protocol_rejected",
                    {
                        "kind": kind,
                        "error_code": "tool_budget_exhausted",
                        "tool_steps": tool_steps,
                        "max_tool_steps": agent.max_steps,
                    },
                )
                break

            if kind == "talk":
                if consecutive_talks >= MAX_CONSECUTIVE_TALKS:
                    task_state.record_malformed_output_recovered()
                    task_state.set_phase(
                        PHASE_ACT_OR_ANSWER,
                        next_step="Choose a tool or submit a grounded final answer",
                    )
                    agent.emit_trace(
                        task_state,
                        "talk_rejected",
                        {"error_code": "consecutive_talk_limit", "limit": MAX_CONSECUTIVE_TALKS},
                    )
                else:
                    consecutive_talks += 1
                    task_state.record_talk()
                    hooks.commentary(task_state, str(payload))
                    agent.emit_trace(
                        task_state,
                        "assistant_commentary",
                        {"text": clip(str(payload), 1000), "consecutive": consecutive_talks},
                    )
                    task_state.set_phase(
                        PHASE_ACT_OR_ANSWER,
                        next_step="Continue with a tool or submit a grounded final answer",
                    )
                agent.run_store.write_task_state(task_state)
                agent.emit_agent_state(task_state, "assistant_commentary")
                continue

            if kind == "tool":
                consecutive_talks = 0
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)
                tool_started_at = time.monotonic()
                agent.emit_progress(f"step {attempts}: running tool {name}")
                tool_call_id = _new_tool_call_id()
                if name == "read_file" and task_state.read_files >= task_state.max_read_files:
                    tool_result = ToolExecutionResult(
                        content=(
                            f"error: read_file budget exhausted ({task_state.max_read_files}); "
                            "use the existing evidence or return a final answer"
                        ),
                        metadata={
                            "tool_status": "rejected",
                            "tool_error_code": "read_file_budget_exhausted",
                            "read_only": True,
                            "affected_paths": [],
                        },
                    )
                else:
                    if name == "read_file":
                        task_state.record_read_file()
                    tool_result = agent.execute_tool(name, args, tool_call_id=tool_call_id)
                task_state.record_affected_paths(tool_result.metadata.get("affected_paths", []))
                tool_status = str(tool_result.metadata.get("tool_status", "unknown"))
                if tool_status in {"ok", "partial_success"}:
                    affected_paths = list(tool_result.metadata.get("affected_paths", []))
                    relative_paths = list(affected_paths)
                    requested_path = args.get("path") if isinstance(args, dict) else None
                    if isinstance(requested_path, str) and requested_path.strip():
                        relative_paths.append(requested_path.strip().replace("\\", "/"))
                    task_state.record_evidence(
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": name,
                            "status": tool_status,
                            "read_only": bool(tool_result.metadata.get("read_only", False)),
                            "affected_paths": affected_paths,
                            "relative_paths": sorted(set(relative_paths)),
                            "freshness": "current_run",
                            "sensitivity": "workspace",
                            "summary": agent.summarize_tool_result(name, args, tool_result),
                        }
                    )
                agent.emit_progress(
                    f"step {attempts}: tool {name} finished "
                    f"({tool_result.metadata.get('tool_status', 'unknown')})"
                )
                result = tool_result.content
                summary = agent.summarize_tool_result(name, args, tool_result)
                task_state.begin_post_tool_reasoning(name)
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "summary": summary,
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),
                    },
                )
                if agent.allow_checkpoint:
                    checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
                    agent.run_store.write_task_state(task_state)
                    agent.emit_trace(
                        task_state,
                        "checkpoint_created",
                        {
                            "checkpoint_id": checkpoint["checkpoint_id"],
                            "trigger": "tool_executed",
                        },
                    )
                # 边界 7 由下一轮顶部的边界 2 承担。
                # The next iteration is an explicit post-tool reasoning
                # boundary. It must choose another tool or a final answer.
                agent.emit_trace(
                    task_state,
                    "post_tool_reasoning",
                    {
                        "tool": name,
                        "summary": summary,
                        "decision": "continue_or_final",
                    },
                )
                task_state.finish_post_tool_reasoning("continue")
                agent.run_store.write_task_state(task_state)
                agent.emit_agent_state(task_state, "post_tool_reasoning")
                continue

            if kind == "retry":
                consecutive_talks = 0
                task_state.record_malformed_output_recovered()
                task_state.set_phase(PHASE_ACT_OR_ANSWER, next_step="Retry with a valid tool call or final answer")
                if protocol_repairs < MAX_PROTOCOL_REPAIRS:
                    protocol_repairs += 1
                    protocol_feedback = str(payload)
                    getattr(agent.execution_hooks, "model_protocol_retrying", lambda *_args: None)(
                        task_state,
                        "execute",
                        {
                            "attempt": protocol_repairs,
                            "max_attempts": MAX_PROTOCOL_REPAIRS + 1,
                            **response_diagnostics,
                        },
                    )
                    agent.run_store.write_task_state(task_state)
                    agent.emit_agent_state(task_state, "malformed_output_recovered")
                    continue
                protocol_failed = True
                agent.emit_trace(
                    task_state,
                    "model_protocol_failed",
                    {
                        "repairs": protocol_repairs,
                        **response_diagnostics,
                    },
                )
                break

            # 边界 8：写最终回答和 durable memory 前。
            token.raise_if_cancelled()
            consecutive_talks = 0
            final = (payload or raw).strip()
            if task_state.requires_post_tool_reasoning:
                # Defensive fallback for custom runtimes that bypass the
                # normal post-tool transition.
                task_state.finish_post_tool_reasoning("final")
            task_state.set_phase(
                PHASE_VERIFY,
                next_step="Verify the collected evidence before returning the final answer",
                completed_item="Gather the minimum workspace context",
            )
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "verify_before_final")
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "final")
            if agent.allow_durable_memory_write:
                agent.promote_durable_memory(user_message, final)
            checkpoint = None
            if agent.allow_checkpoint:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.run_store.write_task_state(task_state)
            if checkpoint is not None:
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "run_finished",
                    },
                )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            agent.emit_progress(f"run {task_state.run_id} finished")
            return final

        if protocol_failed or (attempts >= max_attempts and tool_steps < agent.max_steps):
            final = _best_effort_step_limit(task_state, "模型反复返回无效输出（retry_limit_reached）")
            task_state.stop_retry_limit(final)
        else:
            final = _best_effort_step_limit(task_state, "步数预算已用尽（budget_exhausted）")
            task_state.stop_step_limit(final)
        task_state.set_phase(PHASE_FINAL, next_step="Explain the budget or execution blocker")
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "run_stopped")
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        if agent.allow_durable_memory_write:
            agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        if agent.allow_checkpoint:
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": task_state.stop_reason or "run_stopped",
                },
            )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        agent.emit_progress(f"run {task_state.run_id} stopped: {task_state.stop_reason}")
        return final

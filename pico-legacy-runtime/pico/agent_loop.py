"""Agent control loop extracted from the runtime facade."""

import time
import uuid

from .checkpoint import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
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
from .workspace import clip, now


def _new_tool_call_id():
    return "call_" + uuid.uuid4().hex


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
        max_attempts = min(
            max(agent.max_steps * 3, agent.max_steps + 4),
            task_state.max_total_steps,
        )

        # 边界 1：进入控制循环前。
        token.raise_if_cancelled()

        while tool_steps < agent.max_steps and attempts < max_attempts:
            # 边界 2：每轮开始、构建 prompt 前。
            token.raise_if_cancelled()
            attempts += 1
            task_state.record_attempt()
            task_state.set_phase(PHASE_ACT_OR_ANSWER, next_step="Choose a tool or prepare a final answer")
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "model_decision")
            agent.emit_progress(f"step {attempts}: building prompt")
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
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
            )
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
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
            agent.emit_progress(f"step {attempts}: model returned {kind}")
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
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
                task_state.record_malformed_output_recovered()
                task_state.set_phase(PHASE_ACT_OR_ANSWER, next_step="Retry with a valid tool call or final answer")
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                agent.emit_agent_state(task_state, "malformed_output_recovered")
                continue

            # 边界 8：写最终回答和 durable memory 前。
            token.raise_if_cancelled()
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

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
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

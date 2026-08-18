"""Structured tool execution for the agent runtime."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .approval import ApprovalOutcome
from .execution_hooks import ProcessCleanupFailed, RunCancelled
from .workspace import clip

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
    tool_call_id="",
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
        "tool_call_id": tool_call_id,
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


class ToolExecutor:
    def __init__(self, agent: Pico):
        self.agent = agent
        # §7.8.9 并发批：多线程执行只读工具时，除 tool["run"](args) 之外的
        # 共享状态段（校验 / hooks / 审批 / memory / 元数据）必须互斥——
        # 防止心跳线程、memory 沉淀、事件属性归属互相踩踏。
        self._execution_lock = threading.Lock()

    def _record_sandbox_violation(self, tool_error_code, security_event_type, tool_name):
        agent = self.agent
        if agent.current_task_state is None:
            return
        agent.current_task_state.record_sandbox_violation()
        agent.run_store.write_task_state(agent.current_task_state)
        agent.emit_trace(
            agent.current_task_state,
            "sandbox_violation",
            {
                "tool": tool_name,
                "tool_error_code": tool_error_code,
                "security_event_type": security_event_type,
                "agent_role": getattr(agent, "agent_role", "coordinator"),
            },
        )

    def execute(self, name, args, tool_call_id=""):
        agent = self.agent
        tool_call = {"id": tool_call_id, "name": name, "args": dict(args or {})}

        # ── 前置段：校验 / 重复 / hooks / 审批（共享状态，必须互斥）──
        # 并发批里多个只读工具会同时进入这里，靠 _execution_lock 串行化。
        with self._execution_lock:
            if agent.allowed_tools is not None and name not in agent.allowed_tools:
                self._record_sandbox_violation("tool_not_allowed", "tool_not_allowed", name)
                return ToolExecutionResult(
                    content=f"error: tool '{name}' is not allowed in this run",
                    metadata=_metadata(
                        "rejected",
                        tool_error_code="tool_not_allowed",
                        security_event_type="tool_not_allowed",
                        risk_level="high",
                        read_only=False,
                        tool_call_id=tool_call_id,
                    ),
                )

            tool = agent.tools.get(name)
            if tool is None:
                return ToolExecutionResult(
                    content=f"error: unknown tool '{name}'",
                    metadata=_metadata(
                        "rejected",
                        tool_error_code="unknown_tool",
                        risk_level="high",
                        read_only=False,
                        tool_call_id=tool_call_id,
                    ),
                )

            try:
                agent.validate_tool(name, args)
            except Exception as exc:
                example = agent.tool_example(name)
                message = f"error: invalid arguments for {name}: {exc}"
                if example:
                    message += f"\nexample: {example}"
                security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
                if security_event_type:
                    self._record_sandbox_violation("invalid_arguments", security_event_type, name)
                return ToolExecutionResult(
                    content=message,
                    metadata=_metadata(
                        "rejected",
                        tool_error_code="invalid_arguments",
                        security_event_type=security_event_type,
                        risk_level="high" if tool["risky"] else "low",
                        read_only=not tool["risky"],
                        affected_paths=None,
                        tool_call_id=tool_call_id,
                    ),
                )

            if agent.repeated_tool_call(name, args):
                return ToolExecutionResult(
                    content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                    metadata=_metadata(
                        "rejected",
                        tool_error_code="repeated_identical_call",
                        risk_level="high" if tool["risky"] else "low",
                        read_only=not tool["risky"],
                        tool_call_id=tool_call_id,
                    ),
                )

            # 校验通过：tool_requested 是线性化点，之后才可能发起审批。
            agent.execution_hooks.tool_requested(agent.current_task_state, tool_call)

            if tool["risky"]:
                outcome = agent.approve_outcome(name, args, tool_call_id=tool_call_id)
                if outcome is ApprovalOutcome.CANCELLED:
                    raise RunCancelled()
                if outcome is ApprovalOutcome.EXPIRED:
                    result = ToolExecutionResult(
                        content=f"error: approval expired for {name}",
                        metadata=_metadata(
                            "rejected",
                            tool_error_code="approval_expired",
                            security_event_type="approval_denied",
                            risk_level="high",
                            read_only=False,
                            tool_call_id=tool_call_id,
                        ),
                    )
                    agent.execution_hooks.after_tool(agent.current_task_state, result)
                    return result
                if outcome is ApprovalOutcome.REJECTED:
                    security_event_type = "read_only_block" if agent.read_only else "approval_denied"
                    if security_event_type == "read_only_block":
                        self._record_sandbox_violation("approval_denied", security_event_type, name)
                    result = ToolExecutionResult(
                        content=f"error: approval denied for {name}",
                        metadata=_metadata(
                            "rejected",
                            tool_error_code="approval_denied",
                            security_event_type=security_event_type,
                            risk_level="high",
                            read_only=False,
                            tool_call_id=tool_call_id,
                        ),
                    )
                    agent.execution_hooks.after_tool(agent.current_task_state, result)
                    return result

            # 实际执行前的最后一次取消复检。
            agent.cancellation_token.raise_if_cancelled()
            agent.execution_hooks.before_tool(agent.current_task_state, tool_call)

            before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
            after_snapshot = before_snapshot

        # ── 执行段（唯一不加锁：并发批的多个只读工具在这里真正并行）──
        # 只读工具无共享状态；写工具（risky）永远在串行组执行，锁无竞争。
        try:
            content = clip(tool["run"](args))
        except (ProcessCleanupFailed, RunCancelled):
            raise
        except Exception as exc:
            # ── 后置段（异常路径；共享状态，回锁）──
            with self._execution_lock:
                after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
                affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
                workspace_changed = bool(affected_paths)
                security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
                if security_event_type:
                    self._record_sandbox_violation("tool_failed", security_event_type, name)
                metadata = _metadata(
                    "partial_success" if workspace_changed else "error",
                    tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                    affected_paths=affected_paths,
                    workspace_changed=workspace_changed,
                    workspace_fingerprint=agent.workspace.fingerprint(),
                    diff_summary=diff_summary,
                    tool_call_id=tool_call_id,
                )
                agent.record_process_note_for_tool(name, metadata)
                result = ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)
                agent.execution_hooks.after_tool(agent.current_task_state, result)
                return result

        # ── 后置段（成功路径；共享状态，回锁）──
        with self._execution_lock:
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if "[tool timed out" in content:
                    tool_status = "partial_success" if workspace_changed else "error"
                    tool_error_code = "tool_timeout"
                elif exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                tool_call_id=tool_call_id,
            )
            agent.record_process_note_for_tool(name, metadata)
            result = ToolExecutionResult(content=content, metadata=metadata)
            agent.execution_hooks.after_tool(agent.current_task_state, result)
            return result

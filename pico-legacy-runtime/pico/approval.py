"""Approval contract shared by the CLI and the Web backend.

The legacy `Pico.approve(name, args)` terminal-input behavior is preserved as
one strategy (`AskApprovalStrategy`). `auto` and `never` return fixed outcomes.
The Web backend injects a strategy that persists a pending approval and waits
for a REST decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class ApprovalOutcome(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ApprovalRequest:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""


class ApprovalStrategy(Protocol):
    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Return a decision for exactly one tool call."""


class PermissionMode(str, Enum):
    """权限模式档位（来自 Claude Code 的 permission modes 分类）。

    - plan：禁一切写（所有 risky 工具拒绝）。
    - acceptEdits：自动接受编辑工具（write_file / patch_file），其余 risky 仍问。
    - default：逐次审批（risky 工具走底层策略）。
    - bypass：显式豁免（所有 risky 工具放行）。
    """

    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    DEFAULT = "default"
    BYPASS = "bypass"


# 编辑类工具：acceptEdits 模式下自动放行，其余 risky（如 run_shell）仍走审批。
EDIT_TOOLS = frozenset({"write_file", "patch_file"})


class AutoApprovalStrategy:
    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome.APPROVED


class NeverApprovalStrategy:
    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome.REJECTED


class AskApprovalStrategy:
    """CLI interactive strategy preserving the legacy `input()` behavior."""

    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        try:
            answer = input(
                f"approve {request.name} {json.dumps(request.args, ensure_ascii=False)}? [y/N] "
            )
        except EOFError:
            return ApprovalOutcome.REJECTED
        if answer.strip().lower() in {"y", "yes"}:
            return ApprovalOutcome.APPROVED
        return ApprovalOutcome.REJECTED


class AcceptEditsApprovalStrategy:
    """acceptEdits 模式：自动放行编辑工具，其余 risky 委托给底层策略。"""

    def __init__(self, fallback: ApprovalStrategy):
        self._fallback = fallback

    def decide(self, request: ApprovalRequest) -> ApprovalOutcome:
        if request.name in EDIT_TOOLS:
            return ApprovalOutcome.APPROVED
        return self._fallback.decide(request)


def _mode_value(mode) -> str:
    if isinstance(mode, PermissionMode):
        return str(mode.value)
    return str(mode or "").strip()


def strategy_for_mode(mode, fallback: ApprovalStrategy | None = None) -> ApprovalStrategy:
    """按权限模式包装底层审批策略（fallback 默认 = 逐次问）。

    这是 §7.9.2 权限模式的策略层核心：plan / acceptEdits / default / bypass
    只改变「risky 工具怎么审批」，不改变路径 / workspace / 沙盒等安全边界。
    """
    fallback = fallback if fallback is not None else AskApprovalStrategy()
    value = _mode_value(mode)
    if value in {PermissionMode.BYPASS.value, "auto"}:
        return AutoApprovalStrategy()
    if value in {PermissionMode.PLAN.value, "never"}:
        return NeverApprovalStrategy()
    if value == PermissionMode.ACCEPT_EDITS.value:
        return AcceptEditsApprovalStrategy(fallback)
    return fallback  # default / ask / unknown 都走底层策略


def strategy_for_policy(approval_policy: str) -> ApprovalStrategy:
    """Map the legacy approval policy string to a concrete strategy.

    保留 auto / never / ask 的旧语义，并把新权限模式名路由到 strategy_for_mode。
    """
    if approval_policy == "auto":
        return AutoApprovalStrategy()
    if approval_policy == "never":
        return NeverApprovalStrategy()
    if approval_policy in {m.value for m in PermissionMode}:
        return strategy_for_mode(approval_policy, AskApprovalStrategy())
    return AskApprovalStrategy()

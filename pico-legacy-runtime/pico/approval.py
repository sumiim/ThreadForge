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


def strategy_for_policy(approval_policy: str) -> ApprovalStrategy:
    """Map the legacy approval policy string to a concrete strategy."""
    if approval_policy == "auto":
        return AutoApprovalStrategy()
    if approval_policy == "never":
        return NeverApprovalStrategy()
    return AskApprovalStrategy()

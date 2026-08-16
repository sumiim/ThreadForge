"""Deterministic review gate, extracted from the LangGraph orchestration.

§7.7.1 阶段 0：review 门禁抽成独立 `review_gate(task_state)`，不依赖 graph state，
循环层可复用。LangGraph 删除后，原生循环用同一个 gate 做收尾确定性完成门禁。

语义（保持与 graph.py review_node 等价）：
- 写触发：仅当 affected_paths 有写（code_change）才需要收尾 review；
  只读路径（read_only）直接收敛。
- 确定性完成：checklist 全完成 or 无 checklist + 有 final answer。
- 预算收敛（§7.8.7 方案 C）：预算耗尽时按「剩余 checklist × 递减因子(3→2→1)」
  扩展；因子归零/清单清空/到硬顶时 auto-pass。
- 停滞检测（§7.8.7-③）：连续 K 轮零增量 → 强制停。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# §7.8.7 方案 C：review 预算扩展因子按轮递减（3→2→1），第 4 轮起归零 → 收敛。
REVIEW_EXTENSION_FACTORS = {1: 3, 2: 2, 3: 1}
# §7.8.7-③：连续 K 轮零进展（证据/checklist/工具集三信号全零增量）→ 强制停 + 托底。
STAGNATION_ROUNDS_LIMIT = 2


@dataclass
class ReviewDecision:
    """review gate 的确定性输出，与 graph 的 review result 同形。"""

    status: str = "needs_fix"  # pass | needs_fix
    text: str = ""
    issue_codes: list[str] = field(default_factory=list)
    recovered: bool = False
    budget_exhausted_convergence: bool = False
    extended_budget: int = 0
    stagnation_rounds: int = 0
    last_round_signature: str = ""


def _checklist_remaining(task_state) -> int:
    checklist = list(getattr(task_state, "checklist", []) or [])
    completed = set(getattr(task_state, "completed_items", []) or [])
    return max(0, len(checklist) - len(completed))


def _has_evidence(task_state) -> bool:
    return any(
        item.get("status") in {"ok", "partial_success"}
        for item in (getattr(task_state, "evidence", None) or [])
    )


def _has_write_evidence(task_state) -> bool:
    return any(
        item.get("status") in {"ok", "partial_success"}
        and not item.get("read_only", True)
        for item in (getattr(task_state, "evidence", None) or [])
    )


def _round_signature(task_state) -> str:
    """本轮进度签名：证据数 + 已完成项数 + 工具集摘要。三个都零增量才算停滞。"""
    import hashlib
    import json

    evidence = list(getattr(task_state, "evidence", None) or [])
    evidence_count = len(evidence)
    completed_count = len(getattr(task_state, "completed_items", []) or [])
    tool_keys = sorted(
        (str(item.get("tool_name", "")), tuple(item.get("relative_paths", []) or []))
        for item in evidence
    )
    digest = hashlib.sha256(
        json.dumps(tool_keys, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"{evidence_count}:{completed_count}:{digest}"


def run_review_gate(
    task_state,
    *,
    intent: str,
    step_budget: int,
    coordinator_steps_used: int,
    step_budget_explicit: bool = False,
    hard_cap: int = 0,
    fix_attempts: int = 0,
    previous_signature: str = "",
    previous_stagnation_rounds: int = 0,
) -> ReviewDecision:
    """确定性 review gate：返回是否通过 + 预算扩展/停滞状态。

    参数与 graph review_node 的 state 字段对齐，方便 LangGraph 版本与原生
    版本共用同一实现。``task_state`` 是 pico TaskState（含 evidence/checklist）。
    """
    remaining_budget = int(step_budget) - int(coordinator_steps_used)
    decision = ReviewDecision(
        extended_budget=int(step_budget),
        stagnation_rounds=int(previous_stagnation_rounds),
        last_round_signature=previous_signature,
    )

    # 预算耗尽：显式预算 → 直接收敛（§7.8.6 原行为）；软预算 → 按因子扩展。
    if remaining_budget < 1:
        if step_budget_explicit:
            decision.status = "pass"
            decision.budget_exhausted_convergence = True
            decision.text = "explicit step budget exhausted; converged"
            return decision
        remaining_checklist = _checklist_remaining(task_state)
        factor = REVIEW_EXTENSION_FACTORS.get(int(fix_attempts) + 1, 0)
        can_extend = (
            factor > 0
            and remaining_checklist > 0
            and hard_cap > 0
            and int(step_budget) < hard_cap
        )
        if can_extend:
            decision.extended_budget = min(
                int(hard_cap),
                int(step_budget) + remaining_checklist * factor,
            )
            # 扩展后继续 review（调用方决定是否 needs_fix）
            decision.status = "needs_fix"
            decision.text = "budget extended for review fix"
            return decision
        decision.status = "pass"
        decision.budget_exhausted_convergence = True
        decision.text = "review budget exhausted; converged"
        return decision

    # 停滞检测：连续 K 轮零增量 → 强制收敛。
    # 先于 read_only/写证据判断：即使证据看似足够，零进展轮次达到阈值也要停。
    signature = _round_signature(task_state)
    if signature == previous_signature:
        stagnation_rounds = int(previous_stagnation_rounds) + 1
    else:
        stagnation_rounds = 0
    decision.last_round_signature = signature
    decision.stagnation_rounds = stagnation_rounds
    if stagnation_rounds >= STAGNATION_ROUNDS_LIMIT:
        decision.status = "pass"
        decision.budget_exhausted_convergence = True
        decision.text = "stagnation detected; converged"
        return decision

    # 正常路径：只读任务无写证据 → 收敛（不需要模型评审）；
    # 写任务有写证据 + checklist 完成 → 收敛。其余 needs_fix。
    if intent == "read_only":
        if _has_evidence(task_state):
            decision.status = "pass"
            decision.text = "read-only evidence sufficient; converged"
        else:
            decision.status = "needs_fix"
            decision.text = "read-only task lacks current-run evidence"
        return decision

    # code_change / conversation：需要写证据 + 确定性完成。
    if _has_write_evidence(task_state) and _checklist_remaining(task_state) == 0:
        decision.status = "pass"
        decision.text = "write evidence present and checklist complete"
        return decision
    decision.status = "needs_fix"
    decision.text = "change not verified; write evidence or checklist incomplete"
    return decision

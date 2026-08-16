"""review_gate 独立门禁测试：与 graph.py review_node 语义等价。

覆盖：
- 预算耗尽显式预算 → 收敛（§7.8.6）
- 预算耗尽软预算 → 按因子扩展 / 因子归零收敛（§7.8.7 方案 C）
- 停滞检测连续轮零增量 → 强制收敛（§7.8.7-③）
- read_only 有证据 → pass；无证据 → needs_fix
- code_change 有写证据 + checklist 完成 → pass
"""

from __future__ import annotations

from pico.task_state import TaskState

from langgraph_pico.review_gate import (
    REVIEW_EXTENSION_FACTORS,
    STAGNATION_ROUNDS_LIMIT,
    run_review_gate,
)


def _task_state(*, evidence=None, checklist=None, completed=None):
    state = TaskState.create(run_id="r", task_id="t", user_request="test")
    # 替换默认 checklist（planner/调用方设置评审目标，不是追加到默认 5 项）
    if checklist is not None:
        state.checklist = [str(item) for item in checklist]
    if completed:
        state.completed_items = [str(item) for item in completed]
    for item in evidence or []:
        state.record_evidence(item)
    return state


def _evidence(name, *, read_only=True, status="ok", paths=None):
    return {
        "tool_name": name,
        "status": status,
        "read_only": read_only,
        "relative_paths": paths or ["sample.txt"],
    }


def test_explicit_budget_exhausted_converges():
    state = _task_state(evidence=[_evidence("read_file")])
    decision = run_review_gate(
        state,
        intent="code_change",
        step_budget=4,
        coordinator_steps_used=4,
        step_budget_explicit=True,
    )
    assert decision.status == "pass"
    assert decision.budget_exhausted_convergence is True


def test_soft_budget_exhausted_extends_with_factor():
    state = _task_state(
        checklist=["a", "b", "c"],
        completed=["a"],
        evidence=[_evidence("patch_file", read_only=False)],
    )
    decision = run_review_gate(
        state,
        intent="code_change",
        step_budget=4,
        coordinator_steps_used=4,
        step_budget_explicit=False,
        hard_cap=24,
    )
    # 剩余 checklist 2 个 × factor 3 → 4 + 6 = 10
    assert decision.status == "needs_fix"
    assert decision.extended_budget == 10
    assert not decision.budget_exhausted_convergence


def test_soft_budget_exhausted_converges_when_factor_zero():
    state = _task_state(
        checklist=["a"],
        completed=["a"],
        evidence=[_evidence("patch_file", read_only=False)],
    )
    decision = run_review_gate(
        state,
        intent="code_change",
        step_budget=4,
        coordinator_steps_used=4,
        step_budget_explicit=False,
        hard_cap=24,
        fix_attempts=3,  # factor 0
    )
    assert decision.status == "pass"
    assert decision.budget_exhausted_convergence is True


def test_soft_budget_exhausted_converges_at_hard_cap():
    state = _task_state(
        checklist=["a", "b"],
        evidence=[_evidence("patch_file", read_only=False)],
    )
    decision = run_review_gate(
        state,
        intent="code_change",
        step_budget=20,
        coordinator_steps_used=20,
        step_budget_explicit=False,
        hard_cap=24,
    )
    # 20 + 2×3 = 26 > 24 → 封顶 24，但仍在硬顶内可扩展？§7.8.7: min(hard_cap, ...)
    assert decision.extended_budget == 24
    assert not decision.budget_exhausted_convergence


def test_stagnation_force_converges():
    state = _task_state(evidence=[_evidence("read_file")])
    # 用实际签名模拟「上一轮同签名」：先算一次签名，再以它为 previous 传入
    from pico.evaluation.review_gate import _round_signature

    signature = _round_signature(state)
    decision = run_review_gate(
        state,
        intent="read_only",
        step_budget=8,
        coordinator_steps_used=2,
        previous_signature=signature,
        previous_stagnation_rounds=STAGNATION_ROUNDS_LIMIT - 1,
    )
    assert decision.status == "pass"
    assert decision.budget_exhausted_convergence is True


def test_read_only_with_evidence_passes():
    state = _task_state(evidence=[_evidence("read_file")])
    decision = run_review_gate(
        state,
        intent="read_only",
        step_budget=16,
        coordinator_steps_used=2,
    )
    assert decision.status == "pass"


def test_read_only_without_evidence_needs_fix():
    state = _task_state()
    decision = run_review_gate(
        state,
        intent="read_only",
        step_budget=16,
        coordinator_steps_used=2,
    )
    assert decision.status == "needs_fix"


def test_code_change_needs_write_evidence_and_checklist():
    # 只有读证据 → needs_fix
    state = _task_state(
        checklist=["change"],
        completed=[],
        evidence=[_evidence("read_file")],
    )
    decision = run_review_gate(
        state,
        intent="code_change",
        step_budget=24,
        coordinator_steps_used=2,
    )
    assert decision.status == "needs_fix"

    # 写证据 + checklist 完成 → pass
    state = _task_state(
        checklist=["change"],
        completed=["change"],
        evidence=[_evidence("patch_file", read_only=False)],
    )
    decision = run_review_gate(
        state,
        intent="code_change",
        step_budget=24,
        coordinator_steps_used=2,
    )
    assert decision.status == "pass"


def test_review_extension_factors_are_3_2_1():
    assert REVIEW_EXTENSION_FACTORS == {1: 3, 2: 2, 3: 1}
    assert 4 not in REVIEW_EXTENSION_FACTORS

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.evaluation.review_subagent import (
    _parse_review_output,
    build_review_obstacles,
    run_review,
)


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def test_review_obstacles_verification_when_write_unverified(tmp_path):
    agent = build_agent(tmp_path, ["<final>ok</final>"])
    agent.ask("hello")
    obstacles = build_review_obstacles(
        agent.current_task_state, has_write_or_shell=True, verification_passed=False
    )
    assert "verification" in obstacles
    assert "checklist" in obstacles  # 默认阶段模板 checklist 未完成也计


def test_review_obstacles_no_verification_for_readonly_verified(tmp_path):
    agent = build_agent(tmp_path, ["<final>ok</final>"])
    agent.ask("hello")
    obstacles = build_review_obstacles(
        agent.current_task_state, has_write_or_shell=False, verification_passed=True
    )
    assert "verification" not in obstacles


def test_review_parse_valid_and_malformed():
    parsed = _parse_review_output(
        '{"verdict": "finalize", "feedback": "all tests passed", "reason": "done"}'
    )
    assert parsed["verdict"] == "finalize"
    parsed_bad = _parse_review_output("not json")
    assert parsed_bad["verdict"] == "redirect"
    assert "malformed" in parsed_bad["reason"]


def test_review_finalize_without_obstacles(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>ok</final>",
            '{"verdict": "finalize", "feedback": "all tests passed", "reason": "done"}',
        ],
    )
    agent.ask("hello")
    state = agent.current_task_state
    decision = run_review(
        agent,
        state,
        request="inspect",
        trigger="final_before",
        has_write_or_shell=False,
        verification_passed=True,
    )
    assert decision["verdict"] == "finalize"
    assert state.review_audit and state.review_audit[-1]["verdict"] == "finalize"


def test_review_adversarial_rejects_unrebutted_finalize(tmp_path):
    """对抗性验证：finalize 有障碍（checklist）且无证据反驳 → 拒 finalize 转 redirect。

    verification_passed=True：程序 R1 门槛不触发，落到模型反驳层——
    review 无证据关键词 → unrebutted_obstacles。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>ok</final>",
            '{"verdict": "finalize", "feedback": "i think its done", "reason": "done"}',
        ],
    )
    agent.ask("hello")
    state = agent.current_task_state
    decision = run_review(
        agent,
        state,
        request="change code",
        trigger="final_before",
        has_write_or_shell=True,
        verification_passed=True,
    )
    assert decision["verdict"] == "redirect"
    assert "unrebutted_obstacles" in decision["reason"]


def test_review_adversarial_allows_evidence_rebuttal(tmp_path):
    """对抗性验证：finalize 有障碍（checklist）但带证据关键词 → 通过。

    注意 verification_passed=True：程序 R1 门槛不触发（验证已过），
    模型对 checklist 障碍的证据反驳生效。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>ok</final>",
            '{"verdict": "finalize", "feedback": "test passed, verification complete", "reason": "done"}',
        ],
    )
    agent.ask("hello")
    state = agent.current_task_state
    decision = run_review(
        agent,
        state,
        request="change code",
        trigger="final_before",
        has_write_or_shell=True,
        verification_passed=True,
    )
    assert decision["verdict"] == "finalize"


def test_review_verification_gate_requires_passed_verification(tmp_path):
    """方向 B：程序 R1 门槛（不依赖模型）。

    有写/shell 且 verification_passed=False → 即使 review 输出 finalize 且带
    证据关键词，程序也拒绝（verification_gate_unpassed）——「做完了」必须由
    验证动作证明，不是 review 口头确认。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>ok</final>",
            '{"verdict": "finalize", "feedback": "test passed, verification complete", "reason": "done"}',
        ],
    )
    agent.ask("hello")
    state = agent.current_task_state
    decision = run_review(
        agent,
        state,
        request="change code",
        trigger="final_before",
        has_write_or_shell=True,
        verification_passed=False,
    )
    assert decision["verdict"] == "redirect"
    assert decision["reason"] == "verification_gate_unpassed"

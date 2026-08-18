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


def test_review_prompt_includes_acceptance_and_run_trail(tmp_path):
    """review prompt 现在包含验收标准（done_when）与 run trail（动作印记）。

    修复「瞎验收」的信息缺口：review 之前只看到 evidence 摘要 + 障碍，
    看不到任务验收标准和主循环到底干了什么。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>ok</final>",
            # checklist 默认模板未完成会生成障碍，feedback 需带证据关键词反驳
            '{"verdict": "finalize", "feedback": "verified by test, done", "reason": "done"}',
        ],
    )
    agent.ask("hello")
    state = agent.current_task_state
    state.done_when = ["README updated", "tests pass"]
    state.user_request = "change code"
    # 模拟 run trail：主循环真的读过文件。
    agent.record({"role": "tool", "name": "read_file", "args": {"path": "README.md"}, "content": "demo\n"})

    decision = run_review(
        agent,
        state,
        request="change code",
        trigger="final_before",
        has_write_or_shell=False,
        verification_passed=True,
    )
    # FakeModelClient 输出 finalize 且带证据关键词反驳 checklist → 通过。
    assert decision["verdict"] == "finalize"
    # prompt 捕获检查：两块新信息都在。
    prompts = agent.model_client.prompts
    review_prompt = prompts[-1]
    assert "README updated; tests pass" in review_prompt  # done_when
    assert "[tool:read_file]" in review_prompt  # run trail 动作印记
    assert "Acceptance criteria" in review_prompt
    assert "Run trail" in review_prompt


def test_review_system_guides_ungrounded_zero_tool_finalize(tmp_path):
    """系统提示对「零工具凭记忆回答」的引导：请求暗示检查工作区但无工具调用。

    这是「瞎验收」的目标场景：模型回答代码问题却全程没碰工作区，
    review 系统提示现在明确要求 redirect 并要求先读相关文件。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>预算由墙钟硬顶兜底</final>",  # 零工具直接 final
            '{"verdict": "finalize", "feedback": "answered from memory", "reason": "done"}',
        ],
    )
    agent.ask("为什么预算会耗尽")
    state = agent.current_task_state
    decision = run_review(
        agent,
        state,
        request="为什么预算会耗尽，读代码确认",
        trigger="final_before",
        has_write_or_shell=False,
        verification_passed=True,
    )
    prompts = agent.model_client.prompts
    review_prompt = prompts[-1]
    # 系统提示包含 grounded 引导
    assert "not grounded" in review_prompt
    assert "inspect the relevant files" in review_prompt
    # run trail 明确显示零工具（只有 user/assistant 消息）
    assert "[tool:" not in review_prompt

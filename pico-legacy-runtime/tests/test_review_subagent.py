from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.evaluation.review_subagent import (
    _parse_review_action,
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


def test_review_parse_action_tool_vs_verdict():
    """§7.8.9 决策（2026-08-18）：review 输出解析——工具调用 or 判决 JSON。"""
    kind, name, args = _parse_review_action(
        '{"name": "run_shell", "args": {"command": "echo ok"}}'
    )
    assert kind == "tool"
    assert name == "run_shell"
    kind, decision = _parse_review_action(
        '{"verdict": "redirect", "feedback": "path wrong", "reason": "bad"}'
    )
    assert kind == "verdict"
    assert decision["verdict"] == "redirect"
    kind, _ = _parse_review_action("not json")
    assert kind == "invalid"
    kind, name, _args = _parse_review_action(
        '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>'
    )
    assert kind == "tool"
    assert name == "read_file"


def test_review_restricts_tools_to_read_and_shell(tmp_path):
    """§7.8.9 决策（2026-08-18）：review 工具面受限——写工具被拒。

    review 是验证者,不能改代码;结果回 feed review 上下文,不进主循环 evidence。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>Done.</final>",
            '{"name": "write_file", "args": {"path": "x.txt", "content": "nope"}}',
            '{"verdict": "continue", "feedback": "write not allowed in review", "reason": "tool_restricted"}',
        ],
    )
    agent.ask("anything")
    state = agent.current_task_state
    decision = run_review(
        agent,
        state,
        request="anything",
        trigger="final_before",
        has_write_or_shell=False,
        verification_passed=True,
    )
    # review 内部调写工具 → 被拒（error 文案喂回 review）→ review 给 continue
    assert decision["verdict"] == "continue"
    assert state.review_audit and state.review_audit[-1].get("tool_rounds", 0) == 1
    # 主循环 evidence 未被 review 的调查污染
    assert all(e.get("tool_name") != "write_file" for e in state.evidence)


def test_review_shell_verification_passes_r1_gate(tmp_path):
    """§7.8.9 决策（2026-08-18）：review 内部跑 shell 验证 → R1 gate 认可 finalize。

    主循环有写/shell 但无验证动作时,review 判 finalize 会被程序 gate 拒
    （verification_gate_unpassed）;review 先内部跑 run_shell(exit 0)再
    finalize → gate 认可(review_shell_ok=True)。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>ok</final>",
            '<tool>{"name":"run_shell","args":{"command":"echo ok"}}</tool>',
            '{"verdict": "finalize", "feedback": "file verified by shell exit 0", "reason": "done"}',
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
    assert decision["verdict"] == "finalize"
    audit = state.review_audit[-1]
    assert audit.get("review_shell_ok") is True
    assert audit.get("tool_rounds", 0) == 1


def test_agent_loop_review_internal_shell_verifies_write(tmp_path):
    """集成：主循环 write_file → final 前 review 内部跑 shell → finalize → completed。

    §7.8.9 决策（2026-08-18）的端到端验证——「进入 final 怎么跑验证」：
    review 用 bash 自证写的结果,不用主循环再跑一轮验证。
    """
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"a.txt","content":"hi\\n"}}</tool>',
            "<final>Done writing.</final>",
            '<tool>{"name":"run_shell","args":{"command":"echo ok"}}</tool>',
            '{"verdict": "finalize", "feedback": "write verified by shell exit 0", "reason": "done"}',
        ],
        feature_flags={"review_subagent": True},
    )

    answer = agent.ask("Create a.txt")

    assert answer == "Done writing."
    state = agent.current_task_state
    assert state.status == "completed"
    audit = state.review_audit
    assert audit, "review must have run before final"
    assert audit[-1]["verdict"] == "finalize"
    assert audit[-1].get("review_shell_ok") is True
    # review 的内部调查不污染主循环 evidence（主循环只有 write_file）
    assert {e.get("tool_name") for e in state.evidence} == {"write_file"}

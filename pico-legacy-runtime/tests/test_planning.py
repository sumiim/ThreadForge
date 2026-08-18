from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop
from pico.planning import parse_plan_steps


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


def test_parse_plan_steps_valid():
    # §7.8.9 决策（2026-08-18）：返回 list[dict]（goal + done_when）；
    # 纯字符串向后兼容 → done_when=[goal]。
    steps = parse_plan_steps(
        '{"steps": ["Read the module", "Identify the loop", "Write a summary"]}'
    )
    assert steps == [
        {"goal": "Read the module", "done_when": ["Read the module"]},
        {"goal": "Identify the loop", "done_when": ["Identify the loop"]},
        {"goal": "Write a summary", "done_when": ["Write a summary"]},
    ]


def test_parse_plan_steps_with_done_when():
    steps = parse_plan_steps(
        '{"steps": [{"goal": "Update README", "done_when": ["grep:README.md:new"]}, {"goal": "Run tests", "done_when": ["cmd:python -m pytest -q"]}]}'
    )
    assert steps == [
        {"goal": "Update README", "done_when": ["grep:README.md:new"]},
        {"goal": "Run tests", "done_when": ["cmd:python -m pytest -q"]},
    ]


def test_parse_plan_steps_invalid():
    import pytest

    with pytest.raises(ValueError):
        parse_plan_steps("not json")
    with pytest.raises(ValueError):
        parse_plan_steps('{"steps": []}')  # 空步骤


def test_agent_loop_planning_sets_real_checklist(tmp_path):
    """§7.8.9 阶段 3.5：planning flag 开启时，run 开始生成真实 checklist。

    FakeModelClient 顺序输出：第 1 次调用是 planning（返回步骤 JSON），
    后续是主循环输出。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '{"steps": ["Read hello.txt", "Summarize its content"]}',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>done</final>",
        ],
        feature_flags={"planning": True},
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "done"
    state = agent.current_task_state
    # checklist 被 planning 替换为真实步骤（不再是默认阶段模板）
    assert state.checklist == ["Read hello.txt", "Summarize its content"]
    assert state.done_when == ["Read hello.txt", "Summarize its content"]
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "planning_completed"' in trace
    assert '"event": "plan_checklist_created"' in trace


def test_agent_loop_planning_disabled_keeps_default_checklist(tmp_path):
    """planning flag 关闭 → 默认阶段模板 checklist（不变）。"""
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>done</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "done"
    state = agent.current_task_state
    # 默认阶段模板：Understand/Gather/Analyze/Act/Verify
    assert state.checklist[0] == "Understand the request and acceptance criteria"


def test_agent_loop_planning_failure_degrades_to_default(tmp_path):
    """planning 输出非法 → 降级默认模板（不阻塞任务）。"""
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            "not valid plan json",
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>done</final>",
        ],
        feature_flags={"planning": True},
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "done"
    state = agent.current_task_state
    assert state.checklist[0] == "Understand the request and acceptance criteria"
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "planning_failed"' in trace


def test_checklist_programmatic_hook_after_tool(tmp_path):
    """§7.8.9 决策（2026-08-18）：checklist 打钩——程序化 done_when 在工具后验证。

    planning 生成带验收标准的 checklist：grep:README.md:新标题 + cmd:echo ok。
    主循环写 README 后,打钩引擎验证 grep 通过 → step1 完成;cmd exit 0 → step2 完成。
    """
    (tmp_path / "README.md").write_text("old\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '{"steps": [{"goal": "更新 README", "done_when": ["grep:README.md:新标题"]}, {"goal": "跑测试", "done_when": ["cmd:echo ok"]}]}',
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"新标题\\n"}}</tool>',
            "<final>done</final>",
        ],
        feature_flags={"planning": True},
    )

    answer = AgentLoop(agent).run("Update README and test")

    assert answer == "done"
    state = agent.current_task_state
    assert "更新 README" in state.completed_items  # grep 验证通过
    assert "跑测试" in state.completed_items  # cmd exit 0
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "checklist_item_checked"' in trace
    assert '"method": "programmatic"' in trace


def test_checklist_review_semantic_hook(tmp_path):
    """§7.8.9 决策（2026-08-18）：checklist 打钩——自由文本 done_when 由 review 语义打钩。

    review 返回 completed_steps（工具验证后确认的自由文本项）→ 程序写入 completed_items。
    """
    agent = build_agent(
        tmp_path,
        [
            '{"steps": [{"goal": "审查改动", "done_when": ["重构是否合理"]}]}',
            "<final>ok</final>",
            '{"verdict": "finalize", "completed_steps": ["审查改动"], "feedback": "verified reasonable, test passed", "reason": "done"}',
            "<final>ok</final>",
            '{"verdict": "finalize", "feedback": "confirmed; test passed, verified", "reason": "done"}',
        ],
        feature_flags={"planning": True, "review_subagent": True},
    )

    answer = AgentLoop(agent).run("Refactor and review")

    assert answer == "ok"
    state = agent.current_task_state
    assert state.status == "completed"
    assert "审查改动" in state.completed_items  # review 语义打钩
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"method": "review_semantic"' in trace

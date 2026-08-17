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
    steps = parse_plan_steps(
        '{"steps": ["Read the module", "Identify the loop", "Write a summary"]}'
    )
    assert steps == ["Read the module", "Identify the loop", "Write a summary"]


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

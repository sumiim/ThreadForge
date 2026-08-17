from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop


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


def test_agent_loop_runs_same_control_flow_as_pico_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.report_path(agent.current_task_state.run_id).exists()


def test_agent_loop_allows_final_only_round_after_tool_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>The workspace contains README.md.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Inspect the workspace")

    assert answer == "The workspace contains README.md."
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.attempts == 2
    assert "The tool-call budget is exhausted" in agent.model_client.prompts[-1]


def test_agent_loop_rejects_tool_during_final_only_round(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>must not be consumed</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Inspect the workspace")

    assert "步数预算已用尽" in answer
    assert "list_files" in answer
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.read_files == 0
    assert len(agent.model_client.outputs) == 1
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert '"event": "finalization_protocol_rejected"' in trace


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, ["<final>Facade works.</final>"])

    assert agent.ask("Use facade") == "Facade works."


def test_agent_loop_passes_current_allowed_tools_to_model(tmp_path):
    class CapturingModel(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.completion_kwargs = kwargs
            return super().complete(prompt, max_new_tokens, **kwargs)

    agent = build_agent(tmp_path, [])
    model = CapturingModel(["<final>Done.</final>"])
    agent.model_client = model

    assert agent.ask("Inspect the workspace") == "Done."

    definitions = model.completion_kwargs["tool_definitions"]
    assert {item["name"] for item in definitions} == set(agent.tools)
    assert all(item["type"] == "function" for item in definitions)


def test_tool_result_enters_post_tool_reasoning_and_persists_agent_state(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Evidence reviewed.</final>",
        ],
    )

    assert agent.ask("Inspect hello.txt") == "Evidence reviewed."
    state = agent.current_task_state
    assert state.phase == "FINAL"
    assert state.read_files == 1
    assert state.checklist
    assert "Gather the minimum workspace context" in state.completed_items
    assert "Analyze evidence and choose the next action" in state.completed_items
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "agent_state_changed"' in trace
    assert '"event": "post_tool_reasoning"' in trace
    assert '"reason": "post_tool_reasoning"' in trace


def test_agent_loop_consumes_native_dict_tool_calls(tmp_path):
    # §2.1 原生 tool calling：客户端直接返回 dict {"name", "args"}，
    # AgentLoop 不再依赖 <tool> 文本协议解析。
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}},
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.read_files == 1


def test_agent_loop_rejects_native_dict_with_non_object_args(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": "not-an-object"},
            "<final>ok</final>",  # retry 后模型给 final
        ],
    )

    answer = AgentLoop(agent).run("Inspect")

    # args 非对象 → parse 返回 retry → 模型给 final → completed，且未执行工具
    assert answer == "ok"
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.read_files == 0


def test_agent_loop_stagnation_converges_without_evidence_growth(tmp_path):
    """§7.8.9：连续坏轮（工具重复/失败，talk 豁免）→ 证据截停，审计链完整。

    模型反复调用相同工具（同名同参 read_file）：第 1 次执行成功，之后被 P4
    重复拦截（tool_repeated_or_failed），连续 3 个坏轮 → 截停。验证：
    ① stagnation_audit 逐轮记录（含坏轮原因）；② stagnation_detected trace
    带完整审计链；③ 后续工具调用被 finalization 拒掉。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>converged</final>",
        ],
        max_steps=6,
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    state = agent.current_task_state
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "stagnation_detected"' in trace
    # 停滞触发后不允许新工具：重复读被 finalization 拒掉，执行数远小于供给数
    assert state.tool_steps < 5
    # 审计链：stagnation_audit 逐轮记录坏轮，且坏轮原因可解释
    assert state.stagnation_audit, "stagnation_audit must record per-turn bad decisions"
    bad_reasons = {reason for entry in state.stagnation_audit for reason in entry.get("reasons", [])}
    assert "tool_repeated_or_failed" in bad_reasons
    # trace 里截停事件带审计明细
    assert '"audit"' in trace


def test_agent_loop_talk_rounds_are_not_bad(tmp_path):
    """§7.8.9：talk 轮是思考信号（trace + 前端可见），不判坏。

    模型 talk → 工具 → talk → 工具 的交替轮次，坏轮判定不把 talk 计为坏，
    停滞窗口不会因思考轮误触发。连续 talk 由 MAX_CONSECUTIVE_TALKS 兜底。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            "<talk>let me look at this</talk>",
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<talk>i see the content</talk>",
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    state = agent.current_task_state
    assert state.status == "completed"
    # talk 轮不产生坏轮记录（reasons 为空且 bad=False）
    for entry in state.stagnation_audit:
        if not entry.get("bad"):
            continue
        assert "silent_turn_no_output" not in entry.get("reasons", []), (
            "talk rounds must not be counted as silent bad rounds"
        )
    assert state.status in {"stopped", "completed"}

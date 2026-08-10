from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
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

import json
import shutil
from pathlib import Path

import pytest

from pico import Pico
from pico.evaluation.backends import build_backend_runner
from pico.evaluation.evaluator import _scripted_outputs_for_task, load_benchmark
from pico.event_sink import NullSink
from pico.execution_hooks import NoopExecutionHooks, RunCancelled
from pico.providers.clients import FakeModelClient
from pico.run_store import RunStore
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


def _run_task(tmp_path, task, outputs, *, null_sink=False):
    source = Path(task["fixture_repo"])
    fixture_root = tmp_path / source.name
    shutil.copytree(source, fixture_root)
    workspace = WorkspaceContext.build(fixture_root, repo_root_override=fixture_root)
    session_store = SessionStore(fixture_root / ".pico" / "sessions")
    run_store = RunStore(fixture_root / ".pico" / "runs")
    kwargs = {"event_sink_factory": (lambda _: NullSink())} if null_sink else {}
    runner = build_backend_runner("langgraph", **kwargs)
    result = runner.run_task(
        task,
        workspace,
        session_store,
        run_store,
        fixture_root,
        model_client=FakeModelClient(outputs),
    )
    return result, fixture_root


def _build_runtime(
    tmp_path,
    outputs,
    *,
    allowed_tools=None,
    model_client=None,
    execution_hooks=None,
):
    source = Path("tests/fixtures/bench_repo_readme")
    fixture_root = tmp_path / "runtime-workspace"
    shutil.copytree(source, fixture_root)
    model_client = model_client or FakeModelClient(outputs)
    agent = Pico(
        model_client=model_client,
        workspace=WorkspaceContext.build(fixture_root, repo_root_override=fixture_root),
        session_store=SessionStore(fixture_root / ".pico" / "sessions"),
        run_store=RunStore(fixture_root / ".pico" / "runs"),
        approval_policy="auto",
        max_steps=6,
        allowed_tools=allowed_tools or ["delegate", "list_files", "read_file", "search", "patch_file"],
        event_sink=NullSink(),
        execution_hooks=execution_hooks,
    )
    return agent, model_client, fixture_root


def _v11_plan(intent, tools):
    return json.dumps(
        {
            "schema_version": "1",
            "plan_id": "plan_v11",
            "revision": 1,
            "intent": intent,
            "summary": "Execute the requested task.",
            "steps": [
                {
                    "id": "execute",
                    "goal": "Execute the task",
                    "dependencies": [],
                    "required_tools": tools,
                    "required_evidence": ["current run evidence"],
                    "done_when": ["the result is grounded and verified"],
                }
            ],
            "acceptance": ["the result is grounded and verified"],
            "risk_level": "low",
            "budgets": {
                "model_rounds": 4,
                "tool_calls": 4,
                "input_tokens": 20_000,
                "output_tokens": 2_000,
                "elapsed_seconds": 120,
            },
        }
    )


def test_langgraph_research_execute_review_uses_isolated_children_and_memory_events(tmp_path):
    task = load_benchmark("benchmarks/delegate_tasks.json")["tasks"][0]
    result, fixture_root = _run_task(
        tmp_path,
        task,
        _scripted_outputs_for_task(task, "langgraph"),
        null_sink=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.task_state.affected_paths == ["README.md"]
    assert result.agent.session["history"] == []
    assert len(result.budget_task_states) == 2
    assert sum(state.tool_steps for state in result.budget_task_states) == 3
    assert len(result.child_task_states) == 3
    assert not result.agent.run_store.trace_path(result.task_state).exists()
    events = list(result.events)
    assert sum(event.get("event") == "delegate_started" for event in events) == 1
    assert sum(event.get("event") == "review_requested" for event in events) == 1
    assert any(event.get("event") == "review_passed" for event in events)
    assert "delegated research" in (fixture_root / "README.md").read_text(encoding="utf-8")


def test_langgraph_no_review_path_stops_without_calling_reviewer(tmp_path):
    task = {
        "id": "no_review_path",
        "prompt": "Inspect without changing files.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 3,
        "expected_artifact": "no change",
        "category": "contract",
        "requires_research": False,
    }
    result, _ = _run_task(tmp_path, task, ["<final>No changes.</final>"])

    assert result.task_state.stop_reason == "no_changes_to_review"
    assert result.final_answer == "No changes."
    assert not any(event.get("event") == "review_requested" for event in result.events)


def test_langgraph_budget_exhaustion_starts_no_delegate_or_model(tmp_path):
    task = {
        "id": "budget_exhaustion",
        "prompt": "Research and patch README.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file", "patch_file"],
        "step_budget": 2,
        "expected_artifact": "no change",
        "category": "contract",
        "requires_research": True,
        "artifact_path": "README.md",
    }
    result, _ = _run_task(tmp_path, task, [])

    assert result.task_state.stop_reason == "budget_exhausted"
    assert result.task_state.tool_steps == 0
    assert not any(event.get("event") in {"delegate_started", "review_requested"} for event in result.events)


def test_langgraph_model_failure_retains_executor_budget_state(tmp_path):
    task = {
        "id": "executor_model_failure",
        "prompt": "Patch README.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file", "patch_file"],
        "step_budget": 3,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
    }
    result, _ = _run_task(tmp_path, task, [])

    assert result.task_state.stop_reason == "model_error"
    assert len(result.budget_task_states) == 2
    assert result.budget_task_states[1].attempts == 1
    assert result.budget_task_states[1] in result.child_task_states


def test_langgraph_children_do_not_write_back_parent_setup_session(tmp_path):
    task = {
        "id": "isolated_setup",
        "prompt": "Inspect README without changing it.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 3,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
        "acceptance": "README remains readable.",
        "setup": {"kind": "context_reduction", "history_count": 2, "note_count": 1},
    }
    result, fixture_root = _run_task(
        tmp_path,
        task,
        [
            "<final>README inspected without changes.</final>",
            "<final>status: pass\nissues: none\nverify_targets: README.md</final>",
        ],
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert len(result.agent.session["history"]) == 2
    assert all("benchmark-history" in item["content"] for item in result.agent.session["history"])
    assert not (fixture_root / ".pico" / "memory").exists()


def test_langgraph_review_retry_limit_has_stable_terminal_reason(tmp_path):
    task = {
        "id": "review_retry_limit",
        "prompt": "Inspect README and satisfy review.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 6,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
        "acceptance": "Reviewer must pass.",
    }
    outputs = []
    for attempt in range(3):
        outputs.extend(
            [
                f"<final>Execution attempt {attempt + 1}.</final>",
                "<final>status: needs_fix\nissue: still incomplete\nverify_targets: README.md</final>",
            ]
        )
    result, _ = _run_task(tmp_path, task, outputs)

    assert result.task_state.stop_reason == "review_retry_limit_reached"
    assert result.final_answer == "自动审查未通过，已达到重试上限。请查看运行详情中的审查记录后重试。"
    assert "status: needs_fix" not in result.final_answer
    assert sum(event.get("event") == "review_retry_started" for event in result.events) == 2


def test_langgraph_graph_state_contains_only_declared_data_fields():
    from langgraph_pico.graph import AgentState

    assert "agent" not in AgentState.__annotations__
    assert "model_client" not in AgentState.__annotations__


def test_langgraph_cancellation_converges_to_cancelled_terminal_state(tmp_path):
    from langgraph_pico import run_agent

    class CancelledToken:
        def is_cancelled(self):
            return True

        def raise_if_cancelled(self):
            raise RunCancelled()

    agent, _, _ = _build_runtime(tmp_path, [_v11_plan("conversation", [])])
    agent.cancellation_token = CancelledToken()

    result = run_agent(
        agent,
        "Stop this run.",
        task_mode="auto",
        enable_planning=True,
    )

    assert result.task_state.status == "stopped"
    assert result.task_state.stop_reason == "user_cancelled"
    assert result.final_answer == ""


def test_run_agent_reuses_cli_runtime_and_records_parent_session(tmp_path):
    from langgraph_pico import run_agent
    from pico import Pico

    task = load_benchmark("benchmarks/delegate_tasks.json")["tasks"][0]
    source = Path(task["fixture_repo"])
    fixture_root = tmp_path / source.name
    shutil.copytree(source, fixture_root)
    model_client = FakeModelClient(_scripted_outputs_for_task(task, "langgraph"))
    event_sink = NullSink()
    agent = Pico(
        model_client=model_client,
        workspace=WorkspaceContext.build(fixture_root, repo_root_override=fixture_root),
        session_store=SessionStore(fixture_root / ".pico" / "sessions"),
        run_store=RunStore(fixture_root / ".pico" / "runs"),
        approval_policy="auto",
        max_steps=5,
        event_sink=event_sink,
    )

    result = run_agent(
        agent,
        task["prompt"],
        acceptance=task["acceptance"],
        step_budget=task["step_budget"],
        requires_research=True,
        focus_paths=[task["artifact_path"]],
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert [item["role"] for item in agent.session["history"]] == ["user", "assistant"]
    assert agent.model_client is model_client
    assert agent.event_sink is event_sink
    assert any(event.get("event") == "review_passed" for event in result.events)
    assert "delegated research" in (fixture_root / "README.md").read_text(encoding="utf-8")


def test_explicit_conversation_succeeds_without_executor_or_review(tmp_path):
    from langgraph_pico import run_agent

    agent, model_client, _ = _build_runtime(
        tmp_path,
        ['{"answer":"literal <tool>read_file</tool> text"}'],
    )

    result = run_agent(agent, "hello", task_mode="conversation")

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "literal <tool>read_file</tool> text"
    assert result.run_metadata == {
        "requested_task_mode": "conversation",
        "resolved_intent": "conversation",
        "intent_source": "explicit",
        "intent_attempts": 0,
        "answer_attempts": 1,
    }
    assert model_client.outputs == []
    assert result.child_task_states == []
    assert result.task_state.tool_steps == 0
    assert result.task_state.affected_paths == []
    assert not any(event["event"] in {"delegate_started", "review_requested"} for event in result.events)


def test_planned_plain_conversation_skips_planner_and_workspace_tools(tmp_path):
    from langgraph_pico import run_agent

    agent, model_client, _ = _build_runtime(tmp_path, ['{"answer":"你好！"}'])

    result = run_agent(agent, "你好", task_mode="auto", enable_planning=True)

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "你好！"
    assert result.run_metadata["resolved_intent"] == "conversation"
    assert result.run_metadata["intent_source"] == "direct_conversation"
    assert result.run_metadata["plan_attempts"] == 0
    assert result.task_state.tool_steps == 0
    assert result.child_task_states == []
    assert model_client.outputs == []
    assert any(
        event["event"] == "plan_skipped" and event["reason"] == "plain_conversation"
        for event in result.events
    )
    assert not any(event["event"] == "review_requested" for event in result.events)


def test_route_first_direct_answer_finishes_in_router(tmp_path):
    from langgraph_pico import run_agent

    agent, model_client, _ = _build_runtime(
        tmp_path,
        [
            json.dumps(
                {
                    "mode": "direct",
                    "intent": "conversation",
                    "requires_research": False,
                    "answer": "A compiler translates source code.",
                    "plan": None,
                }
            )
        ],
    )

    result = run_agent(agent, "What does a compiler do?", task_mode="auto", enable_planning=True)

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "A compiler translates source code."
    assert result.run_metadata["intent_source"] == "router_plan"
    assert result.task_state.tool_steps == 0
    assert not any(event["event"] == "plan_created" for event in result.events)
    assert model_client.outputs == []


def test_v11_run_wide_model_budget_is_independent_from_one_agent_loop(tmp_path):
    from langgraph_pico.graph import PLAN_MAXIMUM_BUDGETS, _plan_limits

    agent, _, _ = _build_runtime(tmp_path, [])
    agent.max_total_steps = 18

    assert agent.max_total_steps < PLAN_MAXIMUM_BUDGETS["model_rounds"]
    assert _plan_limits({"step_budget": agent.max_steps}, agent)["model_rounds"] == 64


def test_continuation_reuses_workspace_context_and_stays_read_only(tmp_path):
    from langgraph_pico import run_agent

    plan = _v11_plan("read_only", ["read_file"])
    outputs = [
        plan,
        '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
        "<final>The README was inspected.</final>",
        "status: pass\nThe answer is grounded in current evidence.",
        plan,
        '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
        "<final>The README was inspected again.</final>",
        "status: pass\nThe resumed answer is grounded in current evidence.",
    ]
    agent, model_client, _ = _build_runtime(tmp_path, outputs)

    first = run_agent(
        agent,
        "Inspect README",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )
    resumed = run_agent(
        agent,
        "continue",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert first.task_state.stop_reason == "final_answer_returned"
    assert resumed.task_state.stop_reason == "final_answer_returned"
    assert resumed.task_state.intent == "read_only"
    assert resumed.task_state.read_files > 0 or any(
        child.read_files > 0 for child in resumed.child_task_states
    )
    assert any("Inspect README" in prompt for prompt in model_client.prompts)


def test_continuation_after_initial_model_failure_restores_workspace_route(tmp_path):
    from langgraph_pico import run_agent

    class FailOnceClient(FakeModelClient):
        def __init__(self):
            super().__init__([])
            self.failed = False

        def complete(self, prompt, max_new_tokens, **kwargs):
            if not self.failed:
                self.failed = True
                raise TimeoutError("simulated provider timeout")
            return super().complete(prompt, max_new_tokens, **kwargs)

    agent, model_client, _ = _build_runtime(tmp_path, [], model_client=FailOnceClient())
    first = run_agent(
        agent,
        "Read the project files and explain the architecture.",
        task_mode="auto",
        enable_planning=True,
    )
    legacy_conversation_plan = json.loads(_v11_plan("conversation", []))
    legacy_conversation_plan["steps"][0]["required_evidence"] = []
    model_client.outputs.extend(
        [
            json.dumps(legacy_conversation_plan),
            _v11_plan("read_only", ["read_file"]),
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>The architecture is documented in README.</final>",
            "status: pass\nThe current evidence supports the answer.",
        ]
    )
    resumed = run_agent(
        agent,
        "continue",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert first.task_state.stop_reason == "model_error"
    assert resumed.task_state.stop_reason == "final_answer_returned"
    assert resumed.task_state.intent == "read_only"
    assert any(child.read_files > 0 for child in resumed.child_task_states)
    assert any(
        event.get("event") == "intent_classification_rejected"
        for event in resumed.events
    )
    assert "Read the project files and explain the architecture." in model_client.prompts[-3]


def test_planned_conversation_without_tools_skips_review(tmp_path):
    from langgraph_pico import run_agent

    plan = json.loads(_v11_plan("conversation", []))
    plan["steps"][0]["required_evidence"] = []
    plan["budgets"]["tool_calls"] = 0
    agent, model_client, _ = _build_runtime(
        tmp_path,
        [json.dumps(plan), '{"answer":"A compiler translates source code."}'],
    )

    result = run_agent(
        agent,
        "What does a compiler do?",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "A compiler translates source code."
    assert result.run_metadata["intent_source"] == "plan"
    assert result.task_state.tool_steps == 0
    assert model_client.outputs == []
    assert not any(event["event"] == "review_requested" for event in result.events)


@pytest.mark.parametrize("requires_research", [False, True])
def test_read_only_routes_through_optional_research_and_reviews_completion(tmp_path, requires_research):
    from langgraph_pico import run_agent

    outputs = ["<final>Research evidence.</final>"] if requires_research else []
    outputs.extend(
        [
            "<final>README contains a project description.</final>",
            "status: pass\nThe answer is grounded in the current workspace evidence.",
        ]
    )
    agent, _, _ = _build_runtime(tmp_path, outputs)

    result = run_agent(
        agent,
        "Explain README",
        task_mode="read_only",
        requires_research=requires_research,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.run_metadata["resolved_intent"] == "read_only"
    assert result.run_metadata["answer_attempts"] == 1
    assert sum(event["event"] == "delegate_started" for event in result.events) == int(requires_research)
    assert any(event["event"] == "review_started" for event in result.events)
    assert any(event["event"] == "review_completed" for event in result.events)
    assert all(state.affected_paths == [] for state in result.child_task_states)


def test_read_only_answer_rejects_write_tool_and_records_sandbox_violation(tmp_path):
    from langgraph_pico import run_agent

    agent, _, fixture_root = _build_runtime(
        tmp_path,
        [
            '<tool>{"name":"patch_file","args":{"path":"README.md","old_text":"fixture","new_text":"changed"}}</tool>',
            "<final>README was not modified.</final>",
            "status: pass\nThe read-only boundary was respected.",
        ],
    )
    original = (fixture_root / "README.md").read_text(encoding="utf-8")

    result = run_agent(
        agent,
        "Inspect README without changing it",
        task_mode="read_only",
        requires_research=False,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert sum(state.sandbox_violations for state in result.child_task_states) == 1
    assert any(event["event"] == "sandbox_violation" for event in result.events)
    assert (fixture_root / "README.md").read_text(encoding="utf-8") == original


def test_auto_router_recovers_once_and_publishes_route_metadata(tmp_path):
    from langgraph_pico import run_agent

    agent, _, _ = _build_runtime(tmp_path, ['{"answer":"Hello."}'])
    router = FakeModelClient(
        [
            "not json",
            '{"intent":"conversation","requires_research":true}',
        ]
    )

    result = run_agent(agent, "hello", task_mode="auto", router_model_client=router)

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.task_state.malformed_output_recovered == 1
    assert result.run_metadata == {
        "requested_task_mode": "auto",
        "resolved_intent": "conversation",
        "intent_source": "router",
        "intent_attempts": 2,
        "answer_attempts": 1,
    }
    assert any(event["event"] == "intent_classification_recovered" for event in result.events)
    routes = [(event["from_node"], event["to_node"]) for event in result.events if event["event"] == "route_selected"]
    assert routes == [("intent_router", "answer")]


def test_auto_router_fails_closed_after_two_malformed_outputs(tmp_path):
    from langgraph_pico import run_agent

    agent, model_client, _ = _build_runtime(tmp_path, [])
    router = FakeModelClient(["bad", '{"intent":"read_only"}'])

    result = run_agent(agent, "inspect", task_mode="auto", router_model_client=router)

    assert result.task_state.stop_reason == "retry_limit_reached"
    assert result.task_state.malformed_output_recovered == 2
    assert result.run_metadata["resolved_intent"] == ""
    assert result.run_metadata["intent_source"] == "router_failed"
    assert result.run_metadata["intent_attempts"] == 2
    assert model_client.prompts == []
    assert result.task_state.tool_steps == 0
    assert result.child_task_states == []
    assert not any(event["event"] in {"delegate_started", "review_requested"} for event in result.events)


def test_router_provider_failure_maps_to_model_error_without_fallback(tmp_path):
    from langgraph_pico import run_agent

    agent, model_client, _ = _build_runtime(tmp_path, [])
    result = run_agent(
        agent,
        "inspect",
        task_mode="auto",
        router_model_client=FakeModelClient([]),
    )

    assert result.task_state.stop_reason == "model_error"
    assert result.run_metadata["intent_attempts"] == 1
    assert model_client.prompts == []
    assert result.child_task_states == []


def test_conversation_protocol_exhaustion_is_a_stable_failure(tmp_path):
    from langgraph_pico import run_agent

    agent, _, _ = _build_runtime(tmp_path, ["plain", '{"answer":""}'])
    result = run_agent(agent, "hello", task_mode="conversation")

    assert result.task_state.stop_reason == "retry_limit_reached"
    assert result.task_state.malformed_output_recovered == 2
    assert result.run_metadata["answer_attempts"] == 2
    rejected = [event for event in result.events if event["event"] == "conversation_protocol_rejected"]
    assert len(rejected) == 2


def test_malformed_review_status_counts_as_one_recovered_output(tmp_path):
    task = {
        "id": "malformed_review",
        "prompt": "Inspect README and satisfy review.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["delegate", "read_file"],
        "step_budget": 3,
        "expected_artifact": "README",
        "category": "contract",
        "requires_research": False,
        "artifact_path": "README.md",
        "acceptance": "README is valid.",
    }
    result, _ = _run_task(
        tmp_path,
        task,
        [
            "<final>README inspected.</final>",
            "<final>unexpected review status</final>",
        ],
    )

    assert result.task_state.malformed_output_recovered == 1
    assert any(
        state.final_answer.startswith("unexpected review status")
        for state in result.child_task_states
    )


def test_focus_path_fast_path_skips_router(tmp_path):
    from langgraph_pico import run_agent

    task = load_benchmark("benchmarks/delegate_tasks.json")["tasks"][0]
    agent, _, fixture_root = _build_runtime(
        tmp_path,
        _scripted_outputs_for_task(task, "langgraph"),
    )
    router = FakeModelClient([])

    result = run_agent(
        agent,
        task["prompt"],
        task_mode="auto",
        router_model_client=router,
        acceptance=task["acceptance"],
        focus_paths=[task["artifact_path"], task["artifact_path"]],
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.run_metadata["intent_source"] == "focus_path"
    assert result.run_metadata["intent_attempts"] == 0
    assert router.prompts == []
    assert "delegated research" in (fixture_root / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_mode": "conversation", "focus_paths": ["README.md"]},
        {"task_mode": "read_only", "acceptance": "must pass"},
        {"task_mode": "conversation", "requires_research": True},
        {"task_mode": "read_only", "router_model_client": FakeModelClient([])},
        {"task_mode": "code_change", "focus_paths": "README.md"},
        {"task_mode": "code_change", "focus_paths": [""]},
        {"task_mode": "code_change", "focus_paths": ["."]},
        {"task_mode": "code_change", "focus_paths": ["../outside"]},
        {"task_mode": "code_change", "focus_paths": ["C:\\outside.txt"]},
    ],
)
def test_run_agent_rejects_invalid_mode_and_path_combinations(tmp_path, kwargs):
    from langgraph_pico import run_agent

    agent, _, _ = _build_runtime(tmp_path, [])
    with pytest.raises((ValueError, PermissionError)):
        run_agent(agent, "task", **kwargs)


def test_v11_read_only_fails_without_current_run_evidence(tmp_path):
    from langgraph_pico import run_agent

    agent, _, _ = _build_runtime(
        tmp_path,
        [
            _v11_plan("read_only", ["read_file"]),
            "<final>Ungrounded research.</final>",
            "<final>Ungrounded research retry.</final>",
            "<final>Ungrounded answer.</final>",
            "<final>Ungrounded answer retry.</final>",
        ],
    )
    result = run_agent(
        agent,
        "Inspect README",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )
    assert result.task_state.stop_reason == "runtime_error"
    assert "no successful workspace evidence" in result.final_answer
    assert any(event.get("event") == "required_tools_retry" for event in result.events)


def test_v11_read_only_protocol_failure_does_not_restart_answer_executor(tmp_path):
    from langgraph_pico import run_agent

    agent, model_client, _ = _build_runtime(
        tmp_path,
        [
            _v11_plan("read_only", ["read_file"]),
            "plain model output",
            "plain model output again",
            "<final>must not be consumed</final>",
        ],
    )

    result = run_agent(
        agent,
        "Inspect README",
        task_mode="read_only",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "retry_limit_reached"
    assert result.run_metadata["answer_attempts"] == 2
    assert len(model_client.outputs) == 1
    assert not any(event.get("event") == "required_tools_retry" for event in result.events)


def test_v11_rejected_answer_candidates_are_discarded(tmp_path):
    from langgraph_pico import run_agent

    class TransactionHooks(NoopExecutionHooks):
        def __init__(self):
            self.begins = 0
            self.commits = 0
            self.discards = 0

        def begin_answer_candidate(self, task_state):
            self.begins += 1

        def commit_answer_candidate(self, task_state):
            self.commits += 1

        def discard_answer_candidate(self, task_state):
            self.discards += 1

    hooks = TransactionHooks()
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            _v11_plan("read_only", ["read_file"]),
            "<final>Ungrounded first answer.</final>",
            "<final>Ungrounded retry answer.</final>",
        ],
        execution_hooks=hooks,
    )

    result = run_agent(
        agent,
        "Inspect README",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "runtime_error"
    assert hooks.begins == 2
    assert hooks.commits == 0
    assert hooks.discards >= 1


def test_v11_accepted_answer_candidate_commits_after_review(tmp_path):
    from langgraph_pico import run_agent

    class TransactionHooks(NoopExecutionHooks):
        def __init__(self):
            self.events = []

        def begin_answer_candidate(self, task_state):
            self.events.append("begin")

        def commit_answer_candidate(self, task_state):
            self.events.append("commit")

        def discard_answer_candidate(self, task_state):
            self.events.append("discard")

    hooks = TransactionHooks()
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            _v11_plan("read_only", ["read_file"]),
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>Grounded answer.</final>",
            "status: pass\nThe answer is grounded.",
        ],
        execution_hooks=hooks,
    )

    result = run_agent(
        agent,
        "Inspect README",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "Grounded answer."
    assert hooks.events == ["begin", "commit"]


def test_v11_research_evidence_can_ground_answer_without_duplicate_read(tmp_path):
    from langgraph_pico import run_agent

    plan = _v11_plan("read_only", ["read_file"])
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            plan,
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>Research found the README.</final>",
            "<final>The README contains the requested project description.</final>",
            "status: pass\nThe answer is grounded in current workspace evidence.",
        ],
    )

    result = run_agent(
        agent,
        "Explain README",
        task_mode="auto",
        requires_research=True,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "The README contains the requested project description."
    assert any(item.get("tool_name") == "read_file" for item in result.task_state.evidence)
    assert not any(
        event.get("event") == "required_tools_retry" and event.get("stage") == "answer"
        for event in result.events
    )


@pytest.mark.parametrize("synthesis_tools", [["read_file"], []])
def test_v11_completion_gate_reuses_evidence_for_synthesis_step(
    tmp_path, synthesis_tools
):
    from langgraph_pico import run_agent

    plan = json.loads(_v11_plan("read_only", ["read_file"]))
    plan["steps"] = [
        {
            "id": "inventory",
            "goal": "Inventory the workspace",
            "dependencies": [],
            "required_tools": ["list_files"],
            "required_evidence": ["workspace entries"],
            "done_when": ["the workspace layout is known"],
        },
        {
            "id": "read",
            "goal": "Read the project overview",
            "dependencies": ["inventory"],
            "required_tools": ["read_file"],
            "required_evidence": ["README contents"],
            "done_when": ["the project overview is known"],
        },
        {
            "id": "search",
            "goal": "Search for architecture evidence",
            "dependencies": ["read"],
            "required_tools": ["search"],
            "required_evidence": ["matching source references"],
            "done_when": ["the architecture evidence is grounded"],
        },
        {
            "id": "synthesize",
            "goal": "Synthesize the final architecture answer",
            "dependencies": ["inventory", "read", "search"],
            "required_tools": synthesis_tools,
            "required_evidence": ["evidence gathered by the preceding steps"],
            "done_when": ["a grounded architecture answer is returned"],
        },
    ]
    plan["budgets"]["tool_calls"] = 6
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            json.dumps(plan),
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            '<tool>{"name":"search","args":{"pattern":"fixture","path":"."}}</tool>',
            "<final>The workspace contains a documented fixture architecture.</final>",
            "status: pass\nThe answer is grounded in current-run evidence.",
        ],
    )

    result = run_agent(
        agent,
        "Inspect the workspace and explain its architecture.",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == (
        "The workspace contains a documented fixture architecture."
    )
    assert result.task_state.completed_items == result.task_state.checklist
    assert sum(
        item.get("tool_name") == "read_file" for item in result.task_state.evidence
    ) == 1


def test_v11_read_only_reviews_after_using_full_tool_budget(tmp_path):
    from langgraph_pico import run_agent

    plan = json.loads(_v11_plan("read_only", ["list_files", "read_file", "search"]))
    plan["budgets"]["tool_calls"] = 6
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            json.dumps(plan),
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
            '<tool>{"name":"search","args":{"pattern":"fixture","path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":".pico"}}</tool>',
            '<tool>{"name":"search","args":{"pattern":"README","path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":2}}</tool>',
            "<final>The full tool budget produced enough evidence for an answer.</final>",
            "status: pass\nThe answer is grounded in all current-run evidence.",
        ],
    )

    result = run_agent(
        agent,
        "Inspect the workspace using the available evidence budget.",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == (
        "The full tool budget produced enough evidence for an answer."
    )
    assert result.task_state.review_status == "pass"
    assert sum(child.tool_steps for child in result.budget_task_states) == 6


def test_v11_read_only_replan_does_not_double_charge_previous_answer_tools(tmp_path):
    from langgraph_pico import run_agent

    first_plan = json.loads(
        _v11_plan("read_only", ["list_files", "read_file", "search"])
    )
    revised_plan = json.loads(json.dumps(first_plan))
    revised_plan["revision"] = 2
    review_feedback = "Explain the module boundaries before summarizing the architecture."
    agent, model_client, _ = _build_runtime(
        tmp_path,
        [
            json.dumps(first_plan),
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            '<tool>{"name":"search","args":{"pattern":"Placeholder","path":"."}}</tool>',
            "<final>The first architecture summary is incomplete.</final>",
            f"status: needs_fix\n{review_feedback}",
            json.dumps(revised_plan),
            "<final>The revised answer explains the module boundaries and architecture.</final>",
            "status: pass\nThe revised answer satisfies the request.",
        ],
    )

    result = run_agent(
        agent,
        "Read the project files and explain the architecture.",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == (
        "The revised answer explains the module boundaries and architecture."
    )
    assert result.task_state.tool_steps == 0
    assert sum(child.tool_steps for child in result.budget_task_states) == 3
    assert review_feedback in model_client.prompts[-2]
    assert "previous_answer" in model_client.prompts[-2]
    assert not any(
        event.get("event") == "plan_budget_exhausted" for event in result.events
    )


def test_v11_code_change_fails_without_write_evidence(tmp_path):
    from langgraph_pico import run_agent

    agent, _, _ = _build_runtime(
        tmp_path,
        [_v11_plan("code_change", ["patch_file"]), "<final>No change.</final>"],
    )
    result = run_agent(
        agent,
        "Patch README",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )
    assert result.task_state.stop_reason == "no_changes_to_review"


def test_v11_conversation_accepts_scalar_plan_fields_and_zero_tool_budget(tmp_path):
    from langgraph_pico import run_agent

    plan = json.loads(_v11_plan("conversation", []))
    plan["steps"][0]["required_evidence"] = "the greeting is available"
    plan["steps"][0]["done_when"] = "a greeting is returned"
    plan["budgets"]["model_rounds"] = 3
    plan["budgets"]["tool_calls"] = 0
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            json.dumps(plan),
            '{"answer":"你好!"}',
            "status: pass\nThe greeting answers the request.",
        ],
    )

    result = run_agent(
        agent,
        "你好",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "你好!"
    assert result.run_metadata["resolved_intent"] == "conversation"
    assert result.task_state.tool_steps == 0


def test_v11_route_plan_repair_attempts_share_one_deadline(tmp_path):
    from langgraph_pico import run_agent

    plan = json.loads(_v11_plan("conversation", []))
    plan["steps"][0]["required_evidence"] = []

    class DeadlineModelClient(FakeModelClient):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.deadlines = []

        def complete(self, prompt, max_new_tokens, **kwargs):
            if "planning" in prompt.lower() or "route" in prompt.lower():
                self.deadlines.append(kwargs.get("deadline_monotonic"))
            return super().complete(prompt, max_new_tokens, **kwargs)

    client = DeadlineModelClient(
        [
            "invalid route",
            json.dumps(plan),
            '{"answer":"done"}',
        ]
    )
    agent, _, _ = _build_runtime(tmp_path, [], model_client=client)

    result = run_agent(
        agent,
        "Explain the approach",
        task_mode="auto",
        enable_planning=True,
        planning_deadline_seconds=75,
    )

    assert result.final_answer == "done"
    assert len(client.deadlines) == 2
    assert client.deadlines[0] == client.deadlines[1]


def test_v11_conversation_normalizes_budget_below_observed_planning_usage(tmp_path):
    from langgraph_pico import run_agent

    class UsageModelClient(FakeModelClient):
        def __init__(self, outputs, usages):
            super().__init__(outputs)
            self.usages = list(usages)

        def complete(self, prompt, max_new_tokens, **kwargs):
            result = super().complete(prompt, max_new_tokens, **kwargs)
            self.last_completion_metadata = self.usages.pop(0)
            return result

    plan = json.loads(_v11_plan("conversation", []))
    plan["steps"][0]["required_evidence"] = []
    plan["budgets"].update(
        {
            "model_rounds": 3,
            "tool_calls": 0,
            "input_tokens": 1_000,
            "output_tokens": 256,
            "elapsed_seconds": 60,
        }
    )
    client = UsageModelClient(
        [
            json.dumps(plan),
            '{"answer":"你好! 有什么我可以帮你?"}',
            "status: pass\nThe greeting answers the request.",
        ],
        [
            {"input_tokens": 4_743, "output_tokens": 164},
            {"input_tokens": 800, "output_tokens": 30},
            {"input_tokens": 600, "output_tokens": 20},
        ],
    )
    agent, _, _ = _build_runtime(tmp_path, [], model_client=client)

    result = run_agent(
        agent,
        "你好",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "你好! 有什么我可以帮你?"
    assert result.task_state.plan_history[-1]["budgets"]["input_tokens"] == 20_000
    assert not any(event.get("event") == "plan_budget_exhausted" for event in result.events)


def test_v11_successful_research_extends_soft_token_budget(tmp_path):
    from langgraph_pico import run_agent

    class UsageModelClient(FakeModelClient):
        def __init__(self, outputs, usages):
            super().__init__(outputs)
            self.usages = list(usages)

        def complete(self, prompt, max_new_tokens, **kwargs):
            result = super().complete(prompt, max_new_tokens, **kwargs)
            self.last_completion_metadata = self.usages.pop(0)
            return result

    plan = json.loads(_v11_plan("read_only", ["read_file"]))
    plan["budgets"].update(
        {
            "model_rounds": 8,
            "tool_calls": 4,
            "input_tokens": 20_000,
            "output_tokens": 4_000,
            "elapsed_seconds": 300,
        }
    )
    client = UsageModelClient(
        [
            json.dumps(plan),
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>Research evidence from README.</final>",
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>README contains the fixture project description.</final>",
            "status: pass\nThe answer is grounded in current workspace evidence.",
        ],
        [
            {"input_tokens": 4_846, "output_tokens": 705},
            {"input_tokens": 14_000, "output_tokens": 1_600},
            {"input_tokens": 14_480, "output_tokens": 1_758},
            {"input_tokens": 1_000, "output_tokens": 100},
            {"input_tokens": 1_000, "output_tokens": 100},
            {"input_tokens": 1_000, "output_tokens": 100},
        ],
    )
    agent, _, _ = _build_runtime(tmp_path, [], model_client=client)

    result = run_agent(
        agent,
        "Explain README",
        task_mode="auto",
        requires_research=True,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert result.final_answer == "README contains the fixture project description."
    extensions = [event for event in result.events if event.get("event") == "plan_budget_extended"]
    assert extensions
    assert {"input_tokens", "output_tokens"}.issubset(extensions[0]["budget_keys"])
    assert extensions[0]["usage"]["input_tokens"] == 33_326
    assert extensions[0]["usage"]["output_tokens"] == 4_063
    assert not any(event.get("event") == "plan_budget_exhausted" for event in result.events)


def test_v11_system_token_budget_remains_a_hard_limit(tmp_path):
    from langgraph_pico import run_agent

    class UsageModelClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            result = super().complete(prompt, max_new_tokens, **kwargs)
            self.last_completion_metadata = {"input_tokens": 1_000_001, "output_tokens": 1}
            return result

    plan = json.loads(_v11_plan("conversation", []))
    plan["steps"][0]["required_evidence"] = []
    plan["budgets"].update(
        {
            "model_rounds": 3,
            "tool_calls": 0,
            "input_tokens": 1_000_000,
            "output_tokens": 4_000,
            "elapsed_seconds": 300,
        }
    )
    client = UsageModelClient([json.dumps(plan)])
    agent, _, _ = _build_runtime(tmp_path, [], model_client=client)

    result = run_agent(
        agent,
        "Explain a general concept without inspecting the workspace.",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
    )

    assert result.task_state.stop_reason == "budget_exhausted"
    exhausted = [event for event in result.events if event.get("event") == "plan_budget_exhausted"]
    assert exhausted[-1]["budget_keys"] == ["input_tokens"]
    assert not any(event.get("event") == "plan_budget_extended" for event in result.events)


def test_v11_review_auto_passes_when_step_budget_exhausted(tmp_path):
    from langgraph_pico import run_agent

    first_plan = json.loads(_v11_plan("read_only", ["read_file"]))
    first_plan["budgets"]["tool_calls"] = 2
    revised_plan = json.loads(json.dumps(first_plan))
    revised_plan["revision"] = 2
    agent, _, _ = _build_runtime(
        tmp_path,
        [
            json.dumps(first_plan),
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>The first answer.</final>",
            "status: needs_fix\nissue: still incomplete",
            json.dumps(revised_plan),
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>The second answer.</final>",
            "status: needs_fix\nissue: still incomplete",
        ],
    )

    result = run_agent(
        agent,
        "Inspect README",
        task_mode="auto",
        requires_research=False,
        enable_planning=True,
        step_budget=2,
    )

    assert result.task_state.stop_reason == "final_answer_returned"
    assert "预算已用尽" in result.final_answer
    assert "The second answer." in result.final_answer
    assert "still incomplete" in result.final_answer

from pathlib import Path
from types import SimpleNamespace

import pytest

from pico import cli


def test_public_run_parser_keeps_legacy_shape(tmp_path):
    args = cli.build_arg_parser().parse_args(["legacy prompt", "--cwd", str(tmp_path)])

    assert args.prompt == ["legacy prompt"]
    assert args.backend == "native"
    assert args.task_mode == "auto"
    assert args.requires_research is None
    assert not hasattr(args, "command")


def test_internal_parser_supports_run_and_eval():
    run_args = cli._build_command_parser().parse_args(
        [
            "run",
            "--backend",
            "langgraph",
            "--no-research",
            "--focus-path",
            "README.md",
            "--acceptance",
            "README is correct",
            "hello",
        ]
    )
    eval_args = cli._build_command_parser().parse_args(
        ["eval", "--backend", "langgraph", "--tasks", "benchmarks/delegate_tasks.json"]
    )

    assert run_args.command == "run"
    assert run_args.prompt == ["hello"]
    assert run_args.backend == "langgraph"
    assert run_args.requires_research is False
    assert run_args.focus_paths == ["README.md"]
    assert run_args.acceptance == "README is correct"
    assert eval_args.command == "eval"
    assert eval_args.backend == "langgraph"


def test_eval_main_returns_two_when_no_tasks_apply(monkeypatch, tmp_path, capsys):
    output = tmp_path / "eval.json"

    def fake_run_fixed_benchmark(**kwargs):
        assert kwargs["backend"] == "langgraph"
        assert Path(kwargs["artifact_path"]) == output
        return {
            "summary": {
                "total_tasks": 1,
                "eligible_tasks": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0.0,
            }
        }

    monkeypatch.setattr("pico.evaluation.evaluator.run_fixed_benchmark", fake_run_fixed_benchmark)

    exit_code = cli.main(["eval", "--backend", "langgraph", "--out", str(output)])

    assert exit_code == 2
    assert "tasks:1" in capsys.readouterr().out


def test_run_request_keeps_native_ask_path():
    class NativeAgent:
        current_task_state = SimpleNamespace(stop_reason="final_answer_returned")

        @staticmethod
        def ask(prompt):
            return f"native:{prompt}"

    answer, succeeded, stop_reason = cli._run_request(
        NativeAgent(),
        "hello",
        SimpleNamespace(backend="native"),
    )

    assert answer == "native:hello"
    assert succeeded is True
    assert stop_reason == "final_answer_returned"


def test_run_request_lazily_dispatches_to_langgraph(monkeypatch):
    import langgraph_pico

    captured = {}

    def fake_run_agent(agent, prompt, **kwargs):
        captured.update({"agent": agent, "prompt": prompt, **kwargs})
        return SimpleNamespace(
            final_answer="graph answer",
            task_state=SimpleNamespace(status="completed", stop_reason="final_answer_returned"),
        )

    monkeypatch.setattr(langgraph_pico, "run_agent", fake_run_agent)
    agent = object()
    args = SimpleNamespace(
        backend="langgraph",
        acceptance="done",
        max_steps=7,
        requires_research=False,
        focus_paths=["README.md"],
        task_mode="code_change",
    )
    router_model_client = object()

    answer, succeeded, stop_reason = cli._run_request(
        agent,
        "change README",
        args,
        router_model_client,
    )

    assert answer == "graph answer"
    assert succeeded is True
    assert stop_reason == "final_answer_returned"
    assert captured == {
        "agent": agent,
        "prompt": "change README",
        "acceptance": "done",
        "step_budget": 7,
        "requires_research": False,
        "focus_paths": ["README.md"],
        "task_mode": "code_change",
        "router_model_client": router_model_client,
        "record_session": True,
    }


def test_run_request_native_path_does_not_import_langgraph(monkeypatch):
    imported = []
    original_import = __import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", recording_import)
    agent = SimpleNamespace(
        ask=lambda prompt: "native answer",
        current_task_state=SimpleNamespace(stop_reason="final_answer_returned"),
    )

    cli._run_request(agent, "hello", SimpleNamespace(backend="native"))

    assert not any(name.startswith("langgraph_pico") for name in imported)


@pytest.mark.parametrize(
    "argv",
    [
        ["--backend", "native", "--research", "task"],
        ["--backend", "native", "--task-mode", "read_only", "task"],
        ["--backend", "langgraph", "--task-mode", "read_only", "--router-model", "router", "task"],
        ["--backend", "langgraph", "--task-mode", "conversation", "--research", "task"],
        ["--backend", "langgraph", "--task-mode", "conversation", "--focus-path", "README.md", "task"],
    ],
)
def test_invalid_langgraph_cli_combinations_fail_before_agent_construction(monkeypatch, argv):
    monkeypatch.setattr(cli, "build_agent", lambda args: pytest.fail("agent must not be built"))

    with pytest.raises(SystemExit) as caught:
        cli.main(argv)

    assert caught.value.code == 2


def test_cli_auto_builds_independent_zero_temperature_router_client(monkeypatch):
    main_client = SimpleNamespace(model="main-model", temperature=0.2, host="local")
    router_client = object()
    agent = SimpleNamespace(model_client=main_client)
    build_calls = []
    dispatched = {}

    monkeypatch.setattr(cli, "build_agent", lambda args: agent)
    monkeypatch.setattr(cli, "build_welcome", lambda *args, **kwargs: "")

    def fake_build_model_client(args, **kwargs):
        build_calls.append(kwargs)
        return router_client

    def fake_run_request(agent_arg, prompt, args, router_model_client=None):
        dispatched.update(
            {
                "agent": agent_arg,
                "prompt": prompt,
                "router_model_client": router_model_client,
            }
        )
        return "answer", True, "final_answer_returned"

    monkeypatch.setattr(cli, "_build_model_client", fake_build_model_client)
    monkeypatch.setattr(cli, "_run_request", fake_run_request)

    assert cli.main(
        [
            "--backend",
            "langgraph",
            "--task-mode",
            "auto",
            "--router-model",
            "router-model",
            "hello",
        ]
    ) == 0

    assert build_calls == [{"model_override": "router-model", "temperature_override": 0.0}]
    assert dispatched["router_model_client"] is router_client
    assert main_client.model == "main-model"
    assert main_client.temperature == 0.2

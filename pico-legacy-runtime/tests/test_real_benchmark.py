"""real_benchmark 模块测试：任务过滤、门禁、CLI 退出码。

不调用真实 API：用 fake model_client_factory 与 monkeypatch 验证
- 只跑 real_model: true 的任务
- pass rate 低于门禁时非零退出
- 高于/等于门禁时 0 退出
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pico.evaluation.real_benchmark import (
    DEFAULT_MIN_PASS_RATE,
    _default_artifact_path,
    _real_tasks,
    create_deepseek_client_factory,
    main,
    run_real_benchmark,
)


def test_real_tasks_filters_to_real_model_only():
    tasks = _real_tasks(Path("benchmarks/coding_tasks.json"))
    assert tasks
    assert all(task.get("real_model") is True for task in tasks)
    ids = [task["id"] for task in tasks]
    assert "readme_intro_locked" in ids
    assert "readme_schema_note" in ids
    assert "sample_beta_locked" in ids
    assert "sample_gamma_locked" in ids
    # harness 机制任务（checkpoint/resume/durable）与脚本化 recovery 任务
    # （prompt 未指定目标字符串，verifier 硬编码脚本化短语）不应出现在真模型任务集
    assert "context_reduction_checkpoint" not in ids
    assert "durable_promotion_accept" not in ids
    assert "invalid_patch_recovery" not in ids
    assert "repeated_read_recovery" not in ids


def test_factory_requires_api_key(monkeypatch):
    monkeypatch.delenv("PICO_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API_KEY"):
        create_deepseek_client_factory()


def test_factory_creates_completions_client(monkeypatch):
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("PICO_DEEPSEEK_MODEL", "deepseek-v4-flash")
    factory = create_deepseek_client_factory()
    from pico.providers.clients import OpenAICompletionsModelClient

    client = factory(task={}, workspace=None)
    assert isinstance(client, OpenAICompletionsModelClient)
    assert client.model == "deepseek-v4-flash"
    assert client.api_key == "sk-test"


def test_default_artifact_path_names_model(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pico.evaluation.real_benchmark.DEFAULT_RESULTS_ROOT", tmp_path
    )
    path = _default_artifact_path("deepseek-v4-flash")
    assert path.parent == tmp_path
    assert "deepseek-v4-flash" in path.name
    assert path.name.endswith(".json")


def _fake_payload(pass_rate, total=7):
    passed = int(round(pass_rate * total))
    failed = total - passed
    return {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": pass_rate,
        },
        "rows": [
            {
                "id": f"task-{i}",
                "status": "pass" if i < passed else "fail",
                "passed": i < passed,
                "failure_category": None if i < passed else "verifier_failed",
            }
            for i in range(total)
        ],
    }


def _fake_benchmark_data(total=7):
    return {
        "schema_version": 1,
        "tasks": [
            {
                "id": f"task-{i}",
                "prompt": "p",
                "fixture_repo": "tests/fixtures/bench_repo_readme",
                "allowed_tools": ["read_file"],
                "step_budget": 4,
                "expected_artifact": "a",
                "category": "c",
                "verifier": "true",
                "real_model": True,
            }
            for i in range(total)
        ],
    }


def _patch_load_benchmark(monkeypatch):
    # run_real_benchmark 里 evaluator.load() -> BenchmarkEvaluator.load -> load_benchmark(path)
    # 需要真实文件；直接拦截 BenchmarkEvaluator.load 方法返回固定任务集。
    monkeypatch.setattr(
        "pico.evaluation.evaluator.BenchmarkEvaluator.load",
        lambda self: _fake_benchmark_data(),
    )
    monkeypatch.setattr(
        "pico.evaluation.real_benchmark.load_benchmark",
        lambda path: _fake_benchmark_data(),
    )


def test_run_real_benchmark_gate_failure_exits_nonzero(monkeypatch, tmp_path):
    artifact = tmp_path / "out.json"
    benchmark = tmp_path / "bench.json"
    _patch_load_benchmark(monkeypatch)

    def fake_evaluator_run(self):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text("{}", encoding="utf-8")
        return _fake_payload(0.5)

    monkeypatch.setattr(
        "pico.evaluation.evaluator.BenchmarkEvaluator.run", fake_evaluator_run
    )
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "sk-test")

    with pytest.raises(SystemExit) as caught:
        run_real_benchmark(
            benchmark_path=benchmark,
            artifact_path=artifact,
            min_pass_rate=0.8,
        )
    assert caught.value.code != 0
    assert "below required" in str(caught.value.code)


def test_run_real_benchmark_gate_pass_exits_zero(monkeypatch, tmp_path):
    artifact = tmp_path / "out.json"
    benchmark = tmp_path / "bench.json"
    _patch_load_benchmark(monkeypatch)

    def fake_evaluator_run(self):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text("{}", encoding="utf-8")
        return _fake_payload(0.9)

    monkeypatch.setattr(
        "pico.evaluation.evaluator.BenchmarkEvaluator.run", fake_evaluator_run
    )
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "sk-test")

    payload = run_real_benchmark(
        benchmark_path=benchmark,
        artifact_path=artifact,
        min_pass_rate=0.8,
    )
    assert payload["summary"]["pass_rate"] >= 0.8
    assert artifact.exists()


def test_repetitions_aggregate_any_and_all(monkeypatch, tmp_path):
    artifact = tmp_path / "out.json"
    benchmark = tmp_path / "bench.json"
    _patch_load_benchmark(monkeypatch)

    # 3 次重复：task-0 全过；task-1 1/3 过；task-2 全不过
    run_calls = {"count": 0}

    def fake_evaluator_run(self):
        run_calls["count"] += 1
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text("{}", encoding="utf-8")
        rep = run_calls["count"]
        rows = []
        for i in range(7):
            if i == 0:
                passed = True
            elif i == 1:
                passed = rep <= 2
            else:
                passed = False
            rows.append(
                {
                    "id": f"task-{i}",
                    "status": "pass" if passed else "fail",
                    "passed": passed,
                    "failure_category": None if passed else "verifier_failed",
                }
            )
        return {"summary": {"total": 7, "passed": sum(r["passed"] for r in rows), "failed": 0, "pass_rate": 0.0}, "rows": rows}

    monkeypatch.setattr(
        "pico.evaluation.evaluator.BenchmarkEvaluator.run", fake_evaluator_run
    )
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "sk-test")

    payload = run_real_benchmark(
        benchmark_path=benchmark,
        artifact_path=artifact,
        min_pass_rate=0.0,
        repetitions=3,
    )
    agg = payload["_real_benchmark"]["aggregate"]
    assert agg["per_task_any_pass"]["task-0"] is True
    assert agg["per_task_all_pass"]["task-0"] is True
    assert agg["per_task_any_pass"]["task-1"] is True
    assert agg["per_task_all_pass"]["task-1"] is False
    assert agg["per_task_any_pass"]["task-2"] is False
    # any = 2/7, all = 1/7 → 均值 ≈ 0.2143
    assert abs(agg["pass_rate"] - ((2 / 7) + (1 / 7)) / 2) < 1e-3
    assert run_calls["count"] == 3


def test_cli_main_maps_systemexit_to_code(monkeypatch, tmp_path):
    calls = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        raise SystemExit(3)

    monkeypatch.setattr("pico.evaluation.real_benchmark.run_real_benchmark", fake_run)
    assert main(["--benchmark", str(tmp_path / "b.json"), "--min-pass-rate", "0.7"]) == 3
    assert calls["min_pass_rate"] == 0.7
    assert Path(calls["benchmark_path"]) == tmp_path / "b.json"

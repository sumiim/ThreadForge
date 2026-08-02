import json
from pathlib import Path
from collections import Counter

import pytest

from pico.evaluation.evaluator import (
    BenchmarkEvaluator,
    _now_in_timezone,
    _scripted_outputs_for_task,
    load_benchmark,
    normalize_evaluation_artifact,
    run_harness_regression_v2,
    run_fixed_benchmark,
    summarize_review_rows,
    summarize_rows,
    validate_benchmark,
)
from zoneinfo import ZoneInfoNotFoundError


def test_load_benchmark_validates_fixed_schema():
    benchmark = load_benchmark(Path("benchmarks/coding_tasks.json"))

    assert benchmark["schema_version"] == 1
    assert len(benchmark["tasks"]) == 12
    assert Counter(task["category"] for task in benchmark["tasks"]) == {
        "documentation": 2,
        "text-edit": 2,
        "tool-boundary": 3,
        "recovery": 3,
        "durable-contract": 2,
    }
    for task in benchmark["tasks"]:
        assert {"id", "prompt", "fixture_repo", "allowed_tools", "step_budget", "expected_artifact", "verifier", "category"} <= set(task)
        assert isinstance(task["allowed_tools"], list)
        assert task["step_budget"] > 0


def test_default_timezone_falls_back_without_external_tzdata(monkeypatch):
    def missing_timezone(_):
        raise ZoneInfoNotFoundError("missing tzdata")

    monkeypatch.setattr("pico.evaluation.evaluator.ZoneInfo", missing_timezone)

    captured_at = _now_in_timezone("Asia/Shanghai")

    assert captured_at.endswith("+0800")


def test_load_benchmark_rejects_missing_required_task_fields(tmp_path):
    benchmark_path = tmp_path / "bad-benchmark.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "broken",
                        "prompt": "Missing required task keys.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required"):
        load_benchmark(benchmark_path)


def test_load_delegate_benchmark_normalizes_extension_fields():
    benchmark = load_benchmark(Path("benchmarks/delegate_tasks.json"))

    assert len(benchmark["tasks"]) == 4
    assert benchmark["tasks"][0]["backends"] == ["langgraph"]
    assert benchmark["tasks"][0]["requires_research"] is True
    assert benchmark["tasks"][0]["artifact_path"] == "README.md"
    assert "verifier_argv" in benchmark["tasks"][0]


def test_load_paired_benchmark_has_identical_backend_contract():
    benchmark = load_benchmark(Path("benchmarks/paired_tasks.json"))

    assert len(benchmark["tasks"]) == 20
    assert Counter(task["review_expectation"] for task in benchmark["tasks"]) == {
        "needs_fix": 10,
        "pass": 10,
    }
    assert all(task["backends"] == ["native", "langgraph"] for task in benchmark["tasks"])
    assert all(task["requires_research"] is False for task in benchmark["tasks"])
    for task in benchmark["tasks"]:
        native_outputs = _scripted_outputs_for_task(task, "native")
        langgraph_outputs = _scripted_outputs_for_task(task, "langgraph")
        assert langgraph_outputs[: len(native_outputs)] == native_outputs

    task = benchmark["tasks"][0]
    assert task["id"] == "paired_review_recovery"
    assert task["acceptance"] == (
        "sample.txt contains both beta-reviewed and gamma-reviewed."
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"verifier": "python -V"}, "exactly one"),
        ({"verifier_argv": ["python", 1]}, "must contain non-empty strings"),
        ({"verifier_timeout_s": True}, "verifier_timeout_s must be an integer"),
        ({"id": None}, "task id must be a string"),
        ({"id": "../outside"}, "filesystem-safe slug"),
        ({"id": "CON"}, "filesystem-safe slug"),
        ({"fixture_repo": "."}, "must name a repository subdirectory"),
        ({"fixture_repo": "../outside"}, "escapes repository root"),
        ({"prompt": ""}, "prompt must be a non-empty string"),
        ({"step_budget": "2"}, "step_budget must be an integer"),
        ({"acceptance": ""}, "acceptance must be a non-empty string"),
        ({"requires_research": "true"}, "must be a boolean"),
        ({"review_expectation": "reject", "defect_type": "test"}, "review_expectation"),
        ({"review_expectation": []}, "review_expectation"),
        ({"review_expectation": "needs_fix", "defect_type": ""}, "defect_type"),
        ({"defect_type": "omission"}, "requires review_expectation"),
        ({"backends": ["unknown"]}, "unknown backend"),
        ({"artifact_path": "../outside.txt"}, "escapes fixture root"),
        ({"artifact_path": "C:\\outside.txt"}, "must be relative"),
        (
            {"setup": {"kind": "freshness_mismatch", "path": "../outside.txt"}},
            "setup.path escapes fixture root",
        ),
        ({"setup": {"kind": "unknown"}}, "unknown setup kind"),
    ],
)
def test_benchmark_extension_validation_rejects_invalid_contracts(tmp_path, updates, message):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")
    task = {
        "id": "contract",
        "prompt": "Inspect README.",
        "fixture_repo": "fixture",
        "allowed_tools": ["read_file"],
        "step_budget": 2,
        "expected_artifact": "README exists",
        "verifier_argv": ["python", "-V"],
        "category": "contract",
    }
    task.update(updates)

    with pytest.raises(ValueError, match=message):
        validate_benchmark({"schema_version": 1, "tasks": [task]}, repo_root=tmp_path)


def test_benchmark_rejects_case_colliding_task_ids(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = {
        "id": "CaseTask",
        "prompt": "Inspect README.",
        "fixture_repo": "fixture",
        "allowed_tools": ["read_file"],
        "step_budget": 2,
        "expected_artifact": "README exists",
        "verifier_argv": ["python", "-V"],
        "category": "contract",
    }
    duplicate = {**task, "id": "casetask"}

    with pytest.raises(ValueError, match="duplicate benchmark task id"):
        validate_benchmark(
            {"schema_version": 1, "tasks": [task, duplicate]},
            repo_root=tmp_path,
        )


def test_run_fixed_benchmark_uses_fresh_fixture_copy_and_fresh_run_directory(tmp_path):
    artifact_path = tmp_path / "benchmark-v1.json"
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    original_fixture = Path("tests/fixtures/bench_repo_patch/sample.txt").read_text(encoding="utf-8")
    artifact = evaluator.run()

    row = next(item for item in artifact["rows"] if item["id"] == "sample_beta_locked")
    copied_fixture = (tmp_path / "workspaces" / row["fixture_copy_relpath"]).resolve()
    run_dir = (tmp_path / "workspaces" / row["run_dir_relpath"]).resolve()

    assert artifact_path.exists()
    assert copied_fixture.exists()
    assert run_dir.exists()
    assert not row["fixture_copy_relpath"].startswith("/")
    assert not row["run_dir_relpath"].startswith("/")
    assert row["initial_history_empty"] is True
    assert row["initial_memory_empty"] is True
    assert row["initial_task_summary_empty"] is True
    assert Path("tests/fixtures/bench_repo_patch/sample.txt").read_text(encoding="utf-8") == original_fixture
    assert "beta-locked" in (copied_fixture / "sample.txt").read_text(encoding="utf-8")


def test_run_task_revalidates_task_id_before_removing_workspace(tmp_path):
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
    )
    task = dict(evaluator.load()["tasks"][0])
    task["id"] = "../outside"

    with pytest.raises(ValueError, match="filesystem-safe slug"):
        evaluator.run_task(task)

    assert not (tmp_path / "outside").exists()


def test_run_task_revalidates_setup_path_before_writing_fixture(tmp_path):
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
    )
    task = dict(evaluator.load()["tasks"][0])
    task["setup"] = {"kind": "freshness_mismatch", "path": "../outside.txt"}

    with pytest.raises(ValueError, match="setup.path escapes fixture root"):
        evaluator.run_task(task)

    assert not (tmp_path / "outside.txt").exists()


def test_run_task_rejects_workspace_nested_inside_fixture(tmp_path):
    repo_root = tmp_path / "repo"
    fixture = repo_root / "fixture"
    benchmark_path = repo_root / "benchmarks" / "tasks.json"
    fixture.mkdir(parents=True)
    benchmark_path.parent.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")
    benchmark_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "nested-workspace",
                        "prompt": "Inspect README.",
                        "fixture_repo": "fixture",
                        "allowed_tools": ["read_file"],
                        "step_budget": 2,
                        "expected_artifact": "README exists",
                        "verifier_argv": ["python", "-V"],
                        "category": "contract",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    workspace_root = fixture / "generated"
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=workspace_root,
    )

    with pytest.raises(ValueError, match="workspace_root must not be inside fixture_repo"):
        evaluator.run_task(evaluator.load()["tasks"][0])

    assert not workspace_root.exists()


def test_run_fixed_benchmark_reports_metadata_and_success_definition(tmp_path):
    artifact_path = tmp_path / "benchmark-v1.json"
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact_path.exists()
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert persisted == artifact

    assert artifact["schema_version"] == 2
    assert artifact["benchmark"]["schema_version"] == 1
    assert artifact["backend"] == "native"
    assert artifact["summary"] == {
        "total_tasks": 12,
        "eligible_tasks": 12,
        "skipped_tasks": 0,
        "passed": 12,
        "failed": 0,
        "pass_rate": 1.0,
        "within_budget": 12,
        "verifier_passes": 12,
        "within_budget_rate": 1.0,
        "verifier_pass_rate": 1.0,
        "failure_category_counts": {},
    }
    assert artifact["failure_category_counts"] == {}

    reproducibility = artifact["reproducibility"]
    assert reproducibility["model_name"] == "FakeModelClient"
    assert reproducibility["model_version"] == "scripted-deterministic"
    assert reproducibility["fixture_snapshot_id"].startswith("sha256:")
    assert reproducibility["decoding"] == {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_new_tokens": 64,
    }
    assert reproducibility["timezone"] == "Asia/Shanghai"
    assert reproducibility["locale"]

    for row in artifact["rows"]:
        assert not row["fixture_copy_relpath"].startswith("/")
        assert not row["run_dir_relpath"].startswith("/")
        assert not row["task_state_relpath"].startswith("/")
        assert not row["report_relpath"].startswith("/")
        assert row["status"] == "pass"
        assert row["passed"] is True
        assert row["within_budget"] is True
        assert row["verifier_passed"] is True
        assert row["expected_artifact_exists"] is True
        assert row["non_failure_stop_reason"] is True
        assert row["stop_reason"] == "final_answer_returned"


def test_run_fixed_benchmark_covers_recovery_and_durable_contract_rows(tmp_path):
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "benchmark-v1.json",
        workspace_root=tmp_path / "workspaces",
    )

    context_row = next(item for item in artifact["rows"] if item["id"] == "context_reduction_checkpoint")
    durable_row = next(item for item in artifact["rows"] if item["id"] == "durable_promotion_reject")

    trace_path = (tmp_path / "workspaces" / context_row["run_dir_relpath"] / "trace.jsonl").resolve()
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    assert any(
        event.get("event") == "checkpoint_created" and event.get("trigger") == "context_reduction"
        for event in trace_events
    )
    assert durable_row["report"]["durable_rejections"] == [
        "dependency-facts:secret_shaped",
        "key-decisions:transient_task_state",
    ]


def test_paired_review_benchmark_isolates_review_recovery(tmp_path):
    benchmark_path = Path("benchmarks/paired_tasks.json")
    selected_ids = {"paired_review_recovery", "paired_text_region_control"}

    native_evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "native-paired.json",
        workspace_root=tmp_path / "native-workspaces",
        backend="native",
    )
    native_benchmark = native_evaluator.load()
    native_benchmark["tasks"] = [
        task for task in native_benchmark["tasks"] if task["id"] in selected_ids
    ]
    native_evaluator.load = lambda: native_benchmark
    native = native_evaluator.run()

    langgraph_evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "langgraph-paired.json",
        workspace_root=tmp_path / "langgraph-workspaces",
        backend="langgraph",
    )
    langgraph_benchmark = langgraph_evaluator.load()
    langgraph_benchmark["tasks"] = [
        task for task in langgraph_benchmark["tasks"] if task["id"] in selected_ids
    ]
    langgraph_evaluator.load = lambda: langgraph_benchmark
    langgraph = langgraph_evaluator.run()

    native_row = next(row for row in native["rows"] if row["review_expectation"] == "needs_fix")
    langgraph_row = next(
        row for row in langgraph["rows"] if row["review_expectation"] == "needs_fix"
    )

    assert native["summary"]["eligible_tasks"] == 2
    assert native["summary"]["passed"] == 1
    assert native_row["status"] == "fail"
    assert native_row["within_budget"] is True
    assert native_row["verifier_passed"] is False
    assert native_row["review_calls"] == 0

    assert native["review_summary"]["defect_recovery_rate"] == 0.0
    assert native["review_summary"]["control_retention_rate"] == 1.0
    assert native["review_summary"]["average_tool_steps"] == 1.5
    assert native["review_summary"]["review_precision"] is None
    assert native["review_summary"]["review_recall"] == 0.0

    assert langgraph["summary"]["eligible_tasks"] == 2
    assert langgraph["summary"]["passed"] == 2
    assert langgraph_row["status"] == "pass"
    assert langgraph_row["within_budget"] is True
    assert langgraph_row["verifier_passed"] is True
    assert langgraph_row["review_calls"] == 2
    assert langgraph_row["review_retries"] == 1
    assert langgraph["review_summary"]["defect_detection_rate"] == 1.0
    assert langgraph["review_summary"]["defect_recovery_rate"] == 1.0
    assert langgraph["review_summary"]["control_retention_rate"] == 1.0
    assert langgraph["review_summary"]["false_rejection_rate"] == 0.0
    assert langgraph["review_summary"]["average_tool_steps"] == 5.0
    assert langgraph["review_summary"]["review_confusion_matrix"] == {
        "true_positives": 1,
        "false_positives": 0,
        "true_negatives": 1,
        "false_negatives": 0,
    }
    assert langgraph["review_summary"]["review_precision"] == 1.0
    assert langgraph["review_summary"]["review_recall"] == 1.0
    assert langgraph["review_summary"]["review_f1"] == 1.0
    assert langgraph["review_summary"]["review_specificity"] == 1.0


def test_summarize_review_rows_returns_none_without_review_contract():
    assert summarize_review_rows([{"status": "pass", "passed": True}]) is None


def test_run_harness_regression_v2_writes_named_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"

    artifact = run_harness_regression_v2(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact_path.exists()
    assert artifact["summary"]["total_tasks"] == 12
    assert artifact["summary"]["pass_rate"] == 1.0
    assert artifact["summary"]["within_budget_rate"] == 1.0
    assert artifact["summary"]["verifier_pass_rate"] == 1.0


def test_run_task_anchors_paths_to_fixture_copy_even_inside_repo_workspace():
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=Path("docs/review-pack/benchmark-v1.json"),
        workspace_root=Path("."),
    )

    task = next(item for item in evaluator.load()["tasks"] if item["id"] == "readme_intro_locked")
    row = evaluator.run_task(task)

    assert row["status"] == "pass"
    fixture_copy = Path(row["fixture_copy_relpath"])
    readme_path = fixture_copy / "README.md"
    assert "This fixture is a locked benchmark workspace." in readme_path.read_text(encoding="utf-8")


def test_summarize_rows_counts_failure_categories():
    summary = summarize_rows(
        [
            {
                "status": "pass",
                "within_budget": True,
                "verifier_passed": True,
                "expected_artifact_exists": True,
                "non_failure_stop_reason": True,
            },
            {
                "status": "fail",
                "within_budget": False,
                "verifier_passed": False,
                "expected_artifact_exists": False,
                "non_failure_stop_reason": False,
                "failure_category": "verifier_failed",
            },
            {
                "status": "fail",
                "within_budget": False,
                "verifier_passed": True,
                "expected_artifact_exists": True,
                "non_failure_stop_reason": False,
                "failure_category": "budget_exceeded",
            },
        ]
    )

    assert summary["total_tasks"] == 3
    assert summary["eligible_tasks"] == 3
    assert summary["skipped_tasks"] == 0
    assert summary["passed"] == 1
    assert summary["failed"] == 2
    assert summary["pass_rate"] == pytest.approx(1 / 3)
    assert summary["within_budget"] == 1
    assert summary["verifier_passes"] == 2
    assert summary["failure_category_counts"] == {
        "budget_exceeded": 1,
        "verifier_failed": 1,
    }


def test_summarize_rows_excludes_skipped_from_denominators():
    summary = summarize_rows(
        [
            {"status": "pass", "passed": True, "within_budget": True, "verifier_passed": True},
            {
                "status": "skipped",
                "passed": None,
                "within_budget": None,
                "verifier_passed": None,
                "failure_category": "backend_not_applicable",
            },
        ]
    )

    assert summary["total_tasks"] == 2
    assert summary["eligible_tasks"] == 1
    assert summary["skipped_tasks"] == 1
    assert summary["pass_rate"] == 1.0
    assert summary["failure_category_counts"] == {}


def test_v1_evaluation_artifact_is_normalized_for_readers():
    normalized = normalize_evaluation_artifact(
        {
            "schema_version": 1,
            "benchmark": {"source": "benchmarks/coding_tasks.json", "task_count": 1},
            "rows": [{"id": "legacy", "status": "pass", "passed": True}],
        }
    )

    assert normalized["schema_version"] == 2
    assert normalized["backend"] == "native"
    assert normalized["benchmark"]["schema_version"] == 1
    assert normalized["rows"][0]["events"] == []
    assert normalized["rows"][0]["requested_task_mode"] == ""
    assert normalized["rows"][0]["resolved_intent"] == ""
    assert normalized["rows"][0]["intent_source"] == ""
    assert normalized["rows"][0]["intent_attempts"] == 0
    assert normalized["rows"][0]["answer_attempts"] == 0


def test_old_v2_artifact_gets_route_defaults_without_recomputing_summary():
    original_summary = {"sentinel": "preserved", "failure_category_counts": {}}
    normalized = normalize_evaluation_artifact(
        {
            "schema_version": 2,
            "backend": "native",
            "benchmark": {"schema_version": 1},
            "rows": [{"id": "old-v2", "status": "pass"}],
            "summary": original_summary,
            "failure_category_counts": {},
        }
    )

    assert normalized["summary"] is original_summary
    assert normalized["rows"][0]["requested_task_mode"] == ""
    assert normalized["rows"][0]["resolved_intent"] == ""
    assert normalized["rows"][0]["intent_source"] == ""
    assert normalized["rows"][0]["intent_attempts"] == 0
    assert normalized["rows"][0]["answer_attempts"] == 0


def test_normal_skipped_and_harness_rows_share_route_metadata_fields(tmp_path):
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
    )
    task = evaluator.load()["tasks"][0]
    normal = evaluator.run_task(task)
    skipped = evaluator._skipped_row(task)
    harness = evaluator._harness_failure_row(task, "RuntimeError")
    route_keys = {
        "requested_task_mode",
        "resolved_intent",
        "intent_source",
        "intent_attempts",
        "answer_attempts",
    }

    assert route_keys <= normal.keys()
    assert normal["requested_task_mode"] == ""
    assert route_keys <= skipped.keys()
    assert route_keys <= harness.keys()


def test_backend_not_applicable_returns_uniform_skipped_row(tmp_path):
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        backend="langgraph",
    )

    row = evaluator.run_task(evaluator.load()["tasks"][0])

    assert row["status"] == "skipped"
    assert row["failure_category"] == "backend_not_applicable"
    assert row["within_budget"] is None
    assert row["verifier_passed"] is None
    assert row["execution_started"] is False
    assert row["task_state"] == {}
    assert row["events"] == []


def test_task_setup_failure_does_not_abort_artifact(tmp_path):
    class FailingFactory:
        def __init__(self):
            self.calls = 0

        def __call__(self, task, workspace):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider details must not leak")
            from pico.providers.clients import FakeModelClient

            return FakeModelClient(["<final>Done.</final>"])

    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=FailingFactory(),
    )
    benchmark = evaluator.load()
    benchmark["tasks"] = benchmark["tasks"][:2]
    evaluator.load = lambda: benchmark

    artifact = evaluator.run()

    assert len(artifact["rows"]) == 2
    assert artifact["rows"][0]["failure_category"] == "harness_error"
    assert "provider details" not in artifact["rows"][0]["final_answer"]
    assert artifact["rows"][1]["execution_started"] is True


def test_native_model_failure_is_classified_and_finalized(tmp_path):
    from pico.providers.clients import FakeModelClient

    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda task, workspace: FakeModelClient([]),
    )

    row = evaluator.run_task(evaluator.load()["tasks"][0])

    assert row["status"] == "fail"
    assert row["failure_category"] == "model_error"
    assert row["stop_reason"] == "model_error"
    assert row["task_state"]["status"] == "failed"
    assert row["report"]
    assert any(event.get("event") == "run_finished" for event in row["events"])
    assert "ran out of outputs" not in json.dumps(row)

import shutil
from pathlib import Path

import pytest

from pico.evaluation.backends import (
    BackendRunResult,
    HarnessModelClientAdapter,
    ModelBoundaryError,
    build_backend_runner,
)
from pico.providers.clients import FakeModelClient
from pico.task_state import TaskState


def test_harness_model_adapter_classifies_without_exposing_message():
    adapter = HarnessModelClientAdapter(FakeModelClient([]))

    with pytest.raises(ModelBoundaryError) as caught:
        adapter.complete("prompt", 10)

    assert caught.value.stop_reason == "model_error"
    assert str(caught.value) == "model_call_failed"
    assert "outputs" not in str(caught.value)


def test_native_backend_does_not_import_optional_langgraph(monkeypatch):
    imported = []
    original_import = __import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", recording_import)
    runner = build_backend_runner("native")

    assert runner.__class__.__name__ == "NativeBackendRunner"
    assert not any(name.startswith("langgraph_pico") for name in imported)


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown backend"):
        build_backend_runner("missing")


def test_backend_run_result_deep_copies_run_metadata_and_defaults_to_empty():
    task_state = TaskState.create(task_id="task", user_request="request")
    metadata = {"resolved_intent": "read_only", "nested": {"attempts": [1]}}
    result = BackendRunResult(task_state, "answer", object(), run_metadata=metadata)

    metadata["nested"]["attempts"].append(2)

    assert result.run_metadata == {
        "resolved_intent": "read_only",
        "nested": {"attempts": [1]},
    }
    assert BackendRunResult(task_state, "answer", object()).run_metadata == {}


def _native_task_with_fixture(tmp_path, outputs):
    import shutil

    from pico.run_store import RunStore
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    source = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bench_repo_readme"
    fixture_root = tmp_path / "gate-fixture"
    shutil.copytree(source, fixture_root)
    workspace = WorkspaceContext.build(fixture_root, repo_root_override=fixture_root)
    task = {
        "id": "gate_verify",
        "prompt": "Replace the placeholder with the locked text.",
        "fixture_repo": "tests/fixtures/bench_repo_readme",
        "allowed_tools": ["read_file", "patch_file"],
        "step_budget": 4,
        "expected_artifact": "README locked",
        "verifier": "true",
        "category": "documentation",
    }
    runner = build_backend_runner("native")
    result = runner.run_task(
        task,
        workspace,
        SessionStore(fixture_root / ".pico" / "sessions"),
        RunStore(fixture_root / ".pico" / "runs"),
        fixture_root,
        model_client=FakeModelClient(outputs),
    )
    return result, fixture_root


def test_native_review_gate_passes_when_change_applied():
    outputs = [
        '<tool>{"name": "read_file", "args": {"path": "README.md"}}</tool>',
        '<tool>{"name": "patch_file", "args": {"path": "README.md", "old_text": "This is a placeholder benchmark fixture.", "new_text": "This fixture is a locked benchmark workspace."}}</tool>',
        "<final>Done</final>",
    ]
    result, fixture_root = _native_task_with_fixture(Path(__file__).parent / "native-gate-pass", outputs)
    state = result.task_state
    assert state.status == "completed"
    assert not state.error_code  # gate 不误判
    assert (
        "locked benchmark workspace"
        in (fixture_root / "README.md").read_text(encoding="utf-8")
    )
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_review_gate_rejects_when_change_not_applied():
    # 模型声称完成但 patch 参数错误（old_string 而非 old_text）→ 无写证据 → gate 拦截
    outputs = [
        '<tool>{"name": "read_file", "args": {"path": "README.md"}}</tool>',
        '<tool>{"name": "patch_file", "args": {"path": "README.md", "old_string": "placeholder", "new_string": "locked"}}</tool>',
        "<final>Done</final>",
    ]
    result, fixture_root = _native_task_with_fixture(Path(__file__).parent / "native-gate-reject", outputs)
    state = result.task_state
    assert state.review_status == "needs_fix"
    assert state.error_code == "completion_gate_failed"
    shutil.rmtree(fixture_root, ignore_errors=True)

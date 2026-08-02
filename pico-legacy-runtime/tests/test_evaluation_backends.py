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
    assert str(caught.value) == "model call failed: RuntimeError"
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

from pico.event_sink import CompositeSink, EventCollector, JsonlSink, NullSink
from pico.run_store import RunStore
from pico.task_state import TaskState


class FailingSink:
    def emit(self, task_state, event_type, payload):
        raise RuntimeError("configured sink failed with private details")


def test_jsonl_sink_preserves_existing_trace_output(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = TaskState.create(run_id="run_jsonl", task_id="task_jsonl", user_request="Trace")
    store.start_run(state)

    JsonlSink(store).emit(state, "run_started", {"event": "run_started"})

    assert store.trace_path(state).read_text(encoding="utf-8").strip()


def test_null_sink_does_not_create_trace_file(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = TaskState.create(run_id="run_null", task_id="task_null", user_request="Do not trace")
    store.start_run(state)

    NullSink().emit(state, "run_started", {"event": "run_started"})

    assert not store.trace_path(state).exists()


def test_composite_sink_keeps_collector_when_configured_sink_fails():
    collector = EventCollector()
    sink = CompositeSink(collector, FailingSink())
    state = TaskState.create(run_id="run_composite", task_id="task_composite", user_request="Collect")

    sink.emit(state, "run_started", {"event": "run_started", "created_at": "now"})

    events = collector.snapshot()
    assert events[0]["event"] == "run_started"
    assert events[1] == {
        "event": "event_sink_failed",
        "created_at": "now",
        "source_event": "run_started",
        "sink": "FailingSink",
        "error_type": "RuntimeError",
    }
    assert "private details" not in str(events)


def test_event_collector_snapshot_is_isolated_from_mutation():
    collector = EventCollector()
    state = TaskState.create(run_id="run_snapshot", task_id="task_snapshot", user_request="Collect")
    payload = {"event": "tool_executed", "nested": {"paths": ["a.py"]}}
    collector.emit(state, "tool_executed", payload)

    payload["nested"]["paths"].append("b.py")
    snapshot = collector.snapshot()
    snapshot[0]["nested"]["paths"].append("c.py")

    assert collector.snapshot()[0]["nested"]["paths"] == ["a.py"]

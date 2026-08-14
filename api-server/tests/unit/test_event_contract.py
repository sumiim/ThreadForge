from threadforge_api.domain.events import PublicEvent, event_phase


def test_event_phase_maps_types_to_stable_lanes():
    assert event_phase("plan.created") == "plan"
    assert event_phase("model.started") == "model"
    assert event_phase("assistant.commentary") == "talk"
    assert event_phase("tool.started") == "execute"
    assert event_phase("approval.required") == "approval"
    assert event_phase("review.completed") == "review"
    assert event_phase("message.completed") == "final"
    assert event_phase("task.completed") == "final"
    assert event_phase("task.queued") == "system"


def test_public_event_to_dict_lifts_trace_phase_and_attributes():
    event = PublicEvent(
        event_id="evt_1",
        sequence=3,
        type="tool.started",
        task_id="task_1",
        run_id="run_1",
        data={"tool_name": "read_file", "parent_event_id": "model_round_1"},
    ).to_dict()

    # trace_id defaults to run_id, phase falls back to the type mapping, and
    # attributes stays in lockstep with the redacted data payload.
    assert event["trace_id"] == "run_1"
    assert event["phase"] == "execute"
    assert event["attributes"] == event["data"] == {"tool_name": "read_file", "parent_event_id": "model_round_1"}


def test_public_event_explicit_metadata_wins_over_fallback():
    event = PublicEvent(
        event_id="evt_2",
        sequence=1,
        type="agent.state",
        task_id="task_1",
        run_id="run_1",
        trace_id="trace_1",
        phase="ANALYZE_CONTEXT",
        status="running",
        summary="reading files",
        data={"phase": "ANALYZE_CONTEXT"},
    ).to_dict()

    assert event["trace_id"] == "trace_1"
    assert event["phase"] == "ANALYZE_CONTEXT"
    assert event["status"] == "running"
    assert event["summary"] == "reading files"
    assert event["attempt"] is None

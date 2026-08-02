"""Best-effort lifecycle finalization shared by child runtimes."""

from .task_state import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_PERSISTENCE_ERROR,
    STOP_REASON_RUNTIME_ERROR,
)


def finalize_failed_run(agent, task_state, *, error_type, duration_ms, stop_reason=STOP_REASON_RUNTIME_ERROR):
    if agent is None or task_state is None:
        return
    if stop_reason not in {
        STOP_REASON_MODEL_ERROR,
        STOP_REASON_RUNTIME_ERROR,
        STOP_REASON_PERSISTENCE_ERROR,
    }:
        stop_reason = STOP_REASON_RUNTIME_ERROR
    if task_state.status == STATUS_RUNNING:
        task_state.stop(
            stop_reason,
            status=STATUS_FAILED,
            final_answer=f"agent run failed: {error_type}",
        )

    finalize_run(agent, task_state, duration_ms=duration_ms, error_type=error_type)


def finalize_run(agent, task_state, *, duration_ms, error_type=""):
    """Persist lifecycle artifacts independently so one write cannot skip the rest."""
    if agent is None or task_state is None:
        return

    persistence_error = False
    try:
        agent.run_store.write_task_state(task_state)
    except Exception:
        persistence_error = True
    try:
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "run_duration_ms": int(duration_ms),
                **({"error_type": str(error_type)} if error_type else {}),
            },
        )
    except Exception:
        persistence_error = True
    if persistence_error:
        task_state.stop(
            STOP_REASON_PERSISTENCE_ERROR,
            status=STATUS_FAILED,
            final_answer="agent run finalization failed: persistence error",
        )
    try:
        report = agent.redact_artifact(agent.build_report(task_state))
        report["run_duration_ms"] = int(duration_ms)
        agent.run_store.write_report(task_state, report)
    except Exception:
        task_state.stop(
            STOP_REASON_PERSISTENCE_ERROR,
            status=STATUS_FAILED,
            final_answer="agent run finalization failed: persistence error",
        )
        try:
            agent.run_store.write_task_state(task_state)
        except Exception:
            pass

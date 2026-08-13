"""Fault-injection tests verifying fail-closed contracts (offline)."""

from __future__ import annotations

import json
import sys as _sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pico.approval import ApprovalOutcome
from pico.execution_hooks import RunCancelled

from threadforge_api.infrastructure.cancellation import CancellationToken
from threadforge_api.infrastructure.execution_boundary import ExecutionBoundary
from threadforge_api.infrastructure.recovery_journal import RecoveryJournal
from threadforge_api.infrastructure.run_gate import RunGate
from threadforge_api.main import create_app

from ..conftest import langgraph_review, langgraph_router, wait_for_status, wait_for_terminal


def test_unrecoverable_task_persistence_failure_degrades_service(client, session_id):
    container = client.app.state.container

    def fail_update(*args, **kwargs):
        raise OSError("disk unavailable")

    container.task_repo.update = fail_update
    first = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "first"})
    assert first.status_code == 202
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get("/health/ready").status_code == 200:
        time.sleep(0.02)
    assert client.get("/health/ready").status_code == 503
    second = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "second"})
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "task_runner_unavailable"


def test_approval_second_write_failure_rolls_back(client, session_id, model_outputs):
    model_outputs[:] = [
        langgraph_router("code_change"),
        '<tool>{"name":"write_file","args":{"path":"x.txt","content":"x"}}</tool>',
    ]
    created = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "write"}).json()
    waiting = wait_for_status(client, created["task_id"], "waiting_for_approval")
    approval_id = waiting["pending_approval"]["approval_id"]
    container = client.app.state.container
    original_update = container.approval_repo.update
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected second-write failure")
        return original_update(*args, **kwargs)

    container.approval_repo.update = fail_once
    response = client.post(
        f"/api/v1/tasks/{created['task_id']}/approvals/{approval_id}",
        json={"decision": "approved"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "persistence_unavailable"
    assert container.task_repo.get(created["task_id"]).status.value == "waiting_for_approval"
    assert container.approval_repo.get(approval_id).status.value == "pending"
    assert client.get("/health/ready").status_code == 200
    client.post(f"/api/v1/tasks/{created['task_id']}/cancel")


def test_startup_commits_empty_recovery_intent(settings, model_factory):
    journal = RecoveryJournal(settings.data_dir / "recovery.jsonl")
    journal.begin("approval_request", task_id="task_" + "a" * 32, approval_id="apr_" + "b" * 32)
    app = create_app(settings, model_client_factory=model_factory)
    with TestClient(app) as test_client:
        assert test_client.get("/health/ready").status_code == 200
        assert test_client.app.state.container.recovery_journal.incomplete() == []


def test_recovery_journal_ignores_only_a_truncated_tail(settings):
    journal = RecoveryJournal(settings.data_dir / "recovery.jsonl")
    transition_id = journal.begin(
        "approval_request",
        task_id="task_" + "a" * 32,
        approval_id="apr_" + "b" * 32,
    )
    with journal.path.open("ab") as handle:
        handle.write(b'{"phase":"comm')

    assert [item["transition_id"] for item in journal.incomplete()] == [transition_id]
    assert journal.path.read_bytes().endswith(b"\n")
    journal.commit(transition_id)
    assert journal.incomplete() == []

    journal.path.write_bytes(journal.path.read_bytes() + b'{"phase":"commit"}\n')
    with pytest.raises(ValueError, match="recovery record"):
        journal.incomplete()


def test_startup_repairs_terminal_task_run_mismatch(settings, model_factory, model_outputs):
    app = create_app(settings, model_client_factory=model_factory)
    with TestClient(app) as test_client:
        session = test_client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()
        model_outputs[:] = [langgraph_router("read_only"), "<final>done</final>", langgraph_review("read_only")]
        created = test_client.post(
            "/api/v1/tasks",
            json={"session_id": session["session_id"], "input": "run"},
        ).json()
        terminal = wait_for_terminal(test_client, created["task_id"])

    task_path = settings.data_dir / "tasks" / f"{terminal['task_id']}.json"
    task_data = json.loads(task_path.read_text(encoding="utf-8"))
    task_data["status"] = "failed"
    task_data["stop_reason"] = "injected_failure"
    task_path.write_text(json.dumps(task_data), encoding="utf-8")
    run_dir = settings.data_dir / "runs" / terminal["run_id"]
    state_path = run_dir / "task_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "running"
    state["stop_reason"] = ""
    state_path.write_text(json.dumps(state), encoding="utf-8")

    restarted = create_app(settings, model_client_factory=model_factory)
    with TestClient(restarted) as test_client:
        assert test_client.get("/health/ready").status_code == 200
        repaired_state = json.loads(state_path.read_text(encoding="utf-8"))
        repaired_report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        assert repaired_state["status"] == "failed"
        assert repaired_state["stop_reason"] == "injected_failure"
        assert repaired_report["status"] == "failed"
        assert repaired_report["stop_reason"] == "injected_failure"


def test_startup_preserves_completed_run_when_task_write_was_lost(settings, model_factory, model_outputs):
    app = create_app(settings, model_client_factory=model_factory)
    with TestClient(app) as test_client:
        session = test_client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()
        model_outputs[:] = [langgraph_router("read_only"), "<final>completed before crash</final>", langgraph_review("read_only")]
        created = test_client.post(
            "/api/v1/tasks",
            json={"session_id": session["session_id"], "input": "run"},
        ).json()
        terminal = wait_for_terminal(test_client, created["task_id"])

    task_path = settings.data_dir / "tasks" / f"{terminal['task_id']}.json"
    task_data = json.loads(task_path.read_text(encoding="utf-8"))
    task_data["status"] = "running"
    task_data["stop_reason"] = None
    task_data["final_answer"] = None
    task_path.write_text(json.dumps(task_data), encoding="utf-8")
    run_dir = settings.data_dir / "runs" / terminal["run_id"]
    (run_dir / "report.json").unlink()
    (run_dir / "trace.jsonl").unlink()

    restarted = create_app(settings, model_client_factory=model_factory)
    with TestClient(restarted) as test_client:
        recovered = test_client.get(f"/api/v1/tasks/{terminal['task_id']}").json()
        assert recovered["status"] == "completed"
        assert recovered["stop_reason"] == "final_answer_returned"
        assert recovered["final_answer"] == "completed before crash"
    state = json.loads(
        (run_dir / "task_state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "completed"
    assert (run_dir / "report.json").is_file()
    assert (run_dir / "trace.jsonl").is_file()


def test_runner_initialization_failure_still_has_all_artifacts(settings):
    def fail_factory():
        raise RuntimeError("injected init failure")

    app = create_app(settings, model_client_factory=fail_factory)
    with TestClient(app) as test_client:
        session_id = test_client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()["session_id"]
        created = test_client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
        terminal = wait_for_terminal(test_client, created["task_id"])
        assert terminal["status"] == "failed"
        listing = test_client.get(f"/api/v1/runs/{terminal['run_id']}/artifacts")
        assert listing.status_code == 200
        assert {item["name"] for item in listing.json()["items"]} == {"task_state", "trace", "report"}


def test_runner_thread_start_failure_is_auditable(client, session_id):
    container = client.app.state.container

    def fail_register(_request):
        raise RuntimeError("injected thread start failure")

    container.runner.register = fail_register
    response = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "x"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "task_runner_unavailable"
    task_id = error["details"]["task_id"]
    task = container.task_repo.get(task_id)
    assert task.status.value == "failed"
    listing = client.get(f"/api/v1/runs/{task.run_id}/artifacts")
    assert {item["name"] for item in listing.json()["items"]} == {"task_state", "trace", "report"}


def test_start_transition_serializes_with_immediate_cancel(client, session_id):
    container = client.app.state.container
    original_update = container.task_repo.update
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def block_worker_start(*args, **kwargs):
        if threading.current_thread().name.startswith("run-") and not worker_entered.is_set():
            worker_entered.set()
            assert release_worker.wait(timeout=5)
        return original_update(*args, **kwargs)

    container.task_repo.update = block_worker_start
    created = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "x"},
    ).json()
    assert worker_entered.wait(timeout=5)

    cancel_thread = threading.Thread(
        target=container.runner.cancel,
        args=(created["task_id"],),
    )
    cancel_thread.start()
    time.sleep(0.05)
    assert cancel_thread.is_alive(), "cancel bypassed the worker start transition gate"

    release_worker.set()
    cancel_thread.join(timeout=5)
    assert not cancel_thread.is_alive()
    terminal = wait_for_terminal(client, created["task_id"])
    assert terminal["status"] == "cancelled"


def test_cancel_wins_approval_expiry_race(client, session_id, model_outputs):
    container = client.app.state.container
    approval_ids = []

    def cancel_while_expiring(approval_id, run, *_args):
        approval_ids.append(approval_id)
        assert container.runner.cancel(run.task_id)
        return ApprovalOutcome.EXPIRED

    container.approval_gate._wait = cancel_while_expiring
    model_outputs[:] = [
        langgraph_router("code_change"),
        '<tool>{"name":"write_file","args":{"path":"x.txt","content":"x"}}</tool>',
    ]
    created = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "write"},
    ).json()

    terminal = wait_for_terminal(client, created["task_id"])
    assert terminal["status"] == "cancelled"
    assert len(approval_ids) == 1
    approval = container.approval_repo.get(approval_ids[0])
    assert approval.status.value == "cancelled"
    assert approval.decision == "cancelled"


def test_repeated_cancel_retries_shell_cleanup(client, session_id, model_outputs):
    model_outputs[:] = [
        langgraph_router("code_change"),
        '<tool>{"name":"write_file","args":{"path":"x.txt","content":"x"}}</tool>',
    ]
    created = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "write"},
    ).json()
    wait_for_status(client, created["task_id"], "waiting_for_approval")
    run = client.app.state.container.runner.get_context(created["task_id"])
    cleanup_calls = []

    def cleanup():
        cleanup_calls.append(True)
        return True

    run.terminate_shell = cleanup
    run.token.cancel()
    assert client.app.state.container.runner.cancel(created["task_id"])

    assert cleanup_calls == [True]
    terminal = wait_for_terminal(client, created["task_id"])
    assert terminal["status"] == "cancelled"


def test_shell_cleanup_failure_degrades_api_and_fails_task(client, session_id, model_outputs):
    model_outputs[:] = [
        langgraph_router("code_change"),
        '<tool>{"name":"write_file","args":{"path":"x.txt","content":"x"}}</tool>',
    ]
    created = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "write"},
    ).json()
    wait_for_status(client, created["task_id"], "waiting_for_approval")
    container = client.app.state.container
    run = container.runner.get_context(created["task_id"])
    run.adapter.terminate_shell = lambda: False

    response = client.post(f"/api/v1/tasks/{created['task_id']}/cancel")
    assert response.status_code in {200, 202}
    terminal = wait_for_terminal(client, created["task_id"])
    assert terminal["status"] == "failed"
    assert terminal["stop_reason"] == "process_cleanup_failed"
    assert client.get("/health/ready").status_code == 503


def test_approval_wait_failure_does_not_leave_pending_record(client, session_id, model_outputs):
    approval_ids = []

    def fail_wait(approval_id, *_args):
        approval_ids.append(approval_id)
        raise OSError("injected approval read failure")

    client.app.state.container.approval_gate._wait = fail_wait
    model_outputs[:] = [
        langgraph_router("code_change"),
        '<tool>{"name":"write_file","args":{"path":"x.txt","content":"x"}}</tool>',
    ]
    created = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "write"},
    ).json()

    terminal = wait_for_terminal(client, created["task_id"])
    assert terminal["status"] == "failed"
    assert len(approval_ids) == 1
    approval = client.app.state.container.approval_repo.get(approval_ids[0])
    assert approval.status.value == "cancelled"


def test_ready_rejects_runtime_repository_corruption(client, session_id, workspace_env):
    session_path = workspace_env["data_dir"] / "sessions" / f"{session_id}.json"
    session_path.write_text("[]", encoding="utf-8")
    assert client.get("/health/ready").status_code == 503

    session_path.write_text(json.dumps({"id": session_id, "workspace_id": "w1", "history": []}), encoding="utf-8")
    task_path = workspace_env["data_dir"] / "tasks" / ("task_" + "a" * 32 + ".json")
    task_path.write_text("{broken", encoding="utf-8")
    assert client.get("/health/ready").status_code == 503


def test_startup_reconciliation_failure_rejects_new_tasks(settings, model_factory):
    tasks_dir = settings.data_dir / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / ("task_" + "a" * 32 + ".json")).write_text(
        "{broken",
        encoding="utf-8",
    )
    app = create_app(settings, model_client_factory=model_factory)

    with TestClient(app) as test_client:
        session = test_client.post(
            "/api/v1/sessions",
            json={"workspace_id": "w1"},
        ).json()
        response = test_client.post(
            "/api/v1/tasks",
            json={"session_id": session["session_id"], "input": "x"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "task_runner_unavailable"


def test_after_tool_rejects_event_after_cancellation():
    class Publisher:
        def __init__(self):
            self.events = []

        def publish(self, *args):
            self.events.append(args[2])

    publisher = Publisher()
    token = CancellationToken()
    boundary = ExecutionBoundary(
        publisher=publisher,
        task_id="task_test",
        run_id="run_test",
        gate=RunGate(),
        token=token,
    )
    token.cancel()
    with pytest.raises(RunCancelled):
        boundary.after_tool(
            SimpleNamespace(last_tool="write_file"),
            SimpleNamespace(metadata={"tool_status": "ok"}),
        )
    assert publisher.events == []


def test_execution_boundary_rejects_event_after_terminal_fence():
    class Publisher:
        def __init__(self):
            self.events = []

        def publish(self, *args):
            self.events.append(args[2])

    publisher = Publisher()
    gate = RunGate()
    boundary = ExecutionBoundary(
        publisher=publisher,
        task_id="task_test",
        run_id="run_test",
        gate=gate,
        token=CancellationToken(),
    )
    gate.close()

    with pytest.raises(RunCancelled):
        boundary.before_model(SimpleNamespace())
    assert publisher.events == []


def _shell_long():
    if _sys.platform == "win32":
        return "ping -n 60 127.0.0.1"
    return "sleep 60"


def test_durable_memory_not_written_in_web_path(client, session_id, model_outputs, workspace_env):
    """Web Run must not create .pico/memory/ in the workspace."""
    model_outputs[:] = [
        langgraph_router("read_only"),
        "<final>Project convention: Use constrained tools.\nDecision: Keep things simple.\n</final>",
        langgraph_review("read_only"),
    ]
    task = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "Remember the stable facts you already discovered as durable memory."},
    ).json()
    wait_for_terminal(client, task["task_id"])
    memory_root = workspace_env["wsdir"] / ".pico" / "memory"
    assert not memory_root.exists(), f"Web run polluted workspace: {memory_root}"


def test_artifact_content_is_redacted(client, session_id, model_outputs):
    """Artifact endpoint must redact environment-level API key values."""
    from unittest.mock import patch

    secret = "ak_test_secret_in_env_only"
    with patch.dict("os.environ", {"OPENAI_API_KEY": secret}):
        model_outputs[:] = [langgraph_router("read_only"), f"<final>my api key is {secret}</final>", langgraph_review("read_only")]
        task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "what is my api key"}).json()
        terminal = wait_for_terminal(client, task["task_id"])
        run_id = terminal["run_id"]
        report = client.get(f"/api/v1/runs/{run_id}/artifacts/report")
        assert report.status_code == 200
        raw = report.text
        assert secret not in raw, f"secret leaked in artifact: {raw[:500]}"


def test_task_input_bounds_422(client, session_id):
    """max_steps out of range or empty input must return 422."""
    resp = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "   ", "max_steps": 6})
    assert resp.status_code == 422

    resp = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "ok", "max_steps": 0})
    assert resp.status_code == 422

    resp = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "ok", "max_steps": 999})
    assert resp.status_code == 422


def test_sse_terminal_reconnect_closes_immediately(client, session_id, model_outputs):
    """Reconnecting to a terminal task must send snapshot and close, no indefinite heartbeat."""
    model_outputs[:] = [langgraph_router("read_only"), "<final>done</final>", langgraph_review("read_only")]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
    tid = task["task_id"]
    wait_for_terminal(client, tid)

    events = []
    with client.stream("GET", f"/api/v1/tasks/{tid}/events") as response:
        for line in response.iter_lines():
            if line.startswith("event:"):
                events.append(line[len("event:"):].strip())
    assert events[:1] == ["task.snapshot"]
    # Must not have heartbeat pings — the stream should close after snapshot
    # for a terminal task.
    assert len(events) <= 2


def test_active_slot_released_after_cancelled(client, session_id, model_outputs):
    """After a task is cancelled, a new task can be created immediately."""
    model_outputs[:] = [
        langgraph_router("code_change"),
        '<tool>{"name":"run_shell","args":{"command":"%s","timeout":30}}</tool>' % _shell_long(),
    ]
    first = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "first"}).json()
    tid1 = first["task_id"]
    waiting = wait_for_status(client, tid1, "waiting_for_approval")
    approval = waiting["pending_approval"]
    client.post(f"/api/v1/tasks/{tid1}/approvals/{approval['approval_id']}", json={"decision": "approved"})
    time.sleep(0.3)
    client.post(f"/api/v1/tasks/{tid1}/cancel")
    wait_for_terminal(client, tid1, timeout=15)

    second = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "second"})
    assert second.status_code == 202, f"slot stuck after cancel: {second.json() if second.text else second.status_code}"

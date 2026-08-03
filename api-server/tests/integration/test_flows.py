"""End-to-end integration flows with FakeModelClient (offline)."""

from __future__ import annotations

import sys
import time

from ..conftest import wait_for_status, wait_for_terminal


def _shell_long_command():
    if sys.platform == "win32":
        return "ping -n 60 127.0.0.1"
    return "sleep 30"


def test_read_only_task_completes_and_artifacts_queryable(client, session_id, model_outputs):
    model_outputs[:] = ["<final>plain answer</final>"]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
    assert task["status"] in {"queued", "running"}
    terminal = wait_for_terminal(client, task["task_id"])
    assert terminal["status"] == "completed"
    assert terminal["final_answer"] == "plain answer"

    run_id = terminal["run_id"]
    artifacts = client.get(f"/api/v1/runs/{run_id}/artifacts").json()["items"]
    names = {item["name"] for item in artifacts}
    assert names == {"task_state", "trace", "report"}
    assert client.get(f"/api/v1/runs/{run_id}/artifacts/report").status_code == 200


def test_approve_flow_executes_exactly_the_requested_tool(client, session_id, model_outputs, workspace_env):
    model_outputs[:] = [
        '<tool>{"name":"write_file","args":{"path":"out.txt","content":"hi"}}</tool>',
        "<final>written</final>",
    ]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "write file"}).json()
    tid = task["task_id"]
    waiting = wait_for_status(client, tid, "waiting_for_approval")
    approval = waiting["pending_approval"]
    assert approval["tool_name"] == "write_file"
    assert approval["approval_id"].startswith("apr_")

    resp = client.post(f"/api/v1/tasks/{tid}/approvals/{approval['approval_id']}", json={"decision": "approved"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    terminal = wait_for_terminal(client, tid)
    assert terminal["status"] == "completed"
    assert terminal["final_answer"] == "written"
    assert (workspace_env["wsdir"] / "out.txt").read_text(encoding="utf-8") == "hi"


def test_reject_flow_leaves_file_unchanged(client, session_id, model_outputs, workspace_env):
    target = workspace_env["wsdir"] / "data.txt"
    target.write_text("keep me", encoding="utf-8")
    model_outputs[:] = [
        '<tool>{"name":"patch_file","args":{"path":"data.txt","old_text":"keep me","new_text":"changed"}}</tool>',
        "<final>rejected then done</final>",
    ]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "patch"}).json()
    tid = task["task_id"]
    waiting = wait_for_status(client, tid, "waiting_for_approval")
    approval = waiting["pending_approval"]
    resp = client.post(f"/api/v1/tasks/{tid}/approvals/{approval['approval_id']}", json={"decision": "rejected"})
    assert resp.status_code == 200

    terminal = wait_for_terminal(client, tid)
    assert terminal["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "keep me"


def test_active_task_conflict_returns_409(client, session_id, model_outputs):
    model_outputs[:] = [
        '<tool>{"name":"run_shell","args":{"command":"%s","timeout":30}}</tool>' % _shell_long_command()
    ]
    first = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "first"}).json()
    wait_for_status(client, first["task_id"], "waiting_for_approval")

    second = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "second"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "active_task_exists"
    assert second.json()["error"]["details"]["task_id"] == first["task_id"]

    # cleanup: approve the shell so it finishes quickly
    approval = client.get(f"/api/v1/tasks/{first['task_id']}").json()["pending_approval"]
    client.post(f"/api/v1/tasks/{first['task_id']}/approvals/{approval['approval_id']}", json={"decision": "approved"})
    wait_for_terminal(client, first["task_id"], timeout=40)


def test_cancel_during_shell_terminates_and_reaches_cancelled(client, session_id, model_outputs):
    model_outputs[:] = [
        '<tool>{"name":"run_shell","args":{"command":"%s","timeout":30}}</tool>' % _shell_long_command()
    ]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "long"}).json()
    tid = task["task_id"]
    waiting = wait_for_status(client, tid, "waiting_for_approval")
    approval = waiting["pending_approval"]
    client.post(f"/api/v1/tasks/{tid}/approvals/{approval['approval_id']}", json={"decision": "approved"})
    # let the shell start
    time.sleep(0.5)
    cancel = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert cancel.status_code == 202
    terminal = wait_for_terminal(client, tid, timeout=20)
    assert terminal["status"] == "cancelled"
    assert terminal["stop_reason"] == "user_cancelled"


def test_approval_rejection_has_audit(client, session_id, model_outputs):
    model_outputs[:] = [
        '<tool>{"name":"write_file","args":{"path":"a.txt","content":"x"}}</tool>',
        "<final>end</final>",
    ]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
    tid = task["task_id"]
    waiting = wait_for_status(client, tid, "waiting_for_approval")
    approval = waiting["pending_approval"]
    client.post(f"/api/v1/tasks/{tid}/approvals/{approval['approval_id']}", json={"decision": "rejected"})
    terminal = wait_for_terminal(client, tid)
    assert terminal["status"] == "completed"
    run_id = terminal["run_id"]
    trace = client.get(f"/api/v1/runs/{run_id}/artifacts/trace").text
    assert "approval" in trace or "rejected" in trace

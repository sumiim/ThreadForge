"""End-to-end local Worker control-plane flow over its public APIs."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from fastapi import WebSocketDisconnect

from threadforge_api.api.dependencies import get_actor
from threadforge_api.domain.identity import Actor


def _pair(client, name="Test laptop"):
    code = client.post("/api/v1/devices/pairing-codes", json={}).json()["code"]
    response = client.post("/api/v1/workers/pair", json={"code": code, "name": name})
    assert response.status_code == 200
    return response.json()


def _hello(socket, workspace_id="ws_" + "a" * 32):
    socket.send_json(
        {
            "type": "hello",
            "version": "0.1.0",
            "model": "fake-local-model",
            "model_configured": True,
            "workspaces": [
                {"workspace_id": workspace_id, "name": "Local repo", "is_git": True}
            ],
        }
    )
    assert socket.receive_json()["type"] == "hello.ack"
    return workspace_id


def _wait_status(client, task_id, status, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/v1/tasks/{task_id}").json()
        if snapshot["status"] == status:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"task did not reach {status}")


def test_pair_connect_task_approval_and_terminal_flow(client, app):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(socket)
        devices = client.get("/api/v1/devices").json()["items"]
        assert devices[0]["online"] is True
        assert devices[0]["workspaces"][0]["workspace_id"] == workspace_id

        workspaces = client.get("/api/v1/workspaces").json()["items"]
        local = next(item for item in workspaces if item["workspace_id"] == workspace_id)
        assert local["execution_environment"] == "local_worker"
        assert "Test laptop" in local["display_path"]

        created_session = client.post(
            "/api/v1/sessions", json={"workspace_id": workspace_id, "title": "Local"}
        ).json()
        assert created_session["execution_environment"] == "local_worker"
        task = client.post(
            "/api/v1/tasks",
            json={"session_id": created_session["session_id"], "input": "update README"},
        ).json()
        start = socket.receive_json()
        assert start["type"] == "task.start"
        assert start["task"]["workspace_id"] == workspace_id
        assert start["task"]["session"]["workspace_root"].startswith("worker://")

        approval_args = {"command": "echo ok", "api_key": "secret-value"}
        approval_digest = hashlib.sha256(
            json.dumps(
                approval_args,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        socket.send_json(
            {
                "type": "event",
                "task_id": task["task_id"],
                "event_type": "tool.completed",
                "data": {
                    "tool_call_id": "call_1",
                    "tool_name": "write_file",
                    "tool_status": "ok",
                    "affected_paths": ["README.md", "C:/Users/private/secret.txt", "../escape"],
                },
            }
        )
        socket.send_json(
            {
                "type": "approval.requested",
                "task_id": task["task_id"],
                "tool_call_id": "call_2",
                "tool_name": "run_shell",
                "args": approval_args,
                "args_digest": approval_digest,
            }
        )
        registered = socket.receive_json()
        assert registered["type"] == "approval.registered"
        waiting = _wait_status(client, task["task_id"], "waiting_for_approval")
        assert waiting["pending_approval"]["args_preview"]["api_key"] != "secret-value"

        decision = client.post(
            f"/api/v1/tasks/{task['task_id']}/approvals/{registered['approval_id']}",
            json={"decision": "approved"},
        )
        assert decision.status_code == 200
        forwarded = socket.receive_json()
        assert forwarded["type"] == "approval.decision"
        assert forwarded["tool_call_id"] == "call_2"
        assert forwarded["args_digest"] == approval_digest

        returned_session = start["task"]["session"]
        returned_session["history"] = [
            {"role": "user", "content": "update README"},
            {"role": "assistant", "content": "done"},
        ]
        socket.send_json(
            {
                "type": "terminal",
                "task_id": task["task_id"],
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "final_answer": "done",
                "session": returned_session,
            }
        )
        terminal = _wait_status(client, task["task_id"], "completed")
        assert terminal["final_answer"] == "done"
        detail = client.get(f"/api/v1/sessions/{created_session['session_id']}").json()
        assert [message["content"] for message in detail["messages"]] == ["update README", "done"]

        second = client.post(
            "/api/v1/tasks",
            json={"session_id": created_session["session_id"], "input": "cancel me"},
        ).json()
        second_start = socket.receive_json()
        second_args = {"command": "sleep 30"}
        second_digest = hashlib.sha256(
            json.dumps(second_args, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        socket.send_json(
            {
                "type": "approval.requested",
                "task_id": second["task_id"],
                "tool_call_id": "call_cancel",
                "tool_name": "run_shell",
                "args": second_args,
                "args_digest": second_digest,
            }
        )
        second_approval = socket.receive_json()
        _wait_status(client, second["task_id"], "waiting_for_approval")
        cancelled = client.post(f"/api/v1/tasks/{second['task_id']}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["pending_approval"] is None
        cancel_message = socket.receive_json()
        assert cancel_message == {"type": "task.cancel", "task_id": second["task_id"]}
        stored_approval = app.state.container.approval_repo.get(second_approval["approval_id"])
        assert stored_approval.status.value == "cancelled"

        socket.send_json(
            {
                "type": "terminal",
                "task_id": second["task_id"],
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "final_answer": "late answer",
                "session": second_start["task"]["session"],
            }
        )
        cancelled_terminal = _wait_status(client, second["task_id"], "cancelled")
        assert cancelled_terminal["final_answer"] is None


def test_worker_auth_owner_isolation_and_revocation(client, app):
    paired = _pair(client)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/api/v1/workers/connect", headers={"Authorization": "Bearer forged"}
    ):
        pass

    foreign = Actor("22222222-2222-4222-8222-222222222222")
    app.dependency_overrides[get_actor] = lambda: foreign
    try:
        assert client.get("/api/v1/devices").json()["items"] == []
        response = client.delete(f"/api/v1/devices/{paired['device_id']}")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_actor, None)

    assert client.delete(f"/api/v1/devices/{paired['device_id']}").status_code == 200
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/api/v1/workers/connect",
        headers={"Authorization": f"Bearer {paired['device_token']}"},
    ):
        pass

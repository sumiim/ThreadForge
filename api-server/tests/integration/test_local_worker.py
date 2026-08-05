"""End-to-end local Worker control-plane flow over its public APIs."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import WebSocketDisconnect

from threadforge_api.api.dependencies import get_actor
from threadforge_api.domain.entities import Task
from threadforge_api.domain.identity import Actor


def _pair(client, name="Test laptop"):
    code = client.post("/api/v1/devices/pairing-codes", json={}).json()["code"]
    response = client.post("/api/v1/workers/pair", json={"code": code, "name": name})
    assert response.status_code == 200
    return response.json()


def _hello(socket, workspace_id="ws_" + "a" * 32, capabilities=None):
    socket.send_json(
        {
            "type": "hello",
            "version": "0.1.0",
            "protocol_version": 1,
            "model": "fake-local-model",
            "model_configured": True,
            "capabilities": capabilities or [],
            "workspaces": [
                {"workspace_id": workspace_id, "name": "Local repo", "is_git": True}
            ],
        }
    )
    assert socket.receive_json()["type"] == "hello.ack"
    return workspace_id


def test_companion_workspace_selection_roundtrip(client):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        existing_workspace_id = _hello(socket, capabilities=["workspace_selection"])
        device = client.get("/api/v1/devices").json()["items"][0]
        assert device["capabilities"] == ["workspace_selection"]

        requested = client.post(
            f"/api/v1/devices/{paired['device_id']}/workspace-selection-requests"
        )
        assert requested.status_code == 202
        selection = requested.json()
        command = socket.receive_json()
        assert command == {
            "type": "workspace.select",
            "request_id": selection["request_id"],
            "expires_at": selection["expires_at"],
        }

        duplicate = client.post(
            f"/api/v1/devices/{paired['device_id']}/workspace-selection-requests"
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "worker_command_pending"

        selected_workspace_id = "ws_" + "b" * 32
        socket.send_json(
            {
                "type": "workspace.selection.completed",
                "request_id": selection["request_id"],
                "status": "selected",
                "workspace_id": selected_workspace_id,
                "workspaces": [
                    {
                        "workspace_id": existing_workspace_id,
                        "name": "Local repo",
                        "is_git": True,
                    },
                    {
                        "workspace_id": selected_workspace_id,
                        "name": "Second repo",
                        "is_git": False,
                    },
                ],
            }
        )
        assert socket.receive_json() == {
            "type": "workspace.selection.ack",
            "request_id": selection["request_id"],
        }
        completed = client.get(
            f"/api/v1/devices/{paired['device_id']}/workspace-selection-requests/{selection['request_id']}"
        ).json()
        assert completed["status"] == "completed"
        assert completed["workspace_id"] == selected_workspace_id
        assert "path" not in json.dumps(completed)
        device = client.get("/api/v1/devices").json()["items"][0]
        assert [item["name"] for item in device["workspaces"]] == [
            "Local repo",
            "Second repo",
        ]


def test_workspace_selection_requires_online_companion(client):
    paired = _pair(client)
    endpoint = f"/api/v1/devices/{paired['device_id']}/workspace-selection-requests"
    offline = client.post(endpoint)
    assert offline.status_code == 409
    assert offline.json()["error"]["code"] == "worker_offline"

    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        _hello(socket)
        unsupported = client.post(endpoint)
        assert unsupported.status_code == 409
        assert unsupported.json()["error"]["code"] == "worker_capability_unavailable"


def test_local_session_index_can_be_recovered_without_uploading_history(client, app):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    session_id = "ses_" + "c" * 32
    legacy_session_id = "ses_" + "d" * 32
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(socket, capabilities=["local_history"])
        owner_id = app.state.container.device_store.get(paired["device_id"]).owner_id
        app.state.container.session_store.save(
            {
                "id": legacy_session_id,
                "workspace_id": workspace_id,
                "execution_environment": "local_worker",
                "device_id": paired["device_id"],
                "owner_id": owner_id,
                "title": "Legacy duplicate",
                "created_at": "2026-08-04T00:00:00Z",
                "history": [{"role": "user", "content": "legacy central prompt"}],
                "memory": {"summary": "legacy central answer"},
                "checkpoints": {"secret": "legacy checkpoint"},
            }
        )
        legacy_task = Task(
            task_id="task_" + "e" * 32,
            session_id=legacy_session_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id="run_" + "f" * 32,
            input="legacy central prompt",
            final_answer="legacy central answer",
            execution_environment="local_worker",
            device_id=paired["device_id"],
        )
        app.state.container.task_repo.create(legacy_task)
        socket.send_json(
            {
                "type": "sessions.updated",
                "complete": True,
                "sessions": [
                    {
                        "session_id": session_id,
                        "workspace_id": workspace_id,
                        "title": "Recovered locally",
                        "created_at": "2026-08-05T00:00:00Z",
                        "updated_at": "2026-08-05T00:01:00Z",
                        "message_total": 2,
                    },
                    {
                        "session_id": legacy_session_id,
                        "workspace_id": workspace_id,
                        "title": "Legacy duplicate",
                        "created_at": "2026-08-04T00:00:00Z",
                        "updated_at": "2026-08-05T00:02:00Z",
                        "message_total": 2,
                    },
                ],
            }
        )
        assert socket.receive_json() == {"type": "sessions.updated.ack", "complete": True}
        summary = next(
            item
            for item in client.get("/api/v1/sessions").json()["items"]
            if item["session_id"] == session_id
        )
        assert summary["message_total"] == 2
        assert app.state.container.session_store.load(session_id)["history"] == []
        scrubbed_session = app.state.container.session_store.load(legacy_session_id)
        assert scrubbed_session["history"] == []
        assert "checkpoints" not in scrubbed_session
        scrubbed_task = app.state.container.task_repo.get(legacy_task.task_id)
        assert scrubbed_task.input == ""
        assert scrubbed_task.final_answer is None

        with ThreadPoolExecutor(max_workers=1) as executor:
            detail_future = executor.submit(client.get, f"/api/v1/sessions/{session_id}")
            command = socket.receive_json()
            assert command["type"] == "session.history.get"
            socket.send_json(
                {
                    "type": "session.history.result",
                    "request_id": command["request_id"],
                    "session_id": session_id,
                    "status": "completed",
                    "message_total": 2,
                    "messages": [
                        {"role": "user", "content": "local question"},
                        {"role": "assistant", "content": "local answer"},
                    ],
                }
            )
            detail = detail_future.result(timeout=2).json()
        assert [item["content"] for item in detail["messages"]] == [
            "local question",
            "local answer",
        ]


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
        workspace_id = _hello(
            socket, capabilities=["local_history", "model_configuration"]
        )
        devices = client.get("/api/v1/devices").json()["items"]
        assert devices[0]["online"] is True
        assert devices[0]["version"] == "0.1.0"
        assert devices[0]["protocol_version"] == 1
        assert devices[0]["compatible"] is True
        assert devices[0]["workspaces"][0]["workspace_id"] == workspace_id

        workspaces = client.get("/api/v1/workspaces").json()["items"]
        local = next(item for item in workspaces if item["workspace_id"] == workspace_id)
        assert local["execution_environment"] == "local_worker"
        assert "Test laptop" in local["display_path"]

        insecure = client.put(
            f"/api/v1/devices/{paired['device_id']}/model-config",
            json={
                "base_url": "http://provider.example/v1",
                "api_key": "must-not-leak",
                "model": "model-b",
            },
        )
        assert insecure.status_code == 422
        assert "must-not-leak" not in insecure.text

        with ThreadPoolExecutor(max_workers=1) as executor:
            configured_future = executor.submit(
                client.put,
                f"/api/v1/devices/{paired['device_id']}/model-config",
                json={
                    "base_url": "https://provider.example/v1",
                    "api_key": "local-only-secret",
                    "model": "model-b",
                },
            )
            configure_command = socket.receive_json()
            assert configure_command["type"] == "model.configure"
            assert configure_command["api_key"] == "local-only-secret"
            socket.send_json(
                {
                    "type": "model.configuration.completed",
                    "request_id": configure_command["request_id"],
                    "status": "completed",
                    "model": "model-b",
                }
            )
            assert socket.receive_json()["type"] == "model.configuration.ack"
            assert configured_future.result(timeout=2).status_code == 200
        assert "local-only-secret" not in json.dumps(
            app.state.container.device_store.list_for_owner(
                app.state.container.owner_id
            )[0].to_dict()
        )

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
                "message_total": 2,
            }
        )
        terminal = _wait_status(client, task["task_id"], "completed")
        assert terminal["final_answer"] == "done"
        assert app.state.container.task_repo.get(task["task_id"]).final_answer is None
        assert app.state.container.task_repo.get(task["task_id"]).input == ""
        central_session = app.state.container.session_store.load(created_session["session_id"])
        assert central_session["history"] == []
        assert central_session["local_message_total"] == 2
        persisted = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in app.state.container.settings.data_dir.rglob("*")
            if path.is_file()
        )
        assert "update README" not in persisted
        assert '"final_answer": "done"' not in persisted
        assert not (
            app.state.container.settings.data_dir / "runs" / task["run_id"]
        ).exists()
        with ThreadPoolExecutor(max_workers=1) as executor:
            detail_future = executor.submit(
                client.get, f"/api/v1/sessions/{created_session['session_id']}"
            )
            history_command = socket.receive_json()
            assert history_command["type"] == "session.history.get"
            socket.send_json(
                {
                    "type": "session.history.result",
                    "request_id": history_command["request_id"],
                    "session_id": created_session["session_id"],
                    "status": "completed",
                    "message_total": 2,
                    "messages": returned_session["history"],
                }
            )
            detail = detail_future.result(timeout=2).json()
        assert [message["content"] for message in detail["messages"]] == ["update README", "done"]

        second = client.post(
            "/api/v1/tasks",
            json={"session_id": created_session["session_id"], "input": "cancel me"},
        ).json()
        assert socket.receive_json()["type"] == "task.start"
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
                "message_total": 2,
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

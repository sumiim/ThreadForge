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
            "platform": "linux",
            "architecture": "x86_64",
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
        assert device["platform"] == "linux"
        assert device["architecture"] == "x86_64"

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
        assert duplicate.status_code == 202
        assert duplicate.json()["request_id"] == selection["request_id"]
        assert duplicate.json()["status"] == "pending"

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


def test_companion_reports_bounded_update_progress(client):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        _hello(socket, capabilities=["auto_update", "resumable_auto_update"])
        socket.send_json(
            {
                "type": "update.status",
                "status": "downloading",
                "current_version": "0.3.0",
                "target_version": "0.3.1",
                "downloaded_bytes": 25,
                "total_bytes": 100,
                "error": "",
                "updated_at": "2026-08-07T12:00:00+00:00",
            }
        )
        assert socket.receive_json() == {"type": "update.status.ack"}
        device = client.get("/api/v1/devices").json()["items"][0]
        assert device["update_status"] == {
            "status": "downloading",
            "current_version": "0.3.0",
            "target_version": "0.3.1",
            "downloaded_bytes": 25,
            "total_bytes": 100,
            "bytes_per_second": 0,
            "retry_count": 0,
            "error": "",
            "updated_at": "2026-08-07T12:00:00+00:00",
        }


def test_local_session_workspace_delete_and_remote_uninstall(client):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(
            socket,
            capabilities=["delete_entities", "worker_uninstall"],
        )
        first_session = client.post(
            "/api/v1/sessions", json={"workspace_id": workspace_id, "title": "First"}
        ).json()["session_id"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            deleted_future = executor.submit(
                client.delete, f"/api/v1/sessions/{first_session}"
            )
            command = socket.receive_json()
            assert command["type"] == "entity.delete"
            assert command["entity_type"] == "session"
            assert command["session_ids"] == [first_session]
            socket.send_json(
                {
                    "type": "entity.delete.completed",
                    "request_id": command["request_id"],
                    "entity_type": "session",
                    "entity_id": first_session,
                    "status": "completed",
                    "deleted_session_ids": [first_session],
                    "workspaces": [
                        {"workspace_id": workspace_id, "name": "Local repo", "is_git": True}
                    ],
                }
            )
            assert socket.receive_json()["type"] == "entity.delete.ack"
            assert deleted_future.result(timeout=2).status_code == 200
        assert client.get(f"/api/v1/sessions/{first_session}").status_code == 404

        second_session = client.post(
            "/api/v1/sessions", json={"workspace_id": workspace_id, "title": "Second"}
        ).json()["session_id"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            workspace_future = executor.submit(
                client.delete,
                f"/api/v1/devices/{paired['device_id']}/workspaces/{workspace_id}",
            )
            command = socket.receive_json()
            assert command["type"] == "entity.delete"
            assert command["entity_type"] == "workspace"
            assert command["session_ids"] == [second_session]
            socket.send_json(
                {
                    "type": "entity.delete.completed",
                    "request_id": command["request_id"],
                    "entity_type": "workspace",
                    "entity_id": workspace_id,
                    "status": "completed",
                    "deleted_session_ids": [second_session],
                    "workspaces": [],
                }
            )
            assert socket.receive_json()["type"] == "entity.delete.ack"
            response = workspace_future.result(timeout=2)
            assert response.status_code == 200
            assert response.json()["deleted_session_ids"] == [second_session]
        assert client.get(f"/api/v1/sessions/{second_session}").status_code == 404
        assert not any(
            item["workspace_id"] == workspace_id
            for item in client.get("/api/v1/workspaces").json()["items"]
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            uninstall_future = executor.submit(
                client.post, f"/api/v1/devices/{paired['device_id']}/uninstall"
            )
            command = socket.receive_json()
            assert command["type"] == "worker.uninstall"
            socket.send_json(
                {
                    "type": "worker.uninstall.completed",
                    "request_id": command["request_id"],
                    "status": "completed",
                }
            )
            assert socket.receive_json()["type"] == "worker.uninstall.ack"
            assert uninstall_future.result(timeout=2).json()["status"] == "uninstalling"


def test_online_worker_pool_supports_multiple_connections_and_capability_filter(client):
    first = _pair(client, "First laptop")
    second = _pair(client, "Second server")
    first_headers = {"Authorization": f"Bearer {first['device_token']}"}
    second_headers = {"Authorization": f"Bearer {second['device_token']}"}

    with client.websocket_connect("/api/v1/workers/connect", headers=first_headers) as first_socket:
        _hello(first_socket, capabilities=["workspace_selection"])
        with client.websocket_connect(
            "/api/v1/workers/connect", headers=second_headers
        ) as second_socket:
            _hello(second_socket, workspace_id="ws_" + "b" * 32, capabilities=["local_history"])

            online = client.get("/api/v1/workers/online")
            assert online.status_code == 200
            payload = online.json()
            assert payload["routing"] == {"mode": "single", "multi_worker": "reserved"}
            assert {item["worker_id"] for item in payload["items"]} == {
                first["device_id"],
                second["device_id"],
            }

            filtered = client.get("/api/v1/workers/online?capability=workspace_selection")
            assert filtered.status_code == 200
            assert [item["worker_id"] for item in filtered.json()["items"]] == [
                first["device_id"]
            ]


def test_session_creation_uses_selected_worker_for_duplicate_workspace_id(client):
    first = _pair(client, "First laptop")
    second = _pair(client, "Second laptop")
    first_headers = {"Authorization": f"Bearer {first['device_token']}"}
    second_headers = {"Authorization": f"Bearer {second['device_token']}"}
    shared_workspace_id = "ws_" + "c" * 32

    with client.websocket_connect(
        "/api/v1/workers/connect", headers=first_headers
    ) as first_socket:
        _hello(first_socket, workspace_id=shared_workspace_id)
        with client.websocket_connect(
            "/api/v1/workers/connect", headers=second_headers
        ) as second_socket:
            _hello(second_socket, workspace_id=shared_workspace_id)

            workspaces = client.get("/api/v1/workspaces").json()["items"]
            matching = [
                item
                for item in workspaces
                if item["workspace_id"] == shared_workspace_id
            ]
            assert {item["device_id"] for item in matching} == {
                first["device_id"],
                second["device_id"],
            }

            created = client.post(
                "/api/v1/sessions",
                json={
                    "workspace_id": shared_workspace_id,
                    "device_id": second["device_id"],
                    "title": "Second worker session",
                },
            )
            assert created.status_code == 201
            assert created.json()["device_id"] == second["device_id"]


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


def test_local_session_rebind_requires_the_previous_device_to_be_removed(client, app):
    previous = _pair(client, "Previous install")
    replacement = _pair(client, "Replacement install")
    owner_id = app.state.container.device_store.get(previous["device_id"]).owner_id
    workspace_id = "ws_" + "b" * 32
    session_id = "ses_" + "e" * 32
    app.state.container.session_store.save(
        {
            "id": session_id,
            "workspace_root": f"worker://{previous['device_id']}/{workspace_id}",
            "workspace_id": workspace_id,
            "execution_environment": "local_worker",
            "device_id": previous["device_id"],
            "owner_id": owner_id,
            "title": "Local history survives reinstall",
            "created_at": "2026-08-08T00:00:00Z",
            "history": [],
            "memory": {},
        }
    )
    summary = {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "title": "Local history survives reinstall",
        "created_at": "2026-08-08T00:00:00Z",
        "updated_at": "2026-08-08T00:01:00Z",
        "message_total": 2,
    }
    headers = {"Authorization": f"Bearer {replacement['device_token']}"}

    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        _hello(socket, workspace_id, capabilities=["local_history"])
        socket.send_json(
            {"type": "sessions.updated", "complete": True, "sessions": [summary]}
        )
        assert socket.receive_json() == {
            "type": "sessions.updated.ack",
            "complete": True,
            "rejected": [
                {
                    "session_id": session_id,
                    "code": "worker_protocol_error",
                    "message": "local session is still bound to another registered device",
                }
            ],
        }
        socket.send_json({"type": "heartbeat"})
        assert socket.receive_json() == {"type": "heartbeat.ack"}

    app.state.container.device_store.revoke(previous["device_id"], owner_id)
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        _hello(socket, workspace_id, capabilities=["local_history"])
        socket.send_json(
            {"type": "sessions.updated", "complete": True, "sessions": [summary]}
        )
        assert socket.receive_json() == {
            "type": "sessions.updated.ack",
            "complete": True,
        }

    migrated = app.state.container.session_store.load(session_id)
    assert migrated["device_id"] == replacement["device_id"]
    assert migrated["workspace_root"] == (
        f"worker://{replacement['device_id']}/{workspace_id}"
    )


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


def test_worker_progress_events_are_forward_compatible(client, app, monkeypatch, caplog):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    published = []
    publisher = app.state.container.publisher
    original_publish = publisher.publish

    def record_publish(task_id, run_id, event_type, data, **metadata):
        published.append({"type": event_type, "data": data})
        return original_publish(task_id, run_id, event_type, data, **metadata)

    monkeypatch.setattr(publisher, "publish", record_publish)
    caplog.set_level("WARNING", logger="threadforge_api.infrastructure.worker_hub")

    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(socket)
        session = client.post(
            "/api/v1/sessions",
            json={"workspace_id": workspace_id, "title": "Compatibility"},
        ).json()
        task = client.post(
            "/api/v1/tasks",
            json={"session_id": session["session_id"], "input": "hello"},
        ).json()
        assert socket.receive_json()["type"] == "task.start"

        socket.send_json(
            {
                "type": "event",
                "task_id": task["task_id"],
                "event_type": "plan.skipped",
                "data": {
                    "reason": "plain_conversation",
                    "intent": "conversation",
                    "summary": "Answer directly",
                    "private": "must not be forwarded",
                },
            }
        )
        for text in ("deep ", "reasoning ", "continues"):
            socket.send_json(
                {
                    "type": "event",
                    "task_id": task["task_id"],
                    "event_type": "assistant.thinking",
                    "data": {"stage": "execute", "text": text},
                }
            )
        socket.send_json(
            {
                "type": "event",
                "task_id": task["task_id"],
                "event_type": "future.progress",
                "data": {"private": "must not be forwarded"},
            }
        )
        socket.send_json(
            {
                "type": "event",
                "task_id": task["task_id"],
                "event_type": "agent.state",
                "data": {"phase": "answering", "next_step": "reply"},
            }
        )
        socket.send_json(
            {
                "type": "terminal",
                "task_id": task["task_id"],
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "final_answer": "Hello",
                "message_total": 2,
            }
        )

        terminal = _wait_status(client, task["task_id"], "completed")
        assert terminal["final_answer"] == "Hello"
        thinking = [
            item for item in terminal["run_index"] if item["type"] == "assistant.thinking"
        ]
        assert len(thinking) == 1
        assert thinking[0]["text"] == "deep reasoning continues"

    skipped = next(item for item in published if item["type"] == "plan.skipped")
    assert skipped["data"] == {
        "reason": "plain_conversation",
        "intent": "conversation",
        "summary": "Answer directly",
    }
    assert "future.progress" not in {item["type"] for item in published}
    assert "agent.state" in {item["type"] for item in published}
    assert "task.completed" in {item["type"] for item in published}
    assert "Ignoring unsupported Worker progress event" in caplog.text


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


def test_single_worker_runs_concurrent_tasks_up_to_quota(client):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(socket)

        def create_session_and_task(label):
            session = client.post(
                "/api/v1/sessions", json={"workspace_id": workspace_id, "title": label}
            ).json()
            return client.post(
                "/api/v1/tasks",
                json={"session_id": session["session_id"], "input": label},
            )

        first = create_session_and_task("first")
        assert first.status_code in {200, 202}
        assert socket.receive_json()["type"] == "task.start"

        second = create_session_and_task("second")
        assert second.status_code in {200, 202}
        assert socket.receive_json()["type"] == "task.start"

        third_session = client.post(
            "/api/v1/sessions", json={"workspace_id": workspace_id, "title": "third"}
        ).json()
        third = client.post(
            "/api/v1/tasks",
            json={"session_id": third_session["session_id"], "input": "third"},
        )
        assert third.status_code == 409
        assert third.json()["error"]["code"] == "worker_concurrency_limit"


def test_revoke_device_cascades_to_sessions(client):
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(socket)
        session = client.post(
            "/api/v1/sessions", json={"workspace_id": workspace_id, "title": "orphan"}
        ).json()
        assert any(
            item["session_id"] == session["session_id"]
            for item in client.get("/api/v1/sessions").json()["items"]
        )

    assert client.delete(f"/api/v1/devices/{paired['device_id']}").status_code == 200
    remaining = client.get("/api/v1/sessions").json()["items"]
    assert all(item["session_id"] != session["session_id"] for item in remaining)


def test_session_history_unavailable_degrades_to_empty_history(client):
    # 任务失败且 worker 本地从未持久化 session 时，控制面仍有 session + task 失败记录。
    # worker 返回 failed/history_unavailable 时，get_session 应降级为 200 + 空历史，
    # 而不是 422 导致前端「历史加载失败」。
    paired = _pair(client)
    headers = {"Authorization": f"Bearer {paired['device_token']}"}
    with client.websocket_connect("/api/v1/workers/connect", headers=headers) as socket:
        workspace_id = _hello(socket, capabilities=["local_history"])
        session = client.post(
            "/api/v1/sessions", json={"workspace_id": workspace_id, "title": "lost history"}
        ).json()
        # 模拟：控制面记录了任务（task_total > 0），但 worker 本地无该 session。
        session_id = session["session_id"]
        task = client.post(
            "/api/v1/tasks", json={"session_id": session_id, "input": "hello"}
        ).json()
        assert socket.receive_json()["type"] == "task.start"
        socket.send_json(
            {
                "type": "terminal",
                "task_id": task["task_id"],
                "status": "failed",
                "stop_reason": "worker_runtime_error",
                "final_answer": "",
                "message_total": 0,
                "session_persisted": False,
            }
        )
        _wait_status(client, task["task_id"], "failed")

        with ThreadPoolExecutor(max_workers=1) as executor:
            detail_future = executor.submit(client.get, f"/api/v1/sessions/{session_id}")
            command = socket.receive_json()
            assert command["type"] == "session.history.get"
            # 旧版 Worker 语义：本地无 session → failed/history_unavailable
            socket.send_json(
                {
                    "type": "session.history.result",
                    "request_id": command["request_id"],
                    "session_id": session_id,
                    "status": "failed",
                    "error": "history_unavailable",
                }
            )
            detail = detail_future.result(timeout=2)
        assert detail.status_code == 200
        assert detail.json()["messages"] == []
        assert detail.json()["task_total"] == 1

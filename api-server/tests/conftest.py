"""Shared fixtures: isolated data dir, workspace allowlist, app + TestClient."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pico.providers.clients import FakeModelClient

from threadforge_api.config import Settings
from threadforge_api.main import create_app


@pytest.fixture
def workspace_env(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    data_dir = tmp_path / "data"
    ws_file = tmp_path / "workspaces.json"
    ws_file.write_text(
        json.dumps({"workspaces": [{"id": "w1", "name": "W1", "path": str(wsdir)}]})
    )
    return {"data_dir": data_dir, "ws_file": ws_file, "wsdir": wsdir, "root": tmp_path}


@pytest.fixture
def settings(workspace_env):
    return Settings(
        data_dir=str(workspace_env["data_dir"]),
        workspaces_file=str(workspace_env["ws_file"]),
        pico_openai_api_key="test-key",
        trusted_hosts=["127.0.0.1", "::1", "localhost", "testserver"],
        model_timeout_seconds=30,
        instance_owner_id="11111111-1111-4111-8111-111111111111",
    )


@pytest.fixture
def model_outputs():
    return ["<final>ok</final>"]


@pytest.fixture
def model_factory(model_outputs):
    def factory():
        return FakeModelClient(outputs=list(model_outputs))

    return factory


@pytest.fixture
def app(settings, model_factory):
    return create_app(settings, model_client_factory=model_factory)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session_id(client):
    return client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()["session_id"]


def wait_for_terminal(client, task_id, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/tasks/{task_id}").json()
        if last["status"] in {"completed", "cancelled", "failed", "interrupted", "blocked"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task did not reach terminal: {last}")


def wait_for_status(client, task_id, status, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/tasks/{task_id}").json()
        if last["status"] == status:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task never reached {status}: {last}")

"""REST contract tests: health / workspaces / sessions / task errors / artifacts."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from threadforge_api.api.dependencies import get_actor
from threadforge_api.config import Settings
from threadforge_api.domain.identity import Actor
from threadforge_api.main import create_app

from ..conftest import wait_for_terminal


def test_live_and_ready(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_workspaces_list(client):
    resp = client.get("/api/v1/workspaces")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    entry = items[0]
    assert entry["workspace_id"] == "w1"
    assert entry["execution_environment"] == "backend_process"
    assert entry["container_sandbox_enabled"] is False


def test_client_metadata_is_truthful(client):
    config = client.get("/api/v1/config")
    assert config.status_code == 200
    assert config.json() == {
        "model": "gpt-5.4",
        "model_configured": True,
        "execution_environment": "backend_process",
        "container_sandbox_enabled": False,
        "identity_mode": "single_owner_instance",
        "multi_user_enabled": False,
    }

    skills = client.get("/api/v1/skills").json()["items"]
    assert skills
    assert all(item["status"] == "planned" and item["available"] is False for item in skills)

    servers = client.get("/api/v1/mcp/servers").json()["items"]
    assert servers
    assert all(
        item["status"] == "not_configured" and item["connected"] is False
        for item in servers
    )


def test_desktop_null_origin_requires_explicit_opt_in(settings, model_factory):
    app = create_app(settings, model_client_factory=model_factory)
    with TestClient(app) as default_client:
        response = default_client.options(
            "/api/v1/config",
            headers={"Origin": "null", "Access-Control-Request-Method": "GET"},
        )
        assert response.headers.get("access-control-allow-origin") is None

    desktop_settings = settings.model_copy(update={"desktop_origin_enabled": True})
    app = create_app(desktop_settings, model_client_factory=model_factory)
    with TestClient(app) as desktop_client:
        response = desktop_client.options(
            "/api/v1/config",
            headers={"Origin": "null", "Access-Control-Request-Method": "GET"},
        )
        assert response.headers["access-control-allow-origin"] == "null"


def test_zero_arg_factory_applies_initialization_settings_from_env(
    monkeypatch, workspace_env
):
    monkeypatch.setenv("THREADFORGE_DATA_DIR", str(workspace_env["data_dir"]))
    monkeypatch.setenv("THREADFORGE_WORKSPACES_FILE", str(workspace_env["ws_file"]))
    monkeypatch.setenv("THREADFORGE_TRUSTED_HOSTS", '["testserver"]')
    monkeypatch.setenv("THREADFORGE_WEB_ORIGIN", "http://localhost:4173")
    monkeypatch.setenv("THREADFORGE_DESKTOP_ORIGIN_ENABLED", "true")
    monkeypatch.setenv("THREADFORGE_OPENAPI_ENABLED", "false")

    app = create_app()
    assert app.openapi_url is None
    assert app.docs_url is None
    with TestClient(app) as env_client:
        response = env_client.options(
            "/api/v1/config",
            headers={"Origin": "null", "Access-Control-Request-Method": "GET"},
        )
        assert response.headers["access-control-allow-origin"] == "null"


def test_session_create_list_get(client):
    created = client.post("/api/v1/sessions", json={"workspace_id": "w1", "title": "My work"})
    assert created.status_code == 201
    body = created.json()
    assert body["session_id"].startswith("ses_")
    assert body["title"] == "My work"

    listing = client.get("/api/v1/sessions").json()
    assert listing["total"] == 1
    assert listing["items"][0]["session_id"] == body["session_id"]

    detail = client.get(f"/api/v1/sessions/{body['session_id']}?message_limit=50")
    assert detail.status_code == 200
    assert detail.json()["session_id"] == body["session_id"]
    assert detail.json()["task_total"] == 0
    assert detail.json()["tasks"] == []


def test_first_request_becomes_stable_automatic_session_title(client, model_outputs):
    created = client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()
    assert created["has_started"] is False
    assert created["display_name_source"] == "auto"

    model_outputs[:] = ["<final>first</final>", "<final>second</final>"]
    first = client.post(
        "/api/v1/tasks",
        json={"session_id": created["session_id"], "input": "  修复\n Worker 自动更新  "},
    ).json()
    wait_for_terminal(client, first["task_id"])

    after_first = client.get(f"/api/v1/sessions/{created['session_id']}").json()
    assert after_first["title"] == "修复 Worker 自动更新"
    assert after_first["display_name_source"] == "auto"
    assert after_first["has_started"] is True

    second = client.post(
        "/api/v1/tasks",
        json={"session_id": created["session_id"], "input": "不要覆盖现有标题"},
    ).json()
    wait_for_terminal(client, second["task_id"])
    after_second = client.get(f"/api/v1/sessions/{created['session_id']}").json()
    assert after_second["title"] == "修复 Worker 自动更新"


def test_first_request_preserves_user_session_title(client, model_outputs):
    created = client.post(
        "/api/v1/sessions",
        json={"workspace_id": "w1", "title": "手动标题"},
    ).json()
    model_outputs[:] = ["<final>done</final>"]
    task = client.post(
        "/api/v1/tasks",
        json={"session_id": created["session_id"], "input": "首条请求"},
    ).json()
    wait_for_terminal(client, task["task_id"])

    detail = client.get(f"/api/v1/sessions/{created['session_id']}").json()
    assert detail["title"] == "手动标题"
    assert detail["display_name_source"] == "user"
    assert detail["has_started"] is True


def test_identity_header_cannot_override_instance_owner(client, settings):
    foreign_owner = "22222222-2222-4222-8222-222222222222"
    response = client.post(
        "/api/v1/sessions",
        headers={"X-User-Id": foreign_owner},
        json={"workspace_id": "w1"},
    )
    assert response.status_code == 201
    session_id = response.json()["session_id"]
    path = settings.data_dir / "sessions" / f"{session_id}.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["owner_id"] == str(settings.instance_owner_id)
    assert stored["owner_id"] != foreign_owner


def test_startup_claims_legacy_session(settings, model_factory):
    from pico.features.memory import default_memory_state
    from pico.session_store import SessionStore

    session_id = "ses_" + "a" * 32
    store = SessionStore(settings.data_dir / "sessions")
    store.save(
        {
            "id": session_id,
            "workspace_id": "w1",
            "workspace_root": "legacy",
            "title": "Legacy",
            "created_at": "2026-08-04T00:00:00Z",
            "history": [],
            "memory": default_memory_state(),
        }
    )

    app = create_app(settings, model_client_factory=model_factory)
    with TestClient(app) as test_client:
        assert test_client.get(f"/api/v1/sessions/{session_id}").status_code == 200
    assert store.load(session_id)["owner_id"] == str(settings.instance_owner_id)


def test_foreign_actor_cannot_access_owned_objects(client, app, session_id, model_outputs):
    model_outputs[:] = ["<final>private answer</final>"]
    created = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "private"}).json()
    terminal = wait_for_terminal(client, created["task_id"])
    foreign = Actor("22222222-2222-4222-8222-222222222222")
    app.dependency_overrides[get_actor] = lambda: foreign
    try:
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404
        assert client.get(f"/api/v1/tasks/{created['task_id']}").status_code == 404
        assert client.post(f"/api/v1/tasks/{created['task_id']}/cancel").status_code == 404
        assert client.get(f"/api/v1/tasks/{created['task_id']}/events").status_code == 404
        assert client.get(f"/api/v1/runs/{terminal['run_id']}/artifacts").status_code == 404
    finally:
        app.dependency_overrides.pop(get_actor, None)


def test_session_detail_contains_task_summaries(client, session_id, model_outputs):
    model_outputs[:] = ["<final>summary answer</final>"]
    created = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "summarize me"}).json()
    wait_for_terminal(client, created["task_id"])
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["task_total"] == 1
    assert detail["tasks"][0]["task_id"] == created["task_id"]
    assert detail["tasks"][0]["status"] == "completed"


def test_session_404(client):
    resp = client.get("/api/v1/sessions/ses_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"


def test_task_404(client):
    resp = client.get("/api/v1/tasks/task_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


def test_task_404_on_events(client):
    resp = client.get("/api/v1/tasks/task_missing/events")
    assert resp.status_code == 404


def test_sse_openapi_declares_event_stream(client):
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/tasks/{task_id}/events"]["get"]
    content = operation["responses"]["200"]["content"]
    assert "text/event-stream" in content
    assert "application/json" not in content


def test_ready_rechecks_workspace_and_sessions(client, session_id, workspace_env):
    workspace_env["wsdir"].rmdir()
    assert client.get("/health/ready").status_code == 503

    workspace_env["wsdir"].mkdir()
    session_path = workspace_env["data_dir"] / "sessions" / f"{session_id}.json"
    session_path.write_text("{broken", encoding="utf-8")
    assert client.get("/health/ready").status_code == 503
    response = client.get("/api/v1/sessions")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "session_corrupted"


def test_request_id_is_present(client):
    resp = client.get("/health/live")
    assert "X-Request-ID" in resp.headers


def test_model_not_configured_returns_503(tmp_path):
    wsdir = tmp_path / "ws"
    wsdir.mkdir()
    ws_file = tmp_path / "workspaces.json"
    ws_file.write_text(json.dumps({"workspaces": [{"id": "w1", "name": "W1", "path": str(wsdir)}]}))
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        workspaces_file=str(ws_file),
        pico_openai_api_key="",
        trusted_hosts=["testserver"],
    )

    def factory():
        raise AssertionError("model factory must not be called")

    app = create_app(settings, model_client_factory=factory)
    with TestClient(app) as client:
        sid = client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()["session_id"]
        resp = client.post("/api/v1/tasks", json={"session_id": sid, "input": "x"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "model_not_configured"


def test_artifacts_404_for_unknown_run(client):
    resp = client.get("/api/v1/runs/run_missing/artifacts")
    assert resp.status_code == 404


def test_artifact_content_endpoints(client, session_id, model_outputs):
    model_outputs[:] = ["<final>artifact answer</final>"]
    task = client.post("/api/v1/tasks", json={"session_id": session_id, "input": "x"}).json()
    wait_for_terminal(client, task["task_id"])
    run_id = client.get(f"/api/v1/tasks/{task['task_id']}").json()["run_id"]

    listing = client.get(f"/api/v1/runs/{run_id}/artifacts").json()
    names = {item["name"] for item in listing["items"]}
    assert names == {"task_state", "trace", "report"}

    report = client.get(f"/api/v1/runs/{run_id}/artifacts/report")
    assert report.status_code == 200
    assert report.json()["final_answer"] == "artifact answer"

    trace = client.get(f"/api/v1/runs/{run_id}/artifacts/trace")
    assert trace.headers["content-type"].startswith("application/x-ndjson")
    assert "run_started" in trace.text


def test_trace_artifact_skips_incomplete_tail(client, session_id, model_outputs, workspace_env):
    model_outputs[:] = ["<final>done</final>"]
    task = client.post(
        "/api/v1/tasks",
        json={"session_id": session_id, "input": "x"},
    ).json()
    terminal = wait_for_terminal(client, task["task_id"])
    trace_path = workspace_env["data_dir"] / "runs" / terminal["run_id"] / "trace.jsonl"
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write('{"event":"interrupted')

    trace = client.get(f"/api/v1/runs/{terminal['run_id']}/artifacts/trace")
    assert trace.status_code == 200
    assert "interrupted" not in trace.text
    for line in trace.text.splitlines():
        json.loads(line)

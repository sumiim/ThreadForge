"""GitHub OAuth HTTP contract and per-user object isolation."""

from __future__ import annotations

import urllib.parse

from fastapi.testclient import TestClient

from threadforge_api.config import Settings
from threadforge_api.infrastructure.auth import GitHubIdentity
from threadforge_api.main import create_app


class FakeOAuthClient:
    def __init__(self, identity: GitHubIdentity):
        self.identity = identity
        self.exchange_calls = 0

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> str:
        assert code == "test-code"
        assert verifier
        assert redirect_uri.endswith("/api/v1/auth/github/callback")
        self.exchange_calls += 1
        return "ephemeral-access-token"

    def get_identity(self, access_token: str) -> GitHubIdentity:
        assert access_token == "ephemeral-access-token"
        return self.identity


def _oauth_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "identity_mode": "github_oauth",
            "github_oauth_client_id": "client-id",
            "github_oauth_client_secret": "client-secret",
            "github_owner_login": "sumiim",
            "github_allowed_logins": ["sumiim", "guest"],
        }
    )


def _start(client: TestClient) -> str:
    response = client.get("/api/v1/auth/github/start", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["client_id"] == ["client-id"]
    assert query["code_challenge_method"] == ["S256"]
    return query["state"][0]


def _complete(client: TestClient, state: str):
    return client.get(
        "/api/v1/auth/github/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )


def test_single_owner_mode_remains_authenticated(client):
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    assert response.json() == {
        "identity_mode": "single_owner_instance",
        "multi_user_enabled": False,
        "authentication_required": False,
        "authenticated": True,
        "user": None,
    }


def test_github_login_logout_and_owner_continuity(settings, model_factory):
    oauth = FakeOAuthClient(GitHubIdentity(123, "sumiim", "Sumiim", "https://example.test/a.png"))
    app = create_app(_oauth_settings(settings), model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/status").json()["authenticated"] is False
        assert client.get("/api/v1/sessions").status_code == 401

        callback = _complete(client, _start(client))
        assert callback.status_code == 303
        assert callback.headers["location"] == settings.github_oauth_return_url
        status = client.get("/api/v1/auth/status").json()
        assert status["authenticated"] is True
        assert status["user"]["login"] == "sumiim"
        assert status["user"]["owner_id"] == str(settings.instance_owner_id)

        assert client.post("/api/v1/sessions", json={"workspace_id": "w1"}).status_code == 403
        client.headers["X-ThreadForge-CSRF"] = "1"
        created = client.post("/api/v1/sessions", json={"workspace_id": "w1"})
        assert created.status_code == 201
        assert client.post("/api/v1/auth/logout").json() == {"status": "signed_out"}
        assert client.get("/api/v1/sessions").status_code == 401


def test_oauth_state_mismatch_and_replay_are_rejected(settings, model_factory):
    oauth = FakeOAuthClient(GitHubIdentity(123, "sumiim"))
    app = create_app(_oauth_settings(settings), model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(app) as client:
        state = _start(client)
        mismatch = client.get(
            "/api/v1/auth/github/callback",
            params={"code": "test-code", "state": "wrong-state"},
            follow_redirects=False,
        )
        assert mismatch.status_code == 303
        assert "auth_error=oauth_state_invalid" in mismatch.headers["location"]

        state = _start(client)
        completed = _complete(client, state)
        assert completed.status_code == 303
        assert completed.headers["location"] == settings.github_oauth_return_url
        client.cookies.set("threadforge_oauth_state", state, path="/api/v1/auth/github/callback")
        replay = _complete(client, state)
        assert replay.status_code == 303
        assert "auth_error=oauth_state_invalid" in replay.headers["location"]
        assert oauth.exchange_calls == 1


def test_non_allowlisted_github_user_is_rejected(settings, model_factory):
    oauth = FakeOAuthClient(GitHubIdentity(999, "intruder"))
    app = create_app(_oauth_settings(settings), model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(app) as client:
        response = _complete(client, _start(client))
        assert response.status_code == 303
        assert "auth_error=authorization_denied" in response.headers["location"]
        assert client.get("/api/v1/auth/status").json()["authenticated"] is False


def test_two_github_users_cannot_see_each_others_sessions(settings, model_factory):
    oauth = FakeOAuthClient(GitHubIdentity(123, "sumiim"))
    app = create_app(_oauth_settings(settings), model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(app) as client:
        client.headers["X-ThreadForge-CSRF"] = "1"
        assert _complete(client, _start(client)).status_code == 303
        owner_session = client.post("/api/v1/sessions", json={"workspace_id": "w1"}).json()
        pairing_code = client.post("/api/v1/devices/pairing-codes", json={}).json()["code"]
        owner_device = client.post(
            "/api/v1/workers/pair",
            json={"code": pairing_code, "name": "Owner laptop"},
        ).json()
        client.post("/api/v1/auth/logout")

        oauth.identity = GitHubIdentity(456, "guest")
        assert _complete(client, _start(client)).status_code == 303
        assert client.get("/api/v1/sessions").json()["items"] == []
        assert client.get(f"/api/v1/sessions/{owner_session['session_id']}").status_code == 404
        assert client.get("/api/v1/devices").json()["items"] == []
        assert client.delete(f"/api/v1/devices/{owner_device['device_id']}").status_code == 404


def test_login_session_survives_app_restart(settings, model_factory):
    oauth = FakeOAuthClient(GitHubIdentity(123, "sumiim"))
    oauth_settings = _oauth_settings(settings)
    first = create_app(oauth_settings, model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(first) as client:
        assert _complete(client, _start(client)).status_code == 303
        token = client.cookies.get("threadforge_session")
        assert token

    second = create_app(oauth_settings, model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(second) as client:
        client.cookies.set("threadforge_session", token)
        assert client.get("/api/v1/auth/status").json()["authenticated"] is True


def test_allowlist_removal_revokes_existing_session(settings, model_factory):
    oauth = FakeOAuthClient(GitHubIdentity(456, "guest"))
    oauth_settings = _oauth_settings(settings)
    first = create_app(oauth_settings, model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(first) as client:
        assert _complete(client, _start(client)).status_code == 303
        token = client.cookies.get("threadforge_session")
        assert token

    restricted = oauth_settings.model_copy(update={"github_allowed_logins": ["sumiim"]})
    second = create_app(restricted, model_client_factory=model_factory, oauth_client=oauth)
    with TestClient(second) as client:
        client.cookies.set("threadforge_session", token)
        status = client.get("/api/v1/auth/status").json()
        assert status["authenticated"] is False
        assert client.get("/api/v1/sessions").status_code == 401

"""Authentication storage and OAuth attempt invariants."""

from __future__ import annotations

import pytest

from threadforge_api.domain.errors import (
    AuthorizationDeniedError,
    OAuthStateInvalidError,
)
from threadforge_api.domain.identity import Actor
from threadforge_api.infrastructure.auth import (
    AuthSessionStore,
    GitHubIdentity,
    OAuthAttemptStore,
    UserStore,
)

OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_oauth_attempt_is_one_use():
    store = OAuthAttemptStore()
    state, challenge = store.create()
    assert state
    assert challenge
    assert store.consume(state)
    with pytest.raises(OAuthStateInvalidError):
        store.consume(state)


def test_auth_session_uses_hashed_filename_and_can_be_revoked(tmp_path):
    store = AuthSessionStore(tmp_path, ttl_seconds=3600)
    actor = Actor(OWNER_ID, "github:1", "sumiim", "Sumiim", "https://example.test/a.png")
    token = store.create(actor)
    paths = list(tmp_path.glob("*.json"))
    assert len(paths) == 1
    assert token not in paths[0].name
    assert store.get(token) == actor
    store.delete(token)
    assert store.get(token) is None


def test_auth_session_rejects_invalid_tokens(tmp_path):
    store = AuthSessionStore(tmp_path, ttl_seconds=3600)
    assert store.get("") is None
    assert store.get("x" * 300) is None


def test_auth_session_prunes_expired_and_limits_each_user(tmp_path):
    store = AuthSessionStore(tmp_path, ttl_seconds=3600)
    actor = Actor(OWNER_ID, "github:1", "sumiim")
    tokens = [store.create(actor) for _ in range(12)]

    assert len(list(tmp_path.glob("*.json"))) == 10
    assert store.get(tokens[0]) is None
    assert store.get(tokens[-1]) == actor


def test_instance_owner_cannot_be_rebound_to_another_github_account(tmp_path):
    first = UserStore(tmp_path, OWNER_ID, "sumiim")
    assert first.upsert(GitHubIdentity(1, "sumiim")).owner_id == OWNER_ID

    changed_config = UserStore(tmp_path, OWNER_ID, "another-owner")
    with pytest.raises(AuthorizationDeniedError, match="already bound"):
        changed_config.upsert(GitHubIdentity(2, "another-owner"))

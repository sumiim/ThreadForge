"""GitHub OAuth and opaque server-side login sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from ..config import Settings
from ..domain.errors import (
    AuthenticationRequiredError,
    AuthorizationDeniedError,
    OAuthProviderError,
    OAuthStateInvalidError,
)
from ..domain.identity import Actor, canonical_owner_id
from .jsonutil import JsonCorruptedError, read_json, secure_directory, write_json_atomic

AUTH_COOKIE_NAME = "threadforge_session"
OAUTH_STATE_COOKIE_NAME = "threadforge_oauth_state"
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"
_OAUTH_ATTEMPT_TTL_SECONDS = 600
_OAUTH_ATTEMPT_LIMIT = 1024
_AUTH_SESSIONS_PER_SUBJECT = 10


@dataclass(frozen=True)
class GitHubIdentity:
    user_id: int
    login: str
    name: str = ""
    avatar_url: str = ""


class OAuthClient(Protocol):
    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> str: ...

    def get_identity(self, access_token: str) -> GitHubIdentity: ...


class GitHubOAuthClient:
    """Minimal GitHub OAuth client. Access tokens are never persisted."""

    def __init__(self, client_id: str, client_secret: str, timeout: int = 15):
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> str:
        payload = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        ).encode("ascii")
        request = urllib.request.Request(
            _GITHUB_TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json", "User-Agent": "ThreadForge"},
            method="POST",
        )
        body = self._request_json(request)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise OAuthProviderError("GitHub did not return an access token")
        return token

    def get_identity(self, access_token: str) -> GitHubIdentity:
        request = urllib.request.Request(
            _GITHUB_USER_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "ThreadForge",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        body = self._request_json(request)
        user_id = body.get("id")
        login = body.get("login")
        if not isinstance(user_id, int) or user_id <= 0 or not isinstance(login, str) or not login:
            raise OAuthProviderError("GitHub returned an invalid user profile")
        return GitHubIdentity(
            user_id=user_id,
            login=login,
            name=body.get("name") if isinstance(body.get("name"), str) else "",
            avatar_url=body.get("avatar_url") if isinstance(body.get("avatar_url"), str) else "",
        )

    def _request_json(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OAuthProviderError("GitHub authentication is temporarily unavailable") from exc
        if not isinstance(payload, dict):
            raise OAuthProviderError("GitHub returned an invalid response")
        if payload.get("error"):
            raise OAuthProviderError("GitHub rejected the authentication request")
        return payload


class OAuthAttemptStore:
    """Short-lived, one-use PKCE verifier storage for the single API process."""

    def __init__(self):
        self._attempts: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        now = time.time()
        with self._lock:
            self._purge(now)
            while len(self._attempts) >= _OAUTH_ATTEMPT_LIMIT:
                self._attempts.pop(next(iter(self._attempts)))
            self._attempts[state] = (verifier, now + _OAUTH_ATTEMPT_TTL_SECONDS)
        return state, challenge

    def consume(self, state: str) -> str:
        now = time.time()
        with self._lock:
            self._purge(now)
            attempt = self._attempts.pop(state, None)
        if attempt is None:
            raise OAuthStateInvalidError("OAuth state is invalid or expired")
        return attempt[0]

    def _purge(self, now: float) -> None:
        expired = [state for state, (_, expires_at) in self._attempts.items() if expires_at <= now]
        for state in expired:
            self._attempts.pop(state, None)


class UserStore:
    def __init__(self, root: Path, instance_owner_id: str, owner_login: str):
        self.root = Path(root)
        secure_directory(self.root)
        self._instance_owner_id = canonical_owner_id(instance_owner_id)
        self._owner_login = owner_login.lower()
        self._lock = threading.RLock()

    def upsert(self, identity: GitHubIdentity) -> Actor:
        path = self.root / f"github-{identity.user_id}.json"
        with self._lock:
            if path.exists():
                payload = read_json(path)
                owner_id = canonical_owner_id(payload["owner_id"])
            elif identity.login.lower() == self._owner_login:
                self._ensure_owner_is_unbound(identity.user_id)
                owner_id = self._instance_owner_id
            else:
                owner_id = str(uuid5(NAMESPACE_URL, f"https://github.com/user/{identity.user_id}"))
            actor = Actor(
                owner_id=owner_id,
                subject=f"github:{identity.user_id}",
                login=identity.login,
                name=identity.name,
                avatar_url=identity.avatar_url,
            )
            write_json_atomic(path, {"schema_version": 1, **actor.public_dict()})
            return actor

    def _ensure_owner_is_unbound(self, github_user_id: int) -> None:
        expected_subject = f"github:{github_user_id}"
        for path in self.root.glob("github-*.json"):
            payload = read_json(path)
            if canonical_owner_id(payload["owner_id"]) != self._instance_owner_id:
                continue
            if payload.get("subject") != expected_subject:
                raise AuthorizationDeniedError("The instance owner is already bound to another GitHub account")


class AuthSessionStore:
    def __init__(self, root: Path, ttl_seconds: int):
        self.root = Path(root)
        secure_directory(self.root)
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def create(self, actor: Actor) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        payload = {
            "schema_version": 1,
            "expires_at": now + self._ttl_seconds,
            **actor.public_dict(),
        }
        with self._lock:
            self._prune(actor.subject, now)
            write_json_atomic(self.root / f"{self._digest(token)}.json", payload)
        return token

    def _prune(self, subject: str, now: int) -> None:
        matching: list[tuple[float, Path]] = []
        for path in self.root.glob("*.json"):
            try:
                payload = read_json(path)
                if not isinstance(payload, dict):
                    raise TypeError("invalid authentication session record")
                if int(payload.get("expires_at", 0)) <= now:
                    path.unlink(missing_ok=True)
                elif payload.get("subject") == subject:
                    matching.append((path.stat().st_mtime, path))
            except (OSError, JsonCorruptedError, TypeError, ValueError):
                path.unlink(missing_ok=True)
        matching.sort(key=lambda item: item[0])
        for _, path in matching[: -(_AUTH_SESSIONS_PER_SUBJECT - 1)]:
            path.unlink(missing_ok=True)

    def get(self, token: str) -> Actor | None:
        if not token or len(token) > 256:
            return None
        path = self.root / f"{self._digest(token)}.json"
        with self._lock:
            try:
                payload = read_json(path)
                if not isinstance(payload, dict):
                    return None
            except (FileNotFoundError, JsonCorruptedError, KeyError, ValueError):
                return None
            if int(payload.get("expires_at", 0)) <= int(time.time()):
                path.unlink(missing_ok=True)
                return None
            try:
                return Actor(
                    owner_id=canonical_owner_id(payload["owner_id"]),
                    subject=str(payload["subject"]),
                    login=str(payload["login"]),
                    name=str(payload.get("name", "")),
                    avatar_url=str(payload.get("avatar_url", "")),
                )
            except (KeyError, ValueError):
                return None

    def delete(self, token: str) -> None:
        if not token or len(token) > 256:
            return
        with self._lock:
            (self.root / f"{self._digest(token)}.json").unlink(missing_ok=True)


class AuthManager:
    def __init__(
        self,
        settings: Settings,
        instance_owner_id: str,
        oauth_client: OAuthClient | None = None,
    ):
        self.settings = settings
        self._access_policy = settings.github_access_policy
        self._allowed_logins = {login.lower() for login in settings.github_allowed_logins}
        self._attempts = OAuthAttemptStore()
        self._users = UserStore(
            settings.data_dir / "users",
            instance_owner_id,
            settings.github_owner_login,
        )
        self._sessions = AuthSessionStore(
            settings.data_dir / "auth-sessions",
            settings.auth_session_ttl_seconds,
        )
        self._oauth_client = oauth_client or GitHubOAuthClient(
            settings.github_oauth_client_id,
            settings.github_oauth_client_secret,
        )

    def authorization_url(self) -> tuple[str, str]:
        state, challenge = self._attempts.create()
        query = urllib.parse.urlencode(
            {
                "client_id": self.settings.github_oauth_client_id,
                "redirect_uri": self.settings.github_oauth_callback_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{_GITHUB_AUTHORIZE_URL}?{query}", state

    def complete_login(self, code: str, state: str, state_cookie: str) -> tuple[Actor, str]:
        if not state_cookie or not hmac.compare_digest(state, state_cookie):
            raise OAuthStateInvalidError("OAuth state does not match this browser")
        verifier = self._attempts.consume(state)
        access_token = self._oauth_client.exchange_code(
            code,
            verifier,
            self.settings.github_oauth_callback_url,
        )
        identity = self._oauth_client.get_identity(access_token)
        if not self._login_allowed(identity.login):
            raise AuthorizationDeniedError("This GitHub account is not allowed to use ThreadForge")
        actor = self._users.upsert(identity)
        return actor, self._sessions.create(actor)

    def authenticate(self, token: str | None) -> Actor:
        actor = self._sessions.get(token or "")
        if actor is None:
            raise AuthenticationRequiredError("Sign in with GitHub to continue")
        if not self._login_allowed(actor.login):
            self._sessions.delete(token or "")
            raise AuthorizationDeniedError("This GitHub account is no longer allowed")
        return actor

    def optional_actor(self, token: str | None) -> Actor | None:
        actor = self._sessions.get(token or "")
        if actor is not None and not self._login_allowed(actor.login):
            self._sessions.delete(token or "")
            return None
        return actor

    def _login_allowed(self, login: str) -> bool:
        return (
            self._access_policy == "all_authenticated"
            or login.lower() in self._allowed_logins
        )

    def logout(self, token: str | None) -> None:
        self._sessions.delete(token or "")

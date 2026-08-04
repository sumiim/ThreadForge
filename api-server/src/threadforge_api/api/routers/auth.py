"""GitHub OAuth login and logout routes."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Cookie, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse

from ...config import Settings
from ...domain.errors import AppError, NotFoundError
from ...domain.identity import Actor
from ...infrastructure.auth import (
    AUTH_COOKIE_NAME,
    OAUTH_STATE_COOKIE_NAME,
    AuthManager,
)
from ..dependencies import (
    get_auth_manager,
    get_optional_actor,
    get_settings,
    require_csrf,
)

router = APIRouter()


@router.get("/api/v1/auth/status")
def auth_status(
    actor: Actor | None = Depends(get_optional_actor),
    settings: Settings = Depends(get_settings),
) -> dict:
    required = settings.identity_mode == "github_oauth"
    return {
        "identity_mode": settings.identity_mode,
        "multi_user_enabled": required,
        "authentication_required": required,
        "authenticated": actor is not None,
        "user": actor.public_dict() if required and actor is not None else None,
    }


@router.get("/api/v1/auth/github/start")
def start_github_login(
    manager: AuthManager | None = Depends(get_auth_manager),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if manager is None:
        raise NotFoundError("GitHub login is not enabled")
    location, state = manager.authorization_url()
    response = RedirectResponse(location, status_code=302)
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/api/v1/auth/github/callback",
    )
    return response


@router.get("/api/v1/auth/github/callback")
def github_callback(
    code: str = Query(default="", max_length=512),
    state: str = Query(default="", max_length=512),
    error: str = Query(default="", max_length=128),
    oauth_state_cookie: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE_NAME),
    manager: AuthManager | None = Depends(get_auth_manager),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if manager is None:
        raise NotFoundError("GitHub login is not enabled")
    if error or not code or not state:
        return _error_redirect(settings, "authentication_cancelled")
    try:
        _, token = manager.complete_login(code, state, oauth_state_cookie or "")
    except AppError as exc:
        return _error_redirect(settings, exc.code)
    response = RedirectResponse(settings.github_oauth_return_url, status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE_NAME, path="/api/v1/auth/github/callback")
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/v1/auth/logout", dependencies=[Depends(require_csrf)])
def logout(
    session_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    manager: AuthManager | None = Depends(get_auth_manager),
) -> JSONResponse:
    if manager is not None:
        manager.logout(session_token)
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


def _error_redirect(settings: Settings, code: str) -> RedirectResponse:
    parsed = urllib.parse.urlsplit(settings.github_oauth_return_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("auth_error", code))
    location = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )
    response = RedirectResponse(location, status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE_NAME, path="/api/v1/auth/github/callback")
    return response

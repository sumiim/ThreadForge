"""FastAPI dependencies resolving services from the lifespan container."""

from __future__ import annotations

from fastapi import Request

from ..application.artifact_service import ArtifactService
from ..application.session_service import SessionService
from ..application.task_service import TaskService
from ..config import Settings
from ..domain.errors import AuthorizationDeniedError
from ..domain.identity import Actor, canonical_owner_id
from ..infrastructure.auth import AUTH_COOKIE_NAME, AuthManager
from ..infrastructure.device_store import DeviceStore, PairingCodeStore
from ..infrastructure.worker_hub import WorkerHub
from ..lifespan import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_settings(request: Request) -> Settings:
    return request.app.state.container.settings


def get_actor(request: Request) -> Actor:
    """Resolve identity from the configured trusted server-side mechanism."""
    container = request.app.state.container
    if container.auth_manager is not None:
        return container.auth_manager.authenticate(request.cookies.get(AUTH_COOKIE_NAME))
    return Actor(canonical_owner_id(container.owner_id))


def get_optional_actor(request: Request) -> Actor | None:
    container = request.app.state.container
    if container.auth_manager is None:
        return Actor(canonical_owner_id(container.owner_id))
    return container.auth_manager.optional_actor(request.cookies.get(AUTH_COOKIE_NAME))


def get_auth_manager(request: Request) -> AuthManager | None:
    return request.app.state.container.auth_manager


def get_device_store(request: Request) -> DeviceStore:
    return request.app.state.container.device_store


def get_pairing_store(request: Request) -> PairingCodeStore:
    return request.app.state.container.pairing_store


def get_worker_hub(request: Request) -> WorkerHub:
    return request.app.state.container.worker_hub


def require_csrf(request: Request) -> None:
    settings = request.app.state.container.settings
    if (
        settings.identity_mode == "github_oauth"
        and request.headers.get("X-ThreadForge-CSRF") != "1"
    ):
        raise AuthorizationDeniedError("CSRF validation failed")


def get_session_service(request: Request) -> SessionService:
    return request.app.state.container.session_service


def get_task_service(request: Request) -> TaskService:
    return request.app.state.container.task_service


def get_artifact_service(request: Request) -> ArtifactService:
    return request.app.state.container.artifact_service

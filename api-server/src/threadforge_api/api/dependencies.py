"""FastAPI dependencies resolving services from the lifespan container."""

from __future__ import annotations

from fastapi import Request

from ..application.artifact_service import ArtifactService
from ..application.session_service import SessionService
from ..application.task_service import TaskService
from ..config import Settings
from ..lifespan import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_settings(request: Request) -> Settings:
    return request.app.state.container.settings


def get_session_service(request: Request) -> SessionService:
    return request.app.state.container.session_service


def get_task_service(request: Request) -> TaskService:
    return request.app.state.container.task_service


def get_artifact_service(request: Request) -> ArtifactService:
    return request.app.state.container.artifact_service

"""FastAPI application factory."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from .api.errors import install_error_handlers
from .api.routers import (
    auth,
    devices,
    health,
    metadata,
    providers,
    runs,
    sessions,
    tasks,
    worker_releases,
    workspaces,
)
from .config import Settings
from .infrastructure.auth import OAuthClient
from .lifespan import build_lifespan

logger = logging.getLogger("threadforge.access")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app(
    settings: Settings | None = None,
    *,
    model_client_factory: Callable | None = None,
    oauth_client: OAuthClient | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings().freeze_provider_env()
    openapi_enabled = bool(resolved_settings.openapi_enabled)
    app = FastAPI(
        title="ThreadForge API",
        version="0.1.0",
        openapi_url="/openapi.json" if openapi_enabled else None,
        docs_url="/docs" if openapi_enabled else None,
        redoc_url=None,
        lifespan=build_lifespan(
            settings=resolved_settings,
            model_client_factory=model_client_factory,
            oauth_client=oauth_client,
        ),
    )
    app.state.settings = resolved_settings

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved_settings.trusted_hosts,
    )
    allowed_origins = [resolved_settings.web_origin]
    if resolved_settings.desktop_origin_enabled:
        allowed_origins.append("null")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "X-ThreadForge-CSRF"],
    )

    @app.middleware("http")
    async def request_id_and_access_log(request: Request, call_next):
        header = request.headers.get("X-Request-ID", "") or ""
        if _REQUEST_ID_PATTERN.fullmatch(header):
            request_id = header
        else:
            request_id = "req_" + uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if resolved_settings.identity_mode == "github_oauth" and request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "request method=%s route=%s status=%d duration_ms=%d request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(devices.router)
    app.include_router(worker_releases.router)
    app.include_router(metadata.router)
    app.include_router(workspaces.router)
    app.include_router(sessions.router)
    app.include_router(tasks.router)
    app.include_router(providers.router)
    app.include_router(runs.router)

    install_error_handlers(app)
    return app

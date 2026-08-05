"""Worker release metadata and verified same-origin downloads."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ...domain.errors import AuthenticationRequiredError, AuthorizationDeniedError
from ...infrastructure.auth import AUTH_COOKIE_NAME
from ...infrastructure.worker_releases import WorkerReleaseService
from ..dependencies import get_worker_release_service

router = APIRouter()


def _require_user_or_device(request: Request) -> None:
    container = request.app.state.container
    if container.auth_manager is None:
        return
    if container.auth_manager.optional_actor(request.cookies.get(AUTH_COOKIE_NAME)) is not None:
        return
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        try:
            container.device_store.authenticate(authorization[7:])
            return
        except AuthorizationDeniedError:
            pass
    raise AuthenticationRequiredError("authentication is required")


@router.get("/api/v1/worker/releases/latest", dependencies=[Depends(_require_user_or_device)])
def latest_worker_release(
    releases: WorkerReleaseService = Depends(get_worker_release_service),
) -> dict:
    return releases.latest()


@router.get(
    "/api/v1/worker/releases/download/{platform_name}",
    dependencies=[Depends(_require_user_or_device)],
)
def download_worker_release(
    platform_name: str,
    releases: WorkerReleaseService = Depends(get_worker_release_service),
):
    handle, artifact = releases.open_verified_artifact(platform_name)

    def chunks() -> Iterator[bytes]:
        while chunk := handle.read(64 * 1024):
            yield chunk

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(artifact["size"]),
            "Content-Disposition": f'attachment; filename="{artifact["filename"]}"',
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(handle.close),
    )

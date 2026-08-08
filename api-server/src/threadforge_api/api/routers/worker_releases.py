"""Worker release metadata and verified same-origin downloads."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
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
    request: Request,
    releases: WorkerReleaseService = Depends(get_worker_release_service),
):
    handle, artifact = releases.open_verified_artifact(platform_name)
    artifact_size = int(artifact["size"])
    try:
        byte_range = _parse_byte_range(request.headers.get("range", ""), artifact_size)
    except ValueError:
        handle.close()
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes */{artifact_size}",
            },
        )
    if byte_range is None:
        start, end, status_code = 0, artifact_size - 1, 200
    else:
        start, end, status_code = byte_range[0], byte_range[1], 206
        handle.seek(start)
    remaining = end - start + 1

    def chunks() -> Iterator[bytes]:
        nonlocal remaining
        while remaining > 0 and (chunk := handle.read(min(1024 * 1024, remaining))):
            remaining -= len(chunk)
            yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Disposition": f'attachment; filename="{artifact["filename"]}"',
        "X-Content-Type-Options": "nosniff",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{artifact_size}"
    return StreamingResponse(
        chunks(),
        status_code=status_code,
        media_type="application/octet-stream",
        headers=headers,
        background=BackgroundTask(handle.close),
    )


def _parse_byte_range(value: str, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if size <= 0 or not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid byte range")
    bounds = value[6:].split("-", 1)
    if len(bounds) != 2:
        raise ValueError("invalid byte range")
    start_text, end_text = bounds
    if not start_text:
        if not end_text.isdigit() or int(end_text) <= 0:
            raise ValueError("invalid byte range")
        suffix_length = min(int(end_text), size)
        return size - suffix_length, size - 1
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        raise ValueError("invalid byte range")
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable byte range")
    return start, min(end, size - 1)

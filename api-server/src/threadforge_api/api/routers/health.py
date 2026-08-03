"""Health check endpoints (no version prefix)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...domain.errors import NotReadyError
from ..dependencies import get_container

router = APIRouter()


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request, container=Depends(get_container)) -> dict:
    if not container.is_ready():
        raise NotReadyError("service not ready")
    return {"status": "ready"}

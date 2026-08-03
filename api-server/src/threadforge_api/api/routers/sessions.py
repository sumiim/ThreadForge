"""Session REST routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...application.session_service import SessionService
from ...infrastructure.id_validators import validate_session_id
from ..dependencies import get_session_service
from ..models import CreateSessionRequest

router = APIRouter()

_SESSION_LIST_LIMIT_MAX = 100


@router.post("/api/v1/sessions", status_code=201)
def create_session(body: CreateSessionRequest, session_service: SessionService = Depends(get_session_service)) -> dict:
    session = session_service.create_session(body.workspace_id, body.title)
    return session


@router.get("/api/v1/sessions")
def list_sessions(
    limit: int = Query(default=50, ge=1, le=_SESSION_LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    session_service: SessionService = Depends(get_session_service),
) -> dict:
    items, total = session_service.list_sessions(limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/api/v1/sessions/{session_id}")
def get_session(
    session_id: str,
    message_limit: int = Query(default=100, ge=1, le=500),
    session_service: SessionService = Depends(get_session_service),
) -> dict:
    validate_session_id(session_id)
    return session_service.get_session(session_id, message_limit)

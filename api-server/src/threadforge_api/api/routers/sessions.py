"""Session REST routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...application.session_service import SessionService
from ...domain.identity import Actor
from ...infrastructure.id_validators import validate_session_id
from ..dependencies import get_actor, get_session_service, get_worker_hub, require_csrf
from ..models import CreateSessionRequest, RenameEntityRequest

router = APIRouter()

_SESSION_LIST_LIMIT_MAX = 100


@router.post("/api/v1/sessions", status_code=201, dependencies=[Depends(require_csrf)])
def create_session(
    body: CreateSessionRequest,
    actor: Actor = Depends(get_actor),
    session_service: SessionService = Depends(get_session_service),
) -> dict:
    session = session_service.create_session(
        workspace_id=body.workspace_id,
        title=body.title,
        owner_id=actor.owner_id,
        device_id=body.device_id,
    )
    return session


@router.get("/api/v1/sessions")
def list_sessions(
    limit: int = Query(default=50, ge=1, le=_SESSION_LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(get_actor),
    session_service: SessionService = Depends(get_session_service),
) -> dict:
    items, total = session_service.list_sessions(limit, offset, actor.owner_id)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/api/v1/sessions/{session_id}")
async def get_session(
    session_id: str,
    message_limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(get_actor),
    session_service: SessionService = Depends(get_session_service),
) -> dict:
    validate_session_id(session_id)
    return await session_service.get_session(session_id, message_limit, actor.owner_id)


@router.patch("/api/v1/sessions/{session_id}", dependencies=[Depends(require_csrf)])
async def rename_session(
    session_id: str,
    body: RenameEntityRequest,
    actor: Actor = Depends(get_actor),
    session_service: SessionService = Depends(get_session_service),
    worker_hub=Depends(get_worker_hub),
) -> dict:
    validate_session_id(session_id)
    session = session_service.load_raw(session_id, actor.owner_id)
    is_local = session.get("execution_environment") == "local_worker"
    if is_local:
        await worker_hub.rename_entity(
            device_id=str(session.get("device_id", "")),
            owner_id=actor.owner_id,
            entity_type="session",
            entity_id=session_id,
            display_name=body.display_name,
            expected_updated_at=body.expected_updated_at,
        )
    summary = session_service.rename_session(
        session_id,
        body.display_name,
        actor.owner_id,
        expected_updated_at=None if is_local else body.expected_updated_at,
    )
    return {
        **summary,
        "display_name": summary["title"],
        "display_name_source": summary.get("display_name_source", "user"),
        "display_name_updated_at": summary.get("display_name_updated_at", ""),
    }

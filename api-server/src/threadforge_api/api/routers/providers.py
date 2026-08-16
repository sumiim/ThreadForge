"""Provider REST routes（2.7 供应商窗口配置面）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...application.provider_service import ProviderService
from ...domain.identity import Actor
from ..dependencies import get_actor, get_provider_service, require_csrf
from ..models import ProviderCreateRequest, ProviderUpdateRequest

router = APIRouter()


@router.get("/api/v1/providers")
def list_providers(
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
) -> dict:
    return {"providers": provider_service.list_providers(actor.owner_id)}


@router.post(
    "/api/v1/providers",
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
def create_provider(
    body: ProviderCreateRequest,
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
) -> dict:
    return provider_service.create_provider(actor.owner_id, "", body.model_dump())


@router.get("/api/v1/providers/{provider_id}")
def get_provider(
    provider_id: str,
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
) -> dict:
    return provider_service.get_provider(provider_id, actor.owner_id)


@router.patch(
    "/api/v1/providers/{provider_id}",
    dependencies=[Depends(require_csrf)],
)
def update_provider(
    provider_id: str,
    body: ProviderUpdateRequest,
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
) -> dict:
    return provider_service.update_provider(
        provider_id, actor.owner_id, body.model_dump(exclude_none=True)
    )


@router.delete(
    "/api/v1/providers/{provider_id}",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
def delete_provider(
    provider_id: str,
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
) -> JSONResponse:
    provider_service.delete_provider(provider_id, actor.owner_id)
    return JSONResponse(status_code=204)


@router.post(
    "/api/v1/providers/{provider_id}/activate",
    dependencies=[Depends(require_csrf)],
)
def activate_provider(
    provider_id: str,
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
) -> dict:
    return provider_service.activate_provider(provider_id, actor.owner_id)

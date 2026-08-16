"""Provider REST routes（2.7 供应商窗口配置面）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ...application.provider_service import ProviderService
from ...domain.identity import Actor
from ...infrastructure.worker_hub import WorkerHub
from ..dependencies import get_actor, get_provider_service, get_worker_hub, require_csrf
from ..models import ConfigureProviderRequest, ProviderCreateRequest, ProviderUpdateRequest

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


@router.post(
    "/api/v1/providers/{provider_id}/configure",
    dependencies=[Depends(require_csrf)],
)
async def configure_provider(
    provider_id: str,
    body: ConfigureProviderRequest,
    actor: Actor = Depends(get_actor),
    provider_service: ProviderService = Depends(get_provider_service),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    # 校验 provider 归属后，把 key 经 Worker socket 转发到 device 本地；中央不落。
    provider_service.get_provider(provider_id, actor.owner_id)
    return await worker_hub.configure_provider(
        device_id=body.device_id,
        owner_id=actor.owner_id,
        provider_id=provider_id,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        protocol=body.protocol,
        reasoning_efforts=body.reasoning_efforts,
    )

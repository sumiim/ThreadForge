"""Local Worker pairing, device inventory, revocation and WebSocket transport."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status

from ...domain.errors import AppError, AuthorizationDeniedError, WorkerProtocolError
from ...domain.identity import Actor
from ...infrastructure.device_store import DeviceStore, PairingCodeStore
from ...infrastructure.worker_hub import WorkerHub
from ..dependencies import (
    get_actor,
    get_device_store,
    get_pairing_store,
    get_session_service,
    get_worker_hub,
    require_csrf,
)
from ..models import ConfigureWorkerModelRequest, PairWorkerRequest, RenameEntityRequest

router = APIRouter()
WORKER_PROTOCOL_VERSION = 1


@router.post("/api/v1/devices/pairing-codes", dependencies=[Depends(require_csrf)])
def create_pairing_code(
    actor: Actor = Depends(get_actor),
    pairing_store: PairingCodeStore = Depends(get_pairing_store),
) -> dict:
    code, expires_in = pairing_store.create(actor.owner_id)
    return {"code": code, "expires_in_seconds": expires_in}


@router.post("/api/v1/workers/pair")
def pair_worker(
    body: PairWorkerRequest,
    pairing_store: PairingCodeStore = Depends(get_pairing_store),
    device_store: DeviceStore = Depends(get_device_store),
) -> dict:
    owner_id = pairing_store.consume(body.code)
    device, token = device_store.create(owner_id, body.name)
    return {
        "device_id": device.device_id,
        "device_token": token,
        "name": device.name,
    }


@router.get("/api/v1/devices")
def list_devices(
    actor: Actor = Depends(get_actor),
    device_store: DeviceStore = Depends(get_device_store),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    online = worker_hub.online_ids(actor.owner_id)
    return {
        "items": [
            {
                "device_id": device.device_id,
                "name": device.name,
                "display_name": device.name,
                "online": device.device_id in online,
                "model": device.model,
                "model_provider": device.model_provider,
                "model_configured": device.model_configured,
                "version": device.version,
                "protocol_version": device.protocol_version,
                "platform": device.platform,
                "architecture": device.architecture,
                "compatible": device.protocol_version == WORKER_PROTOCOL_VERSION,
                "capabilities": device.capabilities,
                "orchestration_backend": device.orchestration_backend,
                "model_capabilities": device.model_capabilities,
                "update_status": device.update_status,
                "created_at": device.created_at,
                "last_seen_at": device.last_seen_at,
                "display_name_source": device.display_name_source,
                "display_name_updated_at": device.display_name_updated_at,
                "workspaces": [workspace.to_dict() for workspace in device.workspaces],
            }
            for device in device_store.list_for_owner(actor.owner_id)
        ]
    }


@router.get("/api/v1/workers/online")
def list_online_workers(
    capability: str | None = Query(default=None, min_length=1, max_length=64),
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    """List ready Workers for future multi-Worker routing.

    The endpoint is deliberately read-only.  Current task execution still
    binds one session to one selected workspace/Worker; clients may use this
    inventory to build a multi-Worker connection plan later.
    """
    items = []
    for device in worker_hub.online_devices(actor.owner_id):
        if capability and capability not in device.capabilities:
            continue
        items.append(
            {
                "worker_id": device.device_id,
                "device_id": device.device_id,
                "name": device.name,
                "display_name": device.name,
                "online": True,
                "version": device.version,
                "protocol_version": device.protocol_version,
                "platform": device.platform,
                "architecture": device.architecture,
                "compatible": device.protocol_version == WORKER_PROTOCOL_VERSION,
                "capabilities": device.capabilities,
                "orchestration_backend": device.orchestration_backend,
                "model_capabilities": device.model_capabilities,
                "workspaces": [workspace.to_dict() for workspace in device.workspaces],
            }
        )
    return {
        "items": items,
        "routing": {
            "mode": "single",
            "multi_worker": "reserved",
        },
    }


@router.patch("/api/v1/devices/{device_id}", dependencies=[Depends(require_csrf)])
def rename_device(
    device_id: str,
    body: RenameEntityRequest,
    actor: Actor = Depends(get_actor),
    device_store: DeviceStore = Depends(get_device_store),
) -> dict:
    device = device_store.rename(
        device_id,
        actor.owner_id,
        body.display_name,
        expected_updated_at=body.expected_updated_at,
    )
    return {
        "device_id": device.device_id,
        "display_name": device.name,
        "display_name_source": device.display_name_source,
        "display_name_updated_at": device.display_name_updated_at,
    }


@router.patch(
    "/api/v1/devices/{device_id}/workspaces/{workspace_id}",
    dependencies=[Depends(require_csrf)],
)
async def rename_workspace(
    device_id: str,
    workspace_id: str,
    body: RenameEntityRequest,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    return await worker_hub.rename_entity(
        device_id=device_id,
        owner_id=actor.owner_id,
        entity_type="workspace",
        entity_id=workspace_id,
        display_name=body.display_name,
        expected_updated_at=body.expected_updated_at,
    )


@router.delete(
    "/api/v1/devices/{device_id}/workspaces/{workspace_id}",
    dependencies=[Depends(require_csrf)],
)
async def delete_workspace(
    device_id: str,
    workspace_id: str,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
    session_service=Depends(get_session_service),
) -> dict:
    session_ids = session_service.session_ids_for_workspace(
        workspace_id,
        device_id,
        actor.owner_id,
    )
    session_service.ensure_sessions_deletable(session_ids, actor.owner_id)
    result = await worker_hub.delete_entity(
        device_id=device_id,
        owner_id=actor.owner_id,
        entity_type="workspace",
        entity_id=workspace_id,
        session_ids=session_ids,
        run_ids=session_service.run_ids_for_sessions(session_ids, actor.owner_id),
    )
    deleted = session_service.delete_sessions(session_ids, actor.owner_id)
    return {
        **result,
        "deleted_session_ids": deleted["deleted_session_ids"],
    }


@router.post(
    "/api/v1/devices/{device_id}/workspace-selection-requests",
    dependencies=[Depends(require_csrf)],
    status_code=status.HTTP_202_ACCEPTED,
)
def request_workspace_selection(
    device_id: str,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    return worker_hub.request_workspace_selection(device_id, actor.owner_id)


@router.get(
    "/api/v1/devices/{device_id}/workspace-selection-requests/{request_id}"
)
def get_workspace_selection(
    device_id: str,
    request_id: str,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    return worker_hub.get_workspace_selection(device_id, request_id, actor.owner_id)


@router.put(
    "/api/v1/devices/{device_id}/model-config",
    dependencies=[Depends(require_csrf)],
)
async def configure_worker_model(
    device_id: str,
    body: ConfigureWorkerModelRequest,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    # The key is forwarded over the authenticated Worker socket and remains
    # in memory only; neither the device store nor API responses persist it.
    return await worker_hub.configure_model(
        device_id=device_id,
        owner_id=actor.owner_id,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        model_provider=body.model_provider,
    )


@router.post(
    "/api/v1/devices/{device_id}/uninstall",
    dependencies=[Depends(require_csrf)],
)
async def uninstall_worker(
    device_id: str,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    return await worker_hub.uninstall_worker(device_id=device_id, owner_id=actor.owner_id)


@router.post(
    "/api/v1/devices/{device_id}/update",
    dependencies=[Depends(require_csrf)],
)
async def update_worker(
    device_id: str,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    return await worker_hub.update_worker(device_id=device_id, owner_id=actor.owner_id)


@router.delete("/api/v1/devices/{device_id}", dependencies=[Depends(require_csrf)])
def revoke_device(
    device_id: str,
    actor: Actor = Depends(get_actor),
    worker_hub: WorkerHub = Depends(get_worker_hub),
) -> dict:
    worker_hub.revoke(device_id, actor.owner_id)
    return {"status": "revoked", "device_id": device_id}


@router.websocket("/api/v1/workers/connect")
async def worker_connect(websocket: WebSocket):
    container = websocket.app.state.container
    authorization = websocket.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        await websocket.close(code=4401, reason="device token required")
        return
    try:
        device = container.device_store.authenticate(authorization[7:])
    except AuthorizationDeniedError:
        await websocket.close(code=4403, reason="invalid device token")
        return
    hub: WorkerHub = container.worker_hub
    connection = await hub.connect(device, websocket)
    sender = asyncio.create_task(hub.sender(connection))
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > container.settings.worker_message_max_bytes:
                raise WorkerProtocolError("Worker message exceeds size limit")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise WorkerProtocolError("Worker message must be JSON") from exc
            if not isinstance(message, dict):
                raise WorkerProtocolError("Worker message must be an object")
            await hub.handle(connection, message)
    except WebSocketDisconnect:
        pass
    except AppError as exc:
        try:
            await websocket.send_json({"type": "protocol.error", "code": exc.code, "message": exc.message})
            await websocket.close(code=4400, reason=exc.code)
        except Exception:
            pass
    finally:
        await hub.disconnect(connection)
        sender.cancel()
        try:
            await sender
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

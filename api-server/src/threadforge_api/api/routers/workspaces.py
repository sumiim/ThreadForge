"""Workspace allowlist listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...domain.enums import ExecutionEnvironment
from ...domain.identity import Actor
from ..dependencies import get_actor, get_container

router = APIRouter()


@router.get("/api/v1/workspaces")
def list_workspaces(
    actor: Actor = Depends(get_actor),
    container=Depends(get_container),
) -> dict:
    items = []
    if container.settings.identity_mode != "github_oauth":
        for entry in container.workspace_catalog.list():
            items.append(
                {
                    "workspace_id": entry.workspace_id,
                    "name": entry.name,
                    "display_path": str(entry.canonical_path),
                    "available": entry.available,
                    "is_git": entry.is_git,
                    "execution_environment": ExecutionEnvironment.BACKEND_PROCESS.value,
                    "container_sandbox_enabled": False,
                }
            )
    online = container.worker_hub.online_ids(actor.owner_id)
    for device in container.device_store.list_for_owner(actor.owner_id):
        for workspace in device.workspaces:
            items.append(
                {
                    "workspace_id": workspace.workspace_id,
                    "name": workspace.name,
                    "display_path": f"{device.name} / {workspace.name}",
                    "available": device.device_id in online,
                    "is_git": workspace.is_git,
                    "execution_environment": ExecutionEnvironment.LOCAL_WORKER.value,
                    "device_id": device.device_id,
                    "device_name": device.name,
                    "model": device.model,
                    "model_configured": device.model_configured,
                    "container_sandbox_enabled": False,
                }
            )

    return {"items": items}

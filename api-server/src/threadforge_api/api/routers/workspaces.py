"""Workspace allowlist listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...domain.enums import ExecutionEnvironment
from ..dependencies import get_container

router = APIRouter()


@router.get("/api/v1/workspaces")
def list_workspaces(container=Depends(get_container)) -> dict:
    items = []
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
    return {"items": items}

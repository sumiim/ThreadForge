"""Run artifact query routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response

from ...application.artifact_service import ArtifactService
from ...domain.errors import NotFoundError
from ...infrastructure.id_validators import validate_run_id
from ..dependencies import get_artifact_service

router = APIRouter()


@router.get("/api/v1/runs/{run_id}/artifacts")
def list_artifacts(run_id: str, artifact_service: ArtifactService = Depends(get_artifact_service)) -> dict:
    validate_run_id(run_id)
    items = artifact_service.list_artifacts(run_id)
    return {"run_id": run_id, "items": items}


@router.get("/api/v1/runs/{run_id}/artifacts/{name}")
def get_artifact(
    run_id: str,
    name: str,
    artifact_service: ArtifactService = Depends(get_artifact_service),
) -> Response:
    validate_run_id(run_id)
    result = artifact_service.get_artifact(run_id, name)
    if not result["found"]:
        raise NotFoundError(f"artifact not found: {name}")
    if name == "trace":
        return Response(content=result["text"], media_type="application/x-ndjson")
    try:
        parsed = json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise NotFoundError(f"artifact unreadable: {name}") from exc
    return Response(content=json.dumps(parsed, ensure_ascii=False), media_type="application/json")

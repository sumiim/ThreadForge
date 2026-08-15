"""Task REST + SSE routes."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...application.task_service import TaskService
from ...domain.identity import Actor
from ...infrastructure.event_broker import CLOSED
from ...infrastructure.id_validators import validate_approval_id, validate_task_id
from ..dependencies import (
    get_actor,
    get_container,
    get_settings,
    get_task_service,
    require_csrf,
)
from ..models import AppendMessageRequest, ApprovalDecisionRequest, CreateTaskRequest, TaskQueuedResponse

router = APIRouter()

_TERMINAL_STATUSES = {"completed", "cancelled", "failed", "interrupted", "blocked"}


@router.post(
    "/api/v1/tasks",
    status_code=202,
    response_model=TaskQueuedResponse,
    dependencies=[Depends(require_csrf)],
)
def create_task(
    body: CreateTaskRequest,
    actor: Actor = Depends(get_actor),
    settings=Depends(get_settings),
    task_service: TaskService = Depends(get_task_service),
) -> TaskQueuedResponse:
    max_steps = body.max_steps if body.max_steps is not None else settings.max_steps
    task = task_service.create_task(
        body.session_id,
        body.input,
        max_steps,
        actor.owner_id,
        model_id=body.model_id,
        reasoning_effort=body.reasoning_effort,
        permission_mode=body.permission_mode,
    )
    return TaskQueuedResponse(
        task_id=task.task_id,
        run_id=task.run_id,
        session_id=task.session_id,
        status=task.status.value,
        events_url=f"/api/v1/tasks/{task.task_id}/events",
    )


@router.get("/api/v1/tasks/{task_id}")
def get_task(
    task_id: str,
    actor: Actor = Depends(get_actor),
    task_service: TaskService = Depends(get_task_service),
) -> dict:
    validate_task_id(task_id)
    return task_service.get_task(task_id, actor.owner_id)


@router.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_task(
    task_id: str,
    actor: Actor = Depends(get_actor),
    task_service: TaskService = Depends(get_task_service),
) -> JSONResponse:
    validate_task_id(task_id)
    snapshot = task_service.cancel_task(task_id, actor.owner_id)
    status_code = 200 if snapshot["status"] in _TERMINAL_STATUSES else 202
    return JSONResponse(status_code=status_code, content=snapshot)


@router.post(
    "/api/v1/tasks/{task_id}/approvals/{approval_id}",
    dependencies=[Depends(require_csrf)],
)
def resolve_approval(
    task_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    actor: Actor = Depends(get_actor),
    task_service: TaskService = Depends(get_task_service),
) -> dict:
    validate_task_id(task_id)
    validate_approval_id(approval_id)
    return task_service.resolve_approval(task_id, approval_id, body.decision, actor.owner_id)


@router.post(
    "/api/v1/tasks/{task_id}/messages",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
def append_message(
    task_id: str,
    body: AppendMessageRequest,
    actor: Actor = Depends(get_actor),
    task_service: TaskService = Depends(get_task_service),
) -> JSONResponse:
    validate_task_id(task_id)
    result = task_service.append_message(task_id, body.content, body.wake, actor.owner_id)
    return JSONResponse(status_code=202, content=result)


@router.get(
    "/api/v1/tasks/{task_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-sent task event stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def task_events(
    task_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
    settings=Depends(get_settings),
    container=Depends(get_container),
):
    # 404 before entering the stream.
    validate_task_id(task_id)
    task_service = container.task_service
    task_service.get_task(task_id, actor.owner_id)  # raises TaskNotFoundError
    snapshot_factory = lambda: task_service.get_task(task_id, actor.owner_id)
    queue, snapshot = container.broker.subscribe(task_id, snapshot_factory)
    snapshot_event = {
        "event_id": "evt_snapshot",
        "sequence": 0,
        "type": "task.snapshot",
        "task_id": task_id,
        "run_id": snapshot.get("run_id", ""),
        "timestamp": _now(),
        "data": snapshot,
    }

    _TERMINAL_TYPES = {
        "task.completed",
        "task.cancelled",
        "task.failed",
        "task.interrupted",
        "task.blocked",
    }

    async def event_stream():
        try:
            yield _frame(snapshot_event)
            if snapshot.get("status") in _TERMINAL_STATUSES:
                # Task already terminal — send snapshot and close.
                container.broker.unsubscribe(task_id, queue)
                return
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=settings.sse_heartbeat_seconds)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is CLOSED:
                    break
                yield _frame(item)
                if item.get("type") in _TERMINAL_TYPES:
                    break
        finally:
            container.broker.unsubscribe(task_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False)
    return f"id: {event.get('event_id', '')}\nevent: {event.get('type', '')}\ndata: {data}\n\n"


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

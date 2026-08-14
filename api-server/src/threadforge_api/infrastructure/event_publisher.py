"""Thread-safe public event envelope builder feeding the EventBroker."""

from __future__ import annotations

import itertools
import threading
import uuid

from ..domain.events import PublicEvent
from .event_broker import EventBroker

_TERMINAL_TYPES = {
    "task.completed",
    "task.cancelled",
    "task.failed",
    "task.interrupted",
    "task.blocked",
}


class EventPublisher:
    def __init__(self, broker: EventBroker):
        self._broker = broker
        self._sequences: dict[str, itertools.count] = {}
        self._lock = threading.Lock()

    def publish(
        self,
        task_id: str,
        run_id: str,
        event_type: str,
        data: dict,
        *,
        trace_id: str = "",
        parent_event_id: str = "",
        phase: str = "",
        attempt: int | None = None,
        started_at: str = "",
        ended_at: str = "",
        status: str = "",
        summary: str = "",
    ) -> PublicEvent:
        with self._lock:
            counter = self._sequences.get(task_id)
            if counter is None:
                counter = itertools.count()
                self._sequences[task_id] = counter
            sequence = next(counter)
        if event_type in _TERMINAL_TYPES:
            with self._lock:
                self._sequences.pop(task_id, None)
        event = PublicEvent(
            event_id="evt_" + uuid.uuid4().hex,
            sequence=sequence,
            type=event_type,
            task_id=task_id,
            run_id=run_id,
            trace_id=trace_id or run_id,
            parent_event_id=parent_event_id,
            phase=phase,
            attempt=attempt,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            summary=summary,
            data=dict(data or {}),
        )
        self._broker.publish(task_id, event.to_dict())
        return event

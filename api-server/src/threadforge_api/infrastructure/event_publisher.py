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

# Conversation text that must never be persisted to the central control plane:
# local Worker sessions own their message bodies, so the durable event replay
# only keeps the envelope + index/summary, never the answer body itself.
_PERSISTENCE_SCRUB_KEYS = {"final_answer", "text", "input"}


def _scrubbed_for_persistence(event: dict) -> dict:
    """Return a copy of ``event`` safe to write to durable storage."""
    scrubbed = dict(event)
    data = scrubbed.get("data")
    if isinstance(data, dict):
        cleaned = {key: value for key, value in data.items() if key not in _PERSISTENCE_SCRUB_KEYS}
        scrubbed["data"] = cleaned
        scrubbed["attributes"] = cleaned
    return scrubbed


class EventPublisher:
    def __init__(self, broker: EventBroker, store=None):
        self._broker = broker
        self._store = store
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
        if self._store is not None:
            # Best-effort durable replay: the in-memory broker remains the P0
            # SSE source of truth; a failure to persist must not break delivery.
            try:
                self._store.insert_event(_scrubbed_for_persistence(event.to_dict()))
            except Exception:
                pass
        return event

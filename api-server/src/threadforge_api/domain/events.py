"""Public SSE event envelope.

This is the single normalized event contract consumed by both the REST snapshot
and the SSE stream. The typed payload stays in ``data`` (schema-validated and
secret-redacted); ``attributes`` is an alias emitted for forward compatibility,
and ``phase``/``status``/``summary``/``attempt``/``started_at``/``ended_at``/
``parent_event_id`` are lifted to the envelope so the frontend can project the
timeline without guessing from ``data``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .entities import utc_now

# Coarse execution phase for the layered trace. The ``agent.state`` event may
# override this with a finer phase string; every other event falls back to this
# type-derived mapping so a run always has a stable lane/phase.
_PHASE_BY_TYPE_PREFIX = {
    "plan.": "plan",
    "model.": "model",
    "assistant.": "talk",
    "tool.": "execute",
    "approval.": "approval",
    "review.": "review",
    "policy.": "execute",
    "sandbox.": "execute",
    "workspace.": "execute",
    "message.completed": "final",
    "task.completed": "final",
    "task.cancelled": "final",
    "task.failed": "final",
    "task.interrupted": "final",
    "task.blocked": "final",
    "task.cancel_requested": "final",
    "task.queued": "system",
    "task.started": "system",
    "task.snapshot": "system",
}


def event_phase(event_type: str) -> str:
    """Return a stable coarse phase for a public event type."""
    if event_type in _PHASE_BY_TYPE_PREFIX:
        return _PHASE_BY_TYPE_PREFIX[event_type]
    prefix = event_type.split(".", 1)[0] + "."
    return _PHASE_BY_TYPE_PREFIX.get(prefix, "system")


@dataclass
class PublicEvent:
    event_id: str
    sequence: int
    type: str
    task_id: str
    run_id: str
    timestamp: str = field(default_factory=utc_now)
    trace_id: str = ""
    parent_event_id: str = ""
    phase: str = ""
    attempt: int | None = None
    started_at: str = ""
    ended_at: str = ""
    status: str = ""
    summary: str = ""
    attributes: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        # ``attributes`` is the canonical redacted payload alias; keep it in
        # lockstep with ``data`` so legacy and new consumers read one source.
        payload["attributes"] = dict(payload.get("data") or {})
        if not payload.get("trace_id"):
            payload["trace_id"] = payload.get("run_id", "")
        if not payload.get("phase"):
            payload["phase"] = event_phase(self.type)
        return payload

"""Public SSE event envelope."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .entities import utc_now


@dataclass
class PublicEvent:
    event_id: str
    sequence: int
    type: str
    task_id: str
    run_id: str
    timestamp: str = field(default_factory=utc_now)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

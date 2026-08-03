"""In-memory EventBroker bridging Runner threads to asyncio SSE subscribers.

Each subscriber has a bounded queue. A full queue closes that subscriber
(slow consumer) without blocking the Runtime thread. Registration and the
first snapshot are performed inside the same critical section.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

CLOSED = object()  # sentinel: subscriber was slow / stream should end


class EventBroker:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue_size: int = 256):
        self._loop = loop
        self._queue_size = int(queue_size)
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = threading.Lock()

    def subscribe(self, task_id: str, snapshot_factory: Callable[[], Any]):
        """Register a subscriber and read the current snapshot atomically.

        Returns ``(queue, snapshot)``. Raises if ``snapshot_factory`` raises.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers[task_id].add(queue)
            try:
                snapshot = snapshot_factory()
            except BaseException:
                self._subscribers[task_id].discard(queue)
                raise
        return queue, snapshot

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            group = self._subscribers.get(task_id, set())
            group.discard(queue)
            if not group:
                self._subscribers.pop(task_id, None)

    def publish(self, task_id: str, event_dict: dict) -> None:
        with self._lock:
            queues = list(self._subscribers.get(task_id, ()))
        if not queues:
            return
        for queue in queues:
            try:
                self._loop.call_soon_threadsafe(self._enqueue, queue, event_dict)
            except RuntimeError:
                # loop is shutting down
                pass

    def _enqueue(self, queue: asyncio.Queue, event_dict: dict) -> None:
        if queue.full():
            # Slow consumer — drop the event and try to deliver CLOSED.
            # Drain one item from the queue (discard) then enqueue CLOSED
            # so the generator can exit cleanly.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(CLOSED)
            except asyncio.QueueFull:
                pass
        else:
            queue.put_nowait(event_dict)

    def close_all(self) -> None:
        with self._lock:
            queues = [q for group in self._subscribers.values() for q in group]
            self._subscribers.clear()
        for queue in queues:
            try:
                self._loop.call_soon_threadsafe(self._enqueue, queue, CLOSED)
            except RuntimeError:
                pass

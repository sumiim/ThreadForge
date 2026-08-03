"""Per-run linearization gate and fencing generation."""

from __future__ import annotations

import threading


class RunGate:
    """A reentrant lock plus a fencing generation for one active Run.

    Control-plane operations (cancellation, approval resolution, terminal
    writes) serialize on this gate. When the service is being force-shut
    down, ``close()`` bumps the generation so stale Runner writes are rejected.
    """

    def __init__(self, generation: int = 0):
        self.lock = threading.RLock()
        self.generation = int(generation)
        self._closed = False

    def close(self) -> None:
        with self.lock:
            self._closed = True
            self.generation += 1

    @property
    def closed(self) -> bool:
        return self._closed

    def check_generation(self, expected: int) -> bool:
        return not self._closed and expected == self.generation

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.lock.release()
        return False

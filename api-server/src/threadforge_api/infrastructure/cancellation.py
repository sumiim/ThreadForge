"""Thread-safe cancellation token implementing the pico protocol."""

from __future__ import annotations

import threading

from pico.execution_hooks import RunCancelled


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelled()

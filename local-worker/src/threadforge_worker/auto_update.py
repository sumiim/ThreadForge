"""Background Worker update loop.

The loop performs one update check immediately after the Worker handshake and
then waits between checks.  It never runs while a task is active, and a failed
check only delays the next attempt; the current Worker keeps serving requests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .config import ConfigStore
from .updater import (
    DeviceUnauthorizedError,
    UpdateStatusCallback,
    apply_update,
    update_available,
)

DEFAULT_CHECK_INTERVAL_SECONDS = 5 * 60
DEFAULT_RETRY_INTERVAL_SECONDS = 30
LOGGER = logging.getLogger(__name__)


def run_auto_update_loop(
    store: ConfigStore,
    client,
    *,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    apply_update_fn: Callable[[ConfigStore, UpdateStatusCallback | None], bool] = apply_update,
    check_update_fn: Callable[[ConfigStore], tuple[bool, dict]] = update_available,
    status_callback: UpdateStatusCallback | None = None,
) -> None:
    """Check for signed updates until the Worker stops or updates itself.

    The availability check runs WITHOUT the update lock: a manifest fetch never
    replaces the running Worker, so it must not reject concurrent tasks. Only
    once an update is actually available does the loop acquire the update lock
    (``begin_update``, which also rejects new tasks) and download + install.
    ``check_update_fn`` and ``apply_update_fn`` are injectable for unit tests.
    """

    next_wait = 0.0
    while not client.wait_for_stop(next_wait):
        next_wait = max(1.0, check_interval_seconds)
        try:
            available, _ = check_update_fn(store)
        except DeviceUnauthorizedError as exc:
            # 永久鉴权失败：令牌失效后重试无意义，退到正常检查间隔，避免每 30s 打一次 401。
            LOGGER.warning("Worker device token is no longer authorized: %s", exc)
            next_wait = max(1.0, check_interval_seconds)
            continue
        except Exception as exc:
            LOGGER.warning("Worker update check skipped: %s", exc)
            next_wait = max(1.0, retry_interval_seconds)
            continue
        if not available:
            continue
        if not client.begin_update():
            # A task is active; try again on the normal interval.
            continue
        try:
            if apply_update_fn(store, status_callback):
                # The installer replaces the binaries after this process exits.
                client.stop()
                return
        except Exception as exc:
            LOGGER.warning("Worker update check skipped: %s", exc)
            next_wait = max(1.0, retry_interval_seconds)
        finally:
            client.end_update()

"""RunStore wrapper that rejects late artifact writes after fencing."""

from __future__ import annotations

from pico.run_store import RunStore

from .run_gate import RunGate


class StaleRunnerWriteError(RuntimeError):
    pass


class FencedRunStore:
    """Owns a rooted RunStore plus a Run lease.

    After ``gate.close()`` every artifact write raises
    :class:`StaleRunnerWriteError`, so a late daemon Runner cannot overwrite a
    ``service_shutdown_timeout`` terminal state.

    The check and the actual write are performed inside the same gate lock
    so that a concurrent ``gate.close()`` cannot land between them.
    """

    def __init__(self, base: RunStore, gate: RunGate):
        self._base = base
        self._gate = gate

    def _guarded(self, fn):
        with self._gate:
            if self._gate.closed:
                raise StaleRunnerWriteError()
            return fn()

    def run_dir(self, run_id):
        return self._base.run_dir(run_id)

    def task_state_path(self, run_id):
        return self._base.task_state_path(run_id)

    def trace_path(self, run_id):
        return self._base.trace_path(run_id)

    def report_path(self, run_id):
        return self._base.report_path(run_id)

    def start_run(self, task_state):
        return self._guarded(lambda: self._base.start_run(task_state))

    def write_task_state(self, task_state):
        return self._guarded(lambda: self._base.write_task_state(task_state))

    def append_trace(self, task_state, event):
        return self._guarded(lambda: self._base.append_trace(task_state, event))

    def write_report(self, task_state, report):
        return self._guarded(lambda: self._base.write_report(task_state, report))

    def load_task_state(self, run_id):
        return self._base.load_task_state(run_id)

    def load_report(self, run_id):
        return self._base.load_report(run_id)

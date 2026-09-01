"""Cross-platform trackable, terminable shell process tree.

POSIX: the child runs in its own process group (`start_new_session=True`);
termination sends SIGTERM to the whole group and SIGKILL after a grace period.

Windows: a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is created,
the ``cmd.exe`` child is started *suspended*, assigned to the Job *before* it
is resumed, and no breakaway is allowed. Killing the Job terminates the whole
tree. If Job creation / assignment / resume fails, the caller fails closed
with :class:`ProcessContainmentUnavailable` — the command must not run with
unconstrained containment.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress

from .subprocess_utils import hidden_process_creation_flags


class ProcessContainmentUnavailable(RuntimeError):
    """Raised when platform process containment cannot be initialized."""


class ShellResult:
    __slots__ = (
        "cancelled",
        "cleanup_succeeded",
        "output_truncated",
        "returncode",
        "stderr",
        "stdout",
        "timed_out",
    )

    def __init__(
        self,
        returncode,
        stdout,
        stderr,
        timed_out=False,
        output_truncated=False,
        cancelled=False,
        cleanup_succeeded=True,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.output_truncated = output_truncated
        self.cancelled = cancelled
        self.cleanup_succeeded = cleanup_succeeded


class _OutputBudget:
    def __init__(self, limit):
        self._remaining = max(0, int(limit))
        self._lock = threading.Lock()
        self.truncated = False

    def take(self, chunk):
        with self._lock:
            if len(chunk) > self._remaining:
                kept = chunk[: self._remaining]
                self._remaining = 0
                self.truncated = True
                return kept
            self._remaining -= len(chunk)
            return chunk


def _is_windows():
    return sys.platform == "win32"


class ShellProcess:
    """A shell command whose process tree can be terminated.

    ``run()`` drains stdout/stderr with a bounded cap, enforces ``timeout``,
    checks the optional ``cancellation_token`` and terminates the tree when
    the timeout fires or cancellation is observed.

    Optional ``resource_limits`` (``memory_bytes`` / ``max_processes`` /
    ``cpu_seconds``) apply OS-level containment on top of the process tree:
    Windows via Job Object limits, POSIX via ``resource.setrlimit``. This is
    the OS-native sandbox backend used when ``sandbox_backend="os"``.
    """

    def __init__(
        self,
        command: str,
        *,
        cwd,
        env,
        timeout: int,
        output_max_bytes: int,
        cancellation_token=None,
        cleanup_grace_seconds: float = 5.0,
        resource_limits: dict | None = None,
    ):
        self.command = command
        self.timeout = int(timeout)
        self.output_max_bytes = max(0, int(output_max_bytes))
        self.cancellation_token = cancellation_token
        self.cleanup_grace_seconds = max(0.0, float(cleanup_grace_seconds))
        self._resource_limits = dict(resource_limits or {})
        if _is_windows():
            self._impl = _WindowsJobProcess(
                command, cwd=cwd, env=env, resource_limits=self._resource_limits
            )
        else:
            self._impl = _PosixGroupProcess(
                command, cwd=cwd, env=env, resource_limits=self._resource_limits
            )

    @property
    def pipe_cap(self):
        return self.output_max_bytes

    def is_running(self):
        return self._impl.is_running()

    def run(self):
        budget = _OutputBudget(self.output_max_bytes)
        return self._impl.run(
            timeout=self.timeout,
            output_budget=budget,
            cancellation_token=self.cancellation_token,
            cleanup_grace_seconds=self.cleanup_grace_seconds,
        )

    def terminate(self, grace_seconds: float):
        """Terminate the whole tree; force-kill after the grace period."""
        return self._impl.terminate(grace_seconds)


class _PosixGroupProcess:
    def __init__(self, command, *, cwd, env, resource_limits=None):
        limits = dict(resource_limits or {})

        def _apply_rlimits():  # runs in the child before exec (POSIX only)
            try:
                import resource

                cpu_secs = int(limits.get("cpu_seconds", 0))
                rss_bytes = int(limits.get("memory_bytes", 0))
                nproc = int(limits.get("max_processes", 0))
                if cpu_secs > 0:
                    resource.setrlimit(resource.RLIMIT_CPU, (cpu_secs, cpu_secs))
                if rss_bytes > 0:
                    resource.setrlimit(resource.RLIMIT_AS, (rss_bytes, rss_bytes))
                    resource.setrlimit(resource.RLIMIT_RSS, (rss_bytes, rss_bytes))
                if nproc > 0:
                    resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            except Exception:  # noqa: S110 - best-effort in child pre-exec; Job Object on
                # Windows and process-group containment remain authoritative, and a
                # failed RLIMIT must not abort the child (which would cascade a spurious
                # failure). The resource limit simply won't be enforced on this host.
                pass

        try:
            self.proc = subprocess.Popen(  # noqa: S602 - deliberate shell command execution
                command,
                cwd=cwd,
                shell=True,
                env=env,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                preexec_fn=_apply_rlimits if limits else None,
            )
        except OSError as exc:
            raise ProcessContainmentUnavailable(str(exc)) from exc
        # start_new_session makes the child the process-group leader. Keep the
        # PGID because querying it through an already-exited leader can fail
        # while descendants in the group are still alive.
        self._pgid = self.proc.pid

    def is_running(self):
        return self.proc.poll() is None

    def run(self, *, timeout, output_budget, cancellation_token, cleanup_grace_seconds):
        out_reader = _PipeReader(self.proc.stdout, output_budget)
        err_reader = _PipeReader(self.proc.stderr, output_budget)
        out_reader.start()
        err_reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = False
        cleanup_succeeded = True
        while True:
            if cancellation_token is not None and cancellation_token.is_cancelled():
                cancelled = True
                cleanup_succeeded = self.terminate(cleanup_grace_seconds)
                break
            if self.proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                cleanup_succeeded = self.terminate(cleanup_grace_seconds)
                break
            time.sleep(0.02)
        readers_finished = _finish_readers(out_reader, err_reader)
        if not readers_finished:
            cleanup_succeeded = self.terminate(cleanup_grace_seconds)
            readers_finished = _finish_readers(out_reader, err_reader)
            cleanup_succeeded = cleanup_succeeded and readers_finished
        return ShellResult(
            self.proc.returncode if self.proc.returncode is not None else -1,
            out_reader.text(),
            err_reader.text(),
            timed_out=timed_out,
            output_truncated=output_budget.truncated,
            cancelled=cancelled,
            cleanup_succeeded=cleanup_succeeded,
        )

    def _terminate_group(self):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self._pgid, signal.SIGTERM)

    def _force_kill(self):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self._pgid, signal.SIGKILL)

    def terminate(self, grace_seconds: float):
        self._terminate_group()
        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        while self._group_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._group_alive():
            self._force_kill()
            kill_deadline = time.monotonic() + 5.0
            while self._group_alive() and time.monotonic() < kill_deadline:
                time.sleep(0.02)
        with suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=0.1)
        return not self._group_alive()

    def _group_alive(self):
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True


class _WindowsJobProcess:
    def __init__(self, command, *, cwd, env, resource_limits=None):
        self._job = None
        self._proc = None
        self._thread_handle = None
        self._out_handles = None
        self._err_handles = None
        self._write_handles = []
        self._lifecycle_lock = threading.RLock()
        self._resource_limits = dict(resource_limits or {})
        try:
            import win32api
            import win32con
            import win32file
            import win32job
            import win32pipe
            import win32process

            self._win32 = (win32api, win32con, win32file, win32job, win32pipe, win32process)
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            # OS-native sandbox resource limits (Job Object). Applied up-front so
            # the job caps resources before any command can over-consume.
            self._apply_job_resource_limits(win32job, info)
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            self._job = job

            out_r, out_w = win32pipe.CreatePipe(None, 0)
            err_r, err_w = win32pipe.CreatePipe(None, 0)
            self._out_handles = out_r
            self._err_handles = err_r
            self._write_handles = [out_w, err_w]
            for handle in (out_w, err_w):
                win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 1)

            si = win32process.STARTUPINFO()
            si.dwFlags = win32con.STARTF_USESTDHANDLES
            si.hStdInput = win32api.GetStdHandle(win32api.STD_INPUT_HANDLE)
            si.hStdOutput = out_w
            si.hStdError = err_w

            creation_flags = (
                win32process.CREATE_SUSPENDED
                | win32process.CREATE_NEW_PROCESS_GROUP
                | win32process.CREATE_UNICODE_ENVIRONMENT
                | hidden_process_creation_flags()
            )
            # 与 subprocess.run(shell=True) 语义一致，经 cmd.exe /c 执行命令串。  # noqa: RUF003
            comspec = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
            # /S with an outer quote preserves the shell=True convention for
            # commands whose executable path itself is quoted.
            full_command = f'"{comspec}" /d /s /c "{command}"'
            handle, thread, pid, _tid = win32process.CreateProcess(
                None,
                full_command,
                None,
                None,
                1,  # bInheritHandles — std pipe handles must reach the child
                creation_flags,
                env,
                str(cwd),
                si,
            )
            self._proc = (handle, pid)
            self._thread_handle = thread
            # Child is still suspended; assign to the Job before it can spawn.
            win32job.AssignProcessToJobObject(job, handle)
            win32process.ResumeThread(thread)
            win32file.CloseHandle(out_w)
            win32file.CloseHandle(err_w)
            self._write_handles = []
            win32file.CloseHandle(thread)
            self._thread_handle = None
        except Exception as exc:
            self._cleanup()
            raise ProcessContainmentUnavailable(str(exc)) from exc

    def _apply_job_resource_limits(self, win32job, info):
        """Apply OS-native Job Object resource limits from ``resource_limits``.

        ``memory_bytes`` -> ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` + ``JobMemoryLimit``
        ``max_processes`` -> ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` + ``ActiveProcessLimit``
        Unknown/unparseable values are ignored (the retained KILL_ON_JOB_CLOSE
        and process-group containment still apply).
        """
        limits = self._resource_limits
        basic = info["BasicLimitInformation"]
        try:
            memory_bytes = int(limits.get("memory_bytes", 0))
        except (TypeError, ValueError):
            memory_bytes = 0
        try:
            max_processes = int(limits.get("max_processes", 0))
        except (TypeError, ValueError):
            max_processes = 0
        if memory_bytes > 0:
            basic["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info["ProcessMemoryLimit"] = memory_bytes
            info["JobMemoryLimit"] = memory_bytes
        if max_processes > 0:
            basic["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            basic["ActiveProcessLimit"] = max_processes

    def _cleanup(self):
        if self._job is not None:
            with suppress(Exception):
                import win32job

                win32job.TerminateJobObject(self._job, 1)
        if self._proc is not None:
            with suppress(Exception):
                import win32api

                win32api.TerminateProcess(self._proc[0], 1)
        for handle in self._write_handles:
            with suppress(Exception):
                import win32file

                win32file.CloseHandle(handle)
        self._write_handles = []
        if self._out_handles is not None:
            with suppress(Exception):
                import win32file

                win32file.CloseHandle(self._out_handles)
            self._out_handles = None
        if self._err_handles is not None:
            with suppress(Exception):
                import win32file

                win32file.CloseHandle(self._err_handles)
            self._err_handles = None
        if self._thread_handle is not None:
            with suppress(Exception):
                import win32file

                win32file.CloseHandle(self._thread_handle)
            self._thread_handle = None
        if self._proc is not None:
            with suppress(Exception):
                import win32file

                win32file.CloseHandle(self._proc[0])
            self._proc = None
        if self._job is not None:
            with suppress(Exception):
                import win32file

                win32file.CloseHandle(self._job)
            self._job = None

    def is_running(self):
        with self._lifecycle_lock:
            if self._proc is None:
                return False
            import win32event

            handle, _ = self._proc
            return win32event.WaitForSingleObject(handle, 0) != 0  # 0 == WAIT_OBJECT_0 (signaled)

    def run(self, *, timeout, output_budget, cancellation_token, cleanup_grace_seconds):
        out_reader = _WinPipeReader(self._out_handles, output_budget)
        err_reader = _WinPipeReader(self._err_handles, output_budget)
        self._out_handles = None
        self._err_handles = None
        out_reader.start()
        err_reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = False
        cleanup_succeeded = True
        while True:
            if cancellation_token is not None and cancellation_token.is_cancelled():
                cancelled = True
                cleanup_succeeded = self.terminate(cleanup_grace_seconds)
                break
            if not self.is_running():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                cleanup_succeeded = self.terminate(cleanup_grace_seconds)
                break
            time.sleep(0.02)
        readers_finished = _finish_readers(out_reader, err_reader)
        if not readers_finished:
            cleanup_succeeded = self.terminate(cleanup_grace_seconds)
            readers_finished = _finish_readers(out_reader, err_reader)
            cleanup_succeeded = cleanup_succeeded and readers_finished
        result = ShellResult(
            self._exit_code(),
            out_reader.text(),
            err_reader.text(),
            timed_out=timed_out,
            output_truncated=output_budget.truncated,
            cancelled=cancelled,
            cleanup_succeeded=cleanup_succeeded,
        )
        if cleanup_succeeded:
            self._release_completed_handles()
        return result

    def _release_completed_handles(self):
        with self._lifecycle_lock:
            if self._proc is not None:
                with suppress(Exception):
                    import win32file

                    win32file.CloseHandle(self._proc[0])
                self._proc = None
            if self._job is not None:
                with suppress(Exception):
                    import win32file

                    win32file.CloseHandle(self._job)
                self._job = None

    def _exit_code(self):
        import win32process

        with self._lifecycle_lock:
            if self._proc is None:
                return -1
            handle, _ = self._proc
            try:
                return int(win32process.GetExitCodeProcess(handle))
            except Exception:
                return -1

    def terminate(self, grace_seconds: float) -> bool:
        import win32job
        import win32event

        with self._lifecycle_lock:
            if self._job is not None:
                try:
                    win32job.TerminateJobObject(self._job, 1)
                except Exception:
                    return False
            if self._proc is None:
                return True
            handle, _ = self._proc
            timeout_ms = max(0, int(float(grace_seconds) * 1000))
            stopped = win32event.WaitForSingleObject(handle, timeout_ms) == 0
            if stopped and self._job is not None:
                with suppress(Exception):
                    import win32file

                    win32file.CloseHandle(self._job)
                self._job = None
            return stopped


class _PipeReader(threading.Thread):
    """Drain a pipe into memory, keeping at most ``output_max_bytes`` *bytes*.

    Read chunk-by-chunk (not line-by-line) to bound per‑read memory even for
    extremely long lines.
    """

    def __init__(self, stream, output_budget):
        super().__init__(daemon=True)
        self._stream = stream
        self._budget = output_budget
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False

    def run(self):
        with suppress(Exception):
            while True:
                chunk = self._stream.buffer.read(4096) if hasattr(self._stream, "buffer") else self._stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                kept = self._budget.take(chunk)
                if len(kept) != len(chunk):
                    self.truncated = True
                if kept:
                    self._chunks.append(kept)
                    self._size += len(kept)
        with suppress(Exception):
            self._stream.close()

    def text(self):
        return b"".join(self._chunks).decode("utf-8", errors="replace")

    def close(self):
        with suppress(Exception):
            os.close(self._stream.fileno())


class _WinPipeReader(threading.Thread):
    """Drain a Windows pipe handle via ``win32file.ReadFile`` with a byte cap."""

    def __init__(self, handle, output_budget):
        super().__init__(daemon=True)
        self._handle = handle
        self._budget = output_budget
        self._chunks = []
        self._size = 0
        self.truncated = False

    def run(self):
        import win32file

        handle = self._handle
        try:
            while True:
                try:
                    err, data = win32file.ReadFile(handle, 4096)
                except Exception:
                    break
                if err == 0 and data:
                    kept = self._budget.take(data)
                    if len(kept) != len(data):
                        self.truncated = True
                    if kept:
                        self._chunks.append(kept)
                        self._size += len(kept)
                else:
                    # ERROR_BROKEN_PIPE / EOF
                    break
        finally:
            with suppress(Exception):
                win32file.CloseHandle(handle)

    def text(self):
        return b"".join(self._chunks).decode("utf-8", errors="replace")

    def close(self):
        with suppress(Exception):
            import win32file

            win32file.CloseHandle(self._handle)


def _finish_readers(*readers) -> bool:
    deadline = time.monotonic() + 1.0
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    if all(not reader.is_alive() for reader in readers):
        return True
    for reader in readers:
        if reader.is_alive():
            reader.close()
    for reader in readers:
        reader.join(timeout=0.5)
    return all(not reader.is_alive() for reader in readers)

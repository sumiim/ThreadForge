"""Docker sandbox backend for untrusted shell execution.

The backend is fail-closed: it validates the sandbox configuration up front and
refuses to execute if the configuration is unsafe, Docker is unavailable, or
the requested resource limits cannot be expressed. There is no path that falls
back to an unconstrained host shell.

The produced ``DockerShellProcess`` is drop-in compatible with the legacy
``pico.shell_process.ShellProcess`` (``run()``/``is_running()``/``terminate()``
plus the ``ShellResult``-shaped return) so ``tool_run_shell`` can swap backends
without changing its control flow.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class SandboxError(RuntimeError):
    """Base class for sandbox failures. Never caught to fall back to the host."""


class SandboxUnavailableError(SandboxError):
    """Docker is missing or the sandbox backend cannot be initialized."""


class SandboxUnsafeConfigError(SandboxError):
    """The sandbox configuration violates a required safety invariant."""


class SandboxStartError(SandboxError):
    """The container failed to start (image pull / daemon / argument error)."""


class SandboxShellResult:
    """``ShellResult``-compatible result of a sandboxed command."""

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


@dataclass(frozen=True)
class SandboxConfig:
    """Validated sandbox limits. Defaults are conservative and fail-closed."""

    image: str = "threadforge-sandbox:latest"
    user: str = "65534:65534"  # non-root uid:gid
    read_only_rootfs: bool = True
    network: str = "none"  # none | bridge (never host)
    cpu_limit: float = 1.0
    memory_limit: str = "512m"
    pids_limit: int = 64
    tmpfs: str = "/tmp:rw,size=64m"
    cap_drop_all: bool = True
    no_new_privileges: bool = True
    workspace_mount_readonly: bool = False

    def validate(self) -> None:
        user = str(self.user).strip()
        if not user or user in {"0", "root", "0:0", "0:root", "root:0"}:
            raise SandboxUnsafeConfigError("sandbox user must be non-root")
        if not self.read_only_rootfs:
            raise SandboxUnsafeConfigError("sandbox rootfs must be read-only")
        if str(self.network) not in {"none", "bridge"}:
            raise SandboxUnsafeConfigError("sandbox network must be none or bridge")
        if float(self.cpu_limit) <= 0:
            raise SandboxUnsafeConfigError("sandbox CPU limit must be positive")
        if not _validate_memory(self.memory_limit):
            raise SandboxUnsafeConfigError("sandbox memory limit is invalid")
        if int(self.pids_limit) <= 0:
            raise SandboxUnsafeConfigError("sandbox pids limit must be positive")


def _validate_memory(value: str) -> bool:
    text = str(value).strip().lower()
    if not text:
        return False
    if text[-1] not in {"k", "m", "g"}:
        try:
            return int(text) > 0
        except ValueError:
            return False
    try:
        return int(text[:-1]) > 0
    except ValueError:
        return False


class SandboxLifecycle:
    """Records sandbox create/start/complete/fail/cleanup events."""

    def __init__(self, on_event: Callable[[str, dict], None] | None = None):
        self._on_event = on_event
        self._lock = threading.Lock()

    def emit(self, kind: str, **payload: object) -> None:
        if self._on_event is None:
            return
        # Lifecycle observation must never abort the command.
        with suppress(Exception):
            self._on_event(kind, dict(payload))


class _OutputBudget:
    def __init__(self, limit: int):
        self._remaining = max(0, int(limit))
        self.truncated = False

    def take(self, chunk: bytes) -> bytes:
        if len(chunk) > self._remaining:
            kept = chunk[: self._remaining]
            self._remaining = 0
            self.truncated = True
            return kept
        self._remaining -= len(chunk)
        return chunk


class DockerShellProcess:
    """A single sandboxed shell command backed by ``docker run``."""

    def __init__(
        self,
        config: SandboxConfig,
        command: str,
        *,
        cwd: Path,
        env: dict,
        timeout: int,
        output_max_bytes: int,
        cancellation_token=None,
        cleanup_grace_seconds: float = 5.0,
        lifecycle: SandboxLifecycle | None = None,
        container_name: str = "",
    ):
        self.config = config
        self.command = command
        self.timeout = int(timeout)
        self.output_max_bytes = max(0, int(output_max_bytes))
        self.cancellation_token = cancellation_token
        self.cleanup_grace_seconds = max(0.0, float(cleanup_grace_seconds))
        self._cwd = Path(cwd)
        self._env = dict(env)
        self._lifecycle = lifecycle or SandboxLifecycle()
        self.container_name = container_name or ("tf_" + uuid.uuid4().hex[:12])
        self._proc: subprocess.Popen | None = None

    @property
    def pipe_cap(self) -> int:
        return self.output_max_bytes

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _run_args(self) -> list[str]:
        args = [
            "docker", "run", "--rm", "--name", self.container_name,
            "--user", self.config.user,
        ]
        if self.config.read_only_rootfs:
            args.append("--read-only")
        if self.config.cap_drop_all:
            args += ["--cap-drop", "ALL"]
        if self.config.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        args += ["--network", self.config.network]
        args += ["--cpus", str(self.config.cpu_limit)]
        args += ["--memory", self.config.memory_limit]
        args += ["--pids-limit", str(self.config.pids_limit)]
        args += ["--tmpfs", self.config.tmpfs]
        args += ["--workdir", "/workspace"]
        mount_mode = "ro" if self.config.workspace_mount_readonly else "rw"
        args += ["--mount", f"type=bind,src={self._cwd},dst=/workspace,{mount_mode}"]
        for key, value in self._env.items():
            args += ["-e", f"{key}={value}"]
        args += [self.config.image, "/bin/sh", "-c", self.command]
        return args

    def run(self) -> SandboxShellResult:
        args = self._run_args()
        self._lifecycle.emit("sandbox.started", container=self.container_name, image=self.config.image)
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )
        except FileNotFoundError:
            self._lifecycle.emit("sandbox.failed", container=self.container_name, reason="docker_missing")
            return SandboxShellResult(
                -1, "", "sandbox unavailable: docker not found", cleanup_succeeded=True
            )
        except OSError as exc:
            self._lifecycle.emit("sandbox.failed", container=self.container_name, reason="start_failed")
            return SandboxShellResult(
                -1, "", f"sandbox start failed: {exc}", cleanup_succeeded=True
            )

        budget = _OutputBudget(self.output_max_bytes)
        deadline = time.monotonic() + self.timeout
        timed_out = False
        cancelled = False
        cleanup_succeeded = True
        chunks_out: list[bytes] = []
        chunks_err: list[bytes] = []

        def drain(stream, chunks):
            for chunk in iter(lambda: stream.read(4096), b""):
                chunks.append(budget.take(chunk))

        out_thread = threading.Thread(target=drain, args=(self._proc.stdout, chunks_out), daemon=True)
        err_thread = threading.Thread(target=drain, args=(self._proc.stderr, chunks_err), daemon=True)
        out_thread.start()
        err_thread.start()

        while True:
            if self.cancellation_token is not None and self.cancellation_token.is_cancelled():
                cancelled = True
                cleanup_succeeded = self.terminate(self.cleanup_grace_seconds)
                break
            if self._proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                cleanup_succeeded = self.terminate(self.cleanup_grace_seconds)
                break
            time.sleep(0.02)

        out_thread.join(timeout=1.0)
        err_thread.join(timeout=1.0)
        returncode = self._proc.returncode if self._proc.returncode is not None else -1
        result = SandboxShellResult(
            returncode,
            b"".join(chunks_out).decode("utf-8", errors="replace"),
            b"".join(chunks_err).decode("utf-8", errors="replace"),
            timed_out=timed_out,
            output_truncated=budget.truncated,
            cancelled=cancelled,
            cleanup_succeeded=cleanup_succeeded,
        )
        if timed_out:
            self._lifecycle.emit("sandbox.failed", container=self.container_name, reason="timeout")
        elif cancelled:
            self._lifecycle.emit("sandbox.cleaned", container=self.container_name, reason="cancelled")
        elif returncode != 0:
            self._lifecycle.emit("sandbox.completed", container=self.container_name, exit_code=returncode)
        else:
            self._lifecycle.emit("sandbox.completed", container=self.container_name, exit_code=0)
        return result

    def terminate(self, grace_seconds: float) -> bool:
        if self._proc is None:
            return True
        with suppress(Exception):
            subprocess.run(
                ["docker", "kill", self.container_name],
                capture_output=True,
                timeout=max(1.0, float(grace_seconds)),
            )
        try:
            self._proc.wait(timeout=max(1.0, float(grace_seconds)))
            return True
        except subprocess.TimeoutExpired:
            return False


class DockerSandboxBackend:
    """Fail-closed sandbox backend that produces sandboxed shell processes."""

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        lifecycle: SandboxLifecycle | None = None,
        docker_binary: str | None = None,
        probe: bool = False,
    ):
        self.config = config or SandboxConfig()
        self.config.validate()
        self._lifecycle = lifecycle or SandboxLifecycle()
        self._docker = docker_binary or shutil.which("docker")
        if not self._docker:
            raise SandboxUnavailableError("docker is not installed")
        if probe and not self._docker_info_ok():
            raise SandboxUnavailableError("docker daemon is not reachable")

    def _docker_info_ok(self) -> bool:
        try:
            result = subprocess.run(
                [self._docker, "info"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def make_shell(
        self,
        command: str,
        *,
        cwd: Path,
        env: dict,
        timeout: int,
        output_max_bytes: int,
        cancellation_token=None,
        cleanup_grace_seconds: float = 5.0,
    ) -> DockerShellProcess:
        self.config.validate()
        return DockerShellProcess(
            self.config,
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            output_max_bytes=output_max_bytes,
            cancellation_token=cancellation_token,
            cleanup_grace_seconds=cleanup_grace_seconds,
            lifecycle=self._lifecycle,
        )

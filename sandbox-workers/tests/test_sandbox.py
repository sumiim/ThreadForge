"""Docker sandbox backend: fail-closed config, args, and lifecycle."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from threadforge_sandbox import (
    DockerSandboxBackend,
    DockerShellProcess,
    SandboxConfig,
    SandboxLifecycle,
    SandboxUnavailableError,
    SandboxUnsafeConfigError,
)


def test_config_accepts_safe_defaults():
    SandboxConfig().validate()


def test_config_rejects_root_user():
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(user="0").validate()
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(user="root").validate()
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(user="0:0").validate()


def test_config_rejects_disabled_safety_flags():
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(read_only_rootfs=False).validate()
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(network="host").validate()


def test_config_rejects_missing_limits():
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(cpu_limit=0).validate()
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(pids_limit=0).validate()
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(memory_limit="0m").validate()
    with pytest.raises(SandboxUnsafeConfigError):
        SandboxConfig(memory_limit="bogus").validate()


def test_backend_refuses_when_docker_missing(monkeypatch):
    monkeypatch.setattr("threadforge_sandbox.sandbox.shutil.which", lambda _name: None)
    with pytest.raises(SandboxUnavailableError):
        DockerSandboxBackend(SandboxConfig())


def test_docker_run_args_are_fail_closed(tmp_path):
    config = SandboxConfig(
        image="img:1",
        user="1000:1000",
        cpu_limit=0.5,
        memory_limit="256m",
        pids_limit=32,
        network="none",
    )
    shell = DockerShellProcess(
        config,
        "make test",
        cwd=Path(tmp_path),
        env={"PATH": "/usr/bin"},
        timeout=10,
        output_max_bytes=1024,
    )
    args = shell._run_args()
    assert args[0:2] == ["docker", "run"]
    assert "--user" in args and args[args.index("--user") + 1] == "1000:1000"
    assert "--read-only" in args
    assert "--cap-drop" in args and "ALL" in args
    assert "--security-opt" in args and "no-new-privileges" in args
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--cpus" in args and args[args.index("--cpus") + 1] == "0.5"
    assert "--memory" in args and args[args.index("--memory") + 1] == "256m"
    assert "--pids-limit" in args and args[args.index("--pids-limit") + 1] == "32"
    # Only the workspace is mounted, and never as read-only for write tools.
    mounts = [item for item in args if item.startswith("type=bind")]
    assert len(mounts) == 1
    assert f"src={tmp_path}" in mounts[0]
    assert mounts[0].endswith(",rw")


def test_run_fails_closed_when_docker_start_unavailable(monkeypatch, tmp_path):
    events: list[tuple[str, dict]] = []
    lifecycle = SandboxLifecycle(on_event=lambda kind, payload: events.append((kind, payload)))
    config = SandboxConfig()

    def boom(*args, **kwargs):
        raise FileNotFoundError("docker missing at run time")

    monkeypatch.setattr(subprocess, "Popen", boom)
    shell = DockerShellProcess(
        config,
        "echo hi",
        cwd=Path(tmp_path),
        env={},
        timeout=10,
        output_max_bytes=1024,
        lifecycle=lifecycle,
    )
    result = shell.run()
    assert result.returncode != 0
    assert "sandbox unavailable" in result.stderr
    # No silent host fallback: the failure is surfaced, and a lifecycle event
    # records the failure.
    assert [kind for kind, _ in events] == ["sandbox.started", "sandbox.failed"]


def test_timeout_terminates_container_and_records_failure(tmp_path, monkeypatch):
    events: list[tuple[str, dict]] = []
    lifecycle = SandboxLifecycle(on_event=lambda kind, payload: events.append((kind, payload)))
    config = SandboxConfig()

    class _FakeProc:
        returncode = None

        def __init__(self):
            self.stdout = _Stream(b"output")
            self.stderr = _Stream(b"")

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    class _Stream:
        def __init__(self, data):
            self._data = iter([data])

        def read(self, _size=-1):
            return next(self._data, b"")

    import threadforge_sandbox.sandbox as sandbox_mod

    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProc(),
    )
    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "run",
        lambda *args, **kwargs: _FakeProc(),
    )
    monkeypatch.setattr(sandbox_mod.time, "sleep", lambda _s: None)

    shell = DockerShellProcess(
        config,
        "sleep 100",
        cwd=Path(tmp_path),
        env={},
        timeout=0,  # timeout fires immediately
        output_max_bytes=1024,
        lifecycle=lifecycle,
        cancellation_token=None,
    )
    result = shell.run()
    assert result.timed_out
    assert any(kind == "sandbox.failed" and payload.get("reason") == "timeout" for kind, payload in events)


def test_make_shell_returns_sandboxed_process(tmp_path):
    config = SandboxConfig()
    backend = DockerSandboxBackend(config, docker_binary="docker")
    shell = backend.make_shell(
        "echo hi",
        cwd=Path(tmp_path),
        env={},
        timeout=10,
        output_max_bytes=1024,
    )
    assert isinstance(shell, DockerShellProcess)

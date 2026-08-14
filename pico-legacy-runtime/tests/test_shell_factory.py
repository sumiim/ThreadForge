"""The run_shell tool must prefer an injected shell factory (sandbox)."""

from __future__ import annotations

from pathlib import Path

from pico.tool_context import ToolContext
from pico.tools import tool_run_shell


class _Result:
    returncode = 0
    stdout = "sandboxed"
    stderr = ""
    timed_out = False
    output_truncated = False
    cancelled = False
    cleanup_succeeded = True


class _Shell:
    def __init__(self):
        self.result = _Result()
        self.terminated = False

    def run(self):
        return self.result

    def is_running(self):
        return False

    def terminate(self, grace_seconds):
        self.terminated = True
        return True


def _context(tmp_path, factory):
    return ToolContext(
        root=Path(tmp_path),
        path_resolver=lambda p: Path(tmp_path) / str(p),
        shell_env_provider=lambda: {},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "",
        shell_factory=factory,
        shell_output_max_bytes=1024,
        shell_cleanup_grace_seconds=1.0,
    )


def test_run_shell_uses_shell_factory_when_present(tmp_path):
    seen = {}

    def factory(command, *, cwd, env, timeout, output_max_bytes, cancellation_token, cleanup_grace_seconds):
        seen["command"] = command
        seen["cwd"] = cwd
        return _Shell()

    ctx = _context(tmp_path, factory)
    output = tool_run_shell(ctx, {"command": "echo hi", "timeout": 5})
    assert seen["command"] == "echo hi"
    assert seen["cwd"] == Path(tmp_path)
    assert "exit_code: 0" in output
    assert "sandboxed" in output


def test_run_shell_falls_back_to_host_process_without_factory(tmp_path):
    # Legacy CLI path (no factory) still executes the host shell.
    ctx = _context(tmp_path, None)
    output = tool_run_shell(ctx, {"command": "echo hello", "timeout": 5})
    assert "hello" in output

"""Narrow context passed from runtime into tool functions."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    cancellation_token: object | None = None
    shell_output_max_bytes: int = 1048576
    shell_cleanup_grace_seconds: float = 5.0
    shell_factory: Callable | None = None
    _active_shell: object = field(default=None, init=False, repr=False)

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def register_shell(self, shell):
        self._active_shell = shell

    def release_shell(self, shell):
        if self._active_shell is shell:
            self._active_shell = None

    def active_shell(self):
        return self._active_shell

    def terminate_active_shell(self, grace_seconds=5.0):
        shell = self._active_shell
        if shell is None:
            return True
        return bool(shell.terminate(grace_seconds))

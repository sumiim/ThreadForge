import os
import shlex
import subprocess
import sys

import pytest


@pytest.fixture
def python_shell_command():
    """Build a shell command for the platform running the test suite."""

    def build(script):
        argv = [sys.executable, "-c", str(script)]
        if os.name == "nt":
            return subprocess.list2cmdline(argv)
        return shlex.join(argv)

    return build

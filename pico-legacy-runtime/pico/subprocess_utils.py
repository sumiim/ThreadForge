"""Platform-specific subprocess presentation defaults."""

from __future__ import annotations

import subprocess
import sys


def hidden_process_creation_flags() -> int:
    """Suppress transient console windows for GUI-hosted Windows Runtime calls."""
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

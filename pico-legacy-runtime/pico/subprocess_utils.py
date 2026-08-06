"""Platform-specific subprocess presentation defaults."""

from __future__ import annotations

import subprocess
import sys


def hidden_process_creation_flags() -> int:
    """Suppress transient console windows for GUI-hosted Windows Runtime calls."""
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def hidden_process_startupinfo():
    """Request a hidden window for Windows children that still create a console."""
    if sys.platform != "win32":
        return None
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is None:
        return None
    startupinfo = startupinfo_type()
    startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001))
    startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
    return startupinfo

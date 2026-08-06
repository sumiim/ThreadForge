"""Subprocess presentation behavior for GUI-hosted Runtime execution."""

import pico.subprocess_utils as subprocess_utils


def test_hidden_process_creation_flags_are_windows_only(monkeypatch):
    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")
    assert subprocess_utils.hidden_process_creation_flags() == 0

    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    monkeypatch.delattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", raising=False)
    assert subprocess_utils.hidden_process_creation_flags() == 0x08000000


def test_hidden_process_startupinfo_is_platform_safe(monkeypatch):
    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")
    assert subprocess_utils.hidden_process_startupinfo() is None

    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    startupinfo = subprocess_utils.hidden_process_startupinfo()
    if startupinfo is None:
        # Non-Windows test hosts do not expose the Windows startupinfo type.
        return
    assert startupinfo.wShowWindow == getattr(subprocess_utils.subprocess, "SW_HIDE", 0)
    assert startupinfo.dwFlags & getattr(subprocess_utils.subprocess, "STARTF_USESHOWWINDOW", 1)

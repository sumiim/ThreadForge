"""Subprocess presentation behavior for GUI-hosted Runtime execution."""

import pico.subprocess_utils as subprocess_utils


def test_hidden_process_creation_flags_are_windows_only(monkeypatch):
    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")
    assert subprocess_utils.hidden_process_creation_flags() == 0

    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    monkeypatch.delattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", raising=False)
    assert subprocess_utils.hidden_process_creation_flags() == 0x08000000

"""Low-resource per-user Worker service with on-demand native prompts."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auto_update import run_auto_update_loop
from .client import WorkerClient
from .config import ConfigStore


class ServiceAlreadyRunningError(RuntimeError):
    pass


def configure_worker_logging(root: Path) -> None:
    """Keep bounded diagnostics on the Worker machine, never on the server."""
    root.mkdir(parents=True, exist_ok=True)
    log_path = (root / "worker.log").resolve()
    logger = logging.getLogger("threadforge_worker")
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            return
    handler = RotatingFileHandler(
        log_path,
        maxBytes=512 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class ServiceLock:
    def __init__(self, root: Path):
        self.path = root / "service.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                if self.handle.read(1) == b"":
                    self.handle.seek(0)
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ServiceAlreadyRunningError("Worker service is already running") from exc
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


_DIRECTORY_SELECTION_TIMEOUT_SECONDS = 120.0


def select_directory(expires_at: str = "") -> str | None:
    timeout_seconds = _remaining_selection_seconds(expires_at)
    if sys.platform == "win32":
        return _select_directory_windows(timeout_seconds)
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("native_directory_picker_unavailable") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            parent=root,
            mustexist=True,
            title="ThreadForge 请求添加本地工作区",
        )
        return selected or None
    finally:
        root.destroy()


def _select_directory_windows(timeout_seconds: float) -> str | None:
    """Open a folder picker without relying on bundled Tcl/Tk files."""
    import ctypes
    from ctypes import wintypes

    browse_callback = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.LPARAM,
        wintypes.LPARAM,
    )

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwnd_owner", wintypes.HWND),
            ("pidl_root", ctypes.c_void_p),
            ("display_name", wintypes.LPWSTR),
            ("title", wintypes.LPCWSTR),
            ("flags", wintypes.UINT),
            ("callback", browse_callback),
            ("lparam", wintypes.LPARAM),
            ("image", ctypes.c_int),
        ]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    with _ole_apartment(ole32):
        dialog_ready = threading.Event()
        dialog_closed = threading.Event()
        dialog_hwnd = 0

        @browse_callback
        def on_browse_event(hwnd, message, _lparam, _data):
            nonlocal dialog_hwnd
            if message == 1:  # BFFM_INITIALIZED
                dialog_hwnd = int(hwnd)
                _bring_window_to_front(user32, kernel32, hwnd)
                dialog_ready.set()
            return 0

        def close_when_expired() -> None:
            deadline = time.monotonic() + timeout_seconds
            while not dialog_ready.wait(0.05):
                if dialog_closed.is_set():
                    return
            remaining = max(0.0, deadline - time.monotonic())
            if not dialog_closed.wait(remaining) and dialog_hwnd:
                user32.PostMessageW(dialog_hwnd, 0x0010, 0, 0)  # WM_CLOSE

        watchdog = threading.Thread(
            target=close_when_expired,
            name="workspace-picker-timeout",
            daemon=True,
        )
        watchdog.start()
        display_name = ctypes.create_unicode_buffer(260)
        browse_info = BrowseInfo(
            hwnd_owner=0,
            pidl_root=None,
            # ctypes does not implicitly convert a wchar array to LPWSTR on
            # Windows; pass the buffer's pointer explicitly.
            display_name=ctypes.cast(display_name, wintypes.LPWSTR),
            title="ThreadForge 请求添加本地工作区",
            flags=0x0001 | 0x0040,  # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
            callback=on_browse_event,
            lparam=0,
            image=0,
        )
        try:
            item_id_list = shell32.SHBrowseForFolderW(ctypes.byref(browse_info))
        finally:
            dialog_closed.set()
        if not item_id_list:
            return None
        try:
            selected_path = ctypes.create_unicode_buffer(32768)
            if not shell32.SHGetPathFromIDListW(item_id_list, selected_path):
                raise RuntimeError("native_directory_picker_failed")
            return selected_path.value or None
        finally:
            ole32.CoTaskMemFree(item_id_list)


def _bring_window_to_front(user32, kernel32, hwnd) -> None:
    """Present a prompt created by a detached background Worker."""
    foreground = user32.GetForegroundWindow()
    current_thread = int(kernel32.GetCurrentThreadId())
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    )
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Toggle topmost so the prompt is visibly raised without leaving it
        # permanently above every other application.
        flags = 0x0001 | 0x0002 | 0x0040  # NOMOVE | NOSIZE | SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _remaining_selection_seconds(expires_at: str) -> float:
    if not expires_at:
        return _DIRECTORY_SELECTION_TIMEOUT_SECONDS
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise ValueError("selection expiry must include a timezone")
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return _DIRECTORY_SELECTION_TIMEOUT_SECONDS
    return max(1.0, min(_DIRECTORY_SELECTION_TIMEOUT_SECONDS, remaining))


@contextmanager
def _ole_apartment(ole32):
    ole32.OleInitialize.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize.restype = ctypes.c_long
    result = int(ole32.OleInitialize(None))
    if result not in {0, 1}:
        raise RuntimeError("native_directory_picker_failed")
    try:
        yield
    finally:
        ole32.OleUninitialize()


def run_service(data_dir: str | None = None) -> int:
    store = ConfigStore(data_dir)
    configure_worker_logging(store.root)
    store.load_model_env()
    config = store.load()
    if not config.device_id or not config.device_token:
        # The installer starts the service before the browser completes the
        # first pairing link. An unpaired service is therefore an idle state.
        return 0
    try:
        with ServiceLock(store.root):
            client: WorkerClient | None = None

            def auto_update() -> None:
                if client is None:
                    return
                run_auto_update_loop(store, client)

            client = WorkerClient(
                store,
                config,
                workspace_selector=select_directory,
                ready_callback=auto_update,
            )
            client.run_forever()
    except ServiceAlreadyRunningError:
        return 0
    return 0


def start_service_background(data_dir: str | None = None) -> None:
    executable = Path(sys.executable)
    frozen = bool(getattr(sys, "frozen", False))
    if sys.platform == "win32" and not frozen:
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            executable = pythonw
    command = [str(executable)]
    if not frozen:
        command.extend(["-m", "threadforge_worker"])
    if data_dir:
        command.extend(["--data-dir", data_dir])
    command.append("service")
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        if frozen:
            kwargs["env"] = {
                **os.environ,
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            }
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def start_uninstaller() -> None:
    if sys.platform != "win32" or not bool(getattr(sys, "frozen", False)):
        raise RuntimeError("worker_uninstaller_unavailable")
    uninstaller = Path(sys.executable).resolve().with_name("uninstall.exe")
    if not uninstaller.is_file():
        raise RuntimeError("worker_uninstaller_unavailable")
    subprocess.Popen(
        [str(uninstaller)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
    )

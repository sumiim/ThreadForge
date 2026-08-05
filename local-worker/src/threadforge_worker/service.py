"""Low-resource per-user Worker service with on-demand native prompts."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
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


def select_directory() -> str | None:
    if sys.platform == "win32":
        return _select_directory_windows()
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


def _select_directory_windows() -> str | None:
    """Open a folder picker without relying on bundled Tcl/Tk files."""
    import ctypes
    from ctypes import wintypes

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwnd_owner", wintypes.HWND),
            ("pidl_root", ctypes.c_void_p),
            ("display_name", wintypes.LPWSTR),
            ("title", wintypes.LPCWSTR),
            ("flags", wintypes.UINT),
            ("callback", ctypes.c_void_p),
            ("lparam", wintypes.LPARAM),
            ("image", ctypes.c_int),
        ]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    with _ole_apartment(ole32):
        display_name = ctypes.create_unicode_buffer(260)
        browse_info = BrowseInfo(
            hwnd_owner=0,
            pidl_root=None,
            display_name=display_name,
            title="ThreadForge 请求添加本地工作区",
            flags=0x0001 | 0x0040,  # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
            callback=None,
            lparam=0,
            image=0,
        )
        item_id_list = shell32.SHBrowseForFolderW(ctypes.byref(browse_info))
        if not item_id_list:
            return None
        try:
            selected_path = ctypes.create_unicode_buffer(32768)
            if not shell32.SHGetPathFromIDListW(item_id_list, selected_path):
                raise RuntimeError("native_directory_picker_failed")
            return selected_path.value or None
        finally:
            ole32.CoTaskMemFree(item_id_list)


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

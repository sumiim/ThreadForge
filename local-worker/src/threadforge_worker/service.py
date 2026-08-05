"""Low-resource per-user Worker service with on-demand native prompts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .client import WorkerClient
from .config import ConfigStore


class ServiceAlreadyRunningError(RuntimeError):
    pass


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


def run_service(data_dir: str | None = None) -> int:
    store = ConfigStore(data_dir)
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
                while not client.wait_for_stop(0):
                    retry_seconds = 6 * 60 * 60
                    if client.begin_update():
                        try:
                            from .updater import apply_update

                            if apply_update(store):
                                client.stop()
                                return
                        except Exception as exc:
                            print(f"Worker update check skipped: {exc}", file=sys.stderr)
                            retry_seconds = 30 * 60
                        finally:
                            client.end_update()
                    else:
                        retry_seconds = 5 * 60
                    if client.wait_for_stop(retry_seconds):
                        return

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
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)

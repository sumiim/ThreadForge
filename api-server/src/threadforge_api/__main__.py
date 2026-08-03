"""Official launch entry point.

Holds a process-exclusive lock on the data directory for the whole OS-process
lifetime and runs Uvicorn with ``workers=1``. Bypassing this entry point (e.g.
``uvicorn threadforge_api.main:create_app``) is unsupported.
"""

from __future__ import annotations

import sys

from filelock import FileLock, Timeout

from .config import Settings
from .infrastructure.jsonutil import secure_directory
from .infrastructure.workspace_catalog import WorkspaceCatalog


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reload = "--reload" in argv
    settings = Settings().freeze_provider_env()
    # This validation is read-only and must happen before data_dir is created,
    # otherwise a bad config could place backend state inside a Workspace.
    WorkspaceCatalog(settings.workspaces_file, settings.data_dir)
    secure_directory(settings.data_dir)
    lock_path = settings.data_dir / ".threadforge.lock"
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        print("another ThreadForge API process is already running", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"cannot acquire ThreadForge data lock: {exc}", file=sys.stderr)
        return 1
    if sys.platform != "win32":
        try:
            lock_path.chmod(0o600)
        except OSError as exc:
            lock.release()
            print(f"cannot secure ThreadForge data lock: {exc}", file=sys.stderr)
            return 1

    # Push frozen settings into os.environ so the zero-arg create_app()
    # factory (needed for --reload import) picks them up.
    _push_env(settings)

    import uvicorn

    try:
        uvicorn.run(
            "threadforge_api.main:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            workers=1,
            reload=reload,
            log_level=settings.log_level.lower(),
        )
    finally:
        try:
            lock.release()
        except Exception:
            pass
    return 0


def _push_env(settings):
    """Mirror frozen settings into env so that create_app() with no arguments
    reads the same values.  Secret values are never written."""
    import os as _os

    _os.environ.setdefault("THREADFORGE_DATA_DIR", str(settings.data_dir))
    _os.environ.setdefault("THREADFORGE_WORKSPACES_FILE", str(settings.workspaces_file))
    _os.environ.setdefault("THREADFORGE_HOST", settings.host)
    _os.environ.setdefault("THREADFORGE_PORT", str(settings.port))
    _os.environ.setdefault("THREADFORGE_WEB_ORIGIN", settings.web_origin)
    _os.environ.setdefault(
        "THREADFORGE_DESKTOP_ORIGIN_ENABLED",
        str(settings.desktop_origin_enabled).lower(),
    )
    _os.environ.setdefault("THREADFORGE_MAX_STEPS", str(settings.max_steps))
    _os.environ.setdefault("THREADFORGE_MAX_NEW_TOKENS", str(settings.max_new_tokens))
    _os.environ.setdefault("THREADFORGE_MODEL_TIMEOUT_SECONDS", str(settings.model_timeout_seconds))
    _os.environ.setdefault("THREADFORGE_LOG_LEVEL", settings.log_level)
    _os.environ.setdefault("THREADFORGE_OPENAPI_ENABLED", str(settings.openapi_enabled).lower())
    _os.environ.setdefault("THREADFORGE_SHELL_OUTPUT_MAX_BYTES", str(settings.shell_output_max_bytes))


if __name__ == "__main__":
    raise SystemExit(main())

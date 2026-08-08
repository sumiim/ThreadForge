"""Session JSON persistence.

Backward-compatible extensions used by the Web backend:
- `save()` writes atomically (temp file + fsync + `Path.replace()`).
- `list_ids()` returns stable session ids ordered by `updated_at DESC, id DESC`.
- `load()` raises `SessionCorruptedError` for unparsable JSON and
  `SessionNotFoundError` for missing sessions.
"""

import json
import os
import re
import sys
import tempfile
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

# session_id is "ses_" + uuid4().hex (32 hex chars).  Reject anything else.
_SESSION_ID_PATTERN = re.compile(r"^ses_[a-f0-9]{32}$")


class SessionStoreError(RuntimeError):
    pass


class SessionNotFoundError(SessionStoreError):
    pass


class SessionCorruptedError(SessionStoreError):
    pass


class SessionStoreUnavailableError(SessionStoreError):
    pass


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)

    @staticmethod
    def _fsync_directory(path):
        if sys.platform == "win32":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def _write_json_atomic(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            path.parent.chmod(0o700)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(path)
            temp_path = None
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            path.chmod(0o600)
        self._fsync_directory(path.parent)

    def save(self, session):
        path = self.path(session["id"])
        self._write_json_atomic(path, session)
        return path

    def exists(self, session_id):
        return self.path(session_id).is_file()

    def load(self, session_id):
        path = self.path(session_id)
        if not path.is_file():
            raise SessionNotFoundError(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SessionStoreUnavailableError(session_id) from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise SessionCorruptedError(session_id) from exc
        if not isinstance(data, dict):
            raise SessionCorruptedError(session_id)
        return data

    def delete(self, session_id):
        session_id = str(session_id)
        if not _SESSION_ID_PATTERN.fullmatch(session_id):
            raise SessionNotFoundError(session_id)
        path = self.path(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            raise SessionNotFoundError(session_id) from None
        except OSError as exc:
            raise SessionStoreUnavailableError(session_id) from exc
        self._fsync_directory(self.root)

    def list_ids(self):
        """Stable listing ordered by `updated_at DESC, id DESC`."""
        items = []
        for path in self.root.glob("*.json"):
            try:
                updated_at = str(path.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SessionStoreUnavailableError(str(self.root)) from exc
            items.append((updated_at, path.stem))
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [stem for _, stem in items]

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None


class InMemorySessionStore:
    """SessionStore-compatible storage without filesystem side effects."""

    def __init__(self):
        self._sessions = {}
        self._latest_id = None

    @staticmethod
    def path(session_id):
        return Path(".memory-sessions") / f"{session_id}.json"

    def save(self, session):
        session_id = str(session["id"])
        self._sessions[session_id] = deepcopy(session)
        self._latest_id = session_id
        return self.path(session_id)

    def exists(self, session_id):
        return str(session_id) in self._sessions

    def load(self, session_id):
        key = str(session_id)
        if key not in self._sessions:
            raise SessionNotFoundError(session_id)
        return deepcopy(self._sessions[key])

    def delete(self, session_id):
        key = str(session_id)
        if key not in self._sessions:
            raise SessionNotFoundError(session_id)
        del self._sessions[key]
        if self._latest_id == key:
            self._latest_id = next(reversed(self._sessions), None)

    def list_ids(self):
        return list(self._sessions.keys())

    def latest(self):
        return self._latest_id

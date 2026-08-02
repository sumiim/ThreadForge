"""Session JSON persistence."""

from copy import deepcopy
import json
from pathlib import Path


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

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

    def load(self, session_id):
        return deepcopy(self._sessions[str(session_id)])

    def latest(self):
        return self._latest_id

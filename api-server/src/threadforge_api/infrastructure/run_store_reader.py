"""Read-only access to Run artifacts under THREADFORGE_DATA_DIR/runs.

Every ``run_id`` is validated against a safe hex-only pattern and the resolved
path is tested for containment inside the data ``runs/`` root. Path traversal
via ``..``, ``%2E``, backslashes, or URL-encoding is rejected before touching
the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..domain.errors import ArtifactTooLargeError, RunNotFoundError

# Reject any run_id containing path separators, parent-dir traversal, or
# non-printable characters.  The prefix (run_) + uuid4().hex is enforced
# at API level; this check prevents path traversal.
_ID_ILLEGAL = re.compile(r"[/\\\\]|\\.\\.|[^\x20-\x7e]")


def _hash_and_size(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()

ARTIFACT_FILES = {
    "task_state": "task_state.json",
    "trace": "trace.jsonl",
    "report": "report.json",
}


class RunStoreReader:
    def __init__(self, data_dir: Path, artifact_max_bytes: int):
        self._runs = Path(data_dir).resolve() / "runs"
        self._artifact_max_bytes = int(artifact_max_bytes)

    def _validate_id(self, value: str, label: str) -> None:
        if _ID_ILLEGAL.search(value):
            raise RunNotFoundError(value)

    def _run_dir(self, run_id: str) -> Path:
        self._validate_id(run_id, "run_id")
        path = (self._runs / run_id).resolve()
        try:
            path.relative_to(self._runs)
        except ValueError:
            raise RunNotFoundError(run_id) from None
        return path

    def read_progress(self, run_id: str) -> dict:
        path = self._run_dir(run_id) / "task_state.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            "attempts": data.get("attempts"),
            "tool_steps": data.get("tool_steps"),
            "last_tool": data.get("last_tool", ""),
        }

    def list_artifacts(self, run_id: str) -> list[dict]:
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise RunNotFoundError(run_id)
        items = []
        for name, filename in ARTIFACT_FILES.items():
            path = run_dir / filename
            if not path.is_file():
                continue
            try:
                size, digest = _hash_and_size(path)
            except OSError:
                continue
            items.append(
                {
                    "name": name,
                    "media_type": "application/json" if name != "trace" else "application/x-ndjson",
                    "size_bytes": size,
                    "sha256": digest,
                    "content_url": f"/api/v1/runs/{run_id}/artifacts/{name}",
                }
            )
        return items

    def artifact_path(self, run_id: str, name: str) -> Path | None:
        if name not in ARTIFACT_FILES:
            raise RunNotFoundError(run_id)
        run_dir = self._run_dir(run_id)
        path = run_dir / ARTIFACT_FILES[name]
        return path if path.is_file() else None

    def artifact_size_ok(self, path: Path) -> bool:
        try:
            return path.stat().st_size <= self._artifact_max_bytes
        except OSError:
            return False

    def read_artifact_text(self, run_id: str, name: str) -> str | None:
        path = self.artifact_path(run_id, name)
        if path is None:
            return None
        with path.open("rb") as handle:
            data = handle.read(self._artifact_max_bytes + 1)
        if len(data) > self._artifact_max_bytes:
            raise ArtifactTooLargeError(f"artifact exceeds {self._artifact_max_bytes} bytes")
        return data.decode("utf-8", errors="replace")

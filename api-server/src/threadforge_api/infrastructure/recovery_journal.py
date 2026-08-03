"""Durable intent journal for cross-file control-state transitions."""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

from ..domain.entities import utc_now


class RecoveryJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.path.parent.chmod(0o700)
        self._lock = threading.RLock()

    def begin(self, kind: str, *, task_id: str, approval_id: str) -> str:
        transition_id = "txn_" + uuid.uuid4().hex
        self._append(
            {
                "phase": "begin",
                "transition_id": transition_id,
                "kind": kind,
                "task_id": task_id,
                "approval_id": approval_id,
                "created_at": utc_now(),
            }
        )
        return transition_id

    def commit(self, transition_id: str) -> None:
        self._append(
            {
                "phase": "commit",
                "transition_id": transition_id,
                "created_at": utc_now(),
            }
        )

    def incomplete(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with self._lock:
            pending: dict[str, dict] = {}
            lines = self.path.read_bytes().splitlines(keepends=True)
            complete_size = 0
            for index, encoded_line in enumerate(lines):
                is_last = index == len(lines) - 1
                if is_last and not encoded_line.endswith(b"\n"):
                    # A process crash can interrupt the single append before its
                    # newline. Remove that tail before a future append can join
                    # onto it and turn it into a permanent corrupt record.
                    self._truncate(complete_size)
                    break
                complete_size += len(encoded_line)
                if not encoded_line.strip():
                    continue
                try:
                    record = json.loads(encoded_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("corrupted recovery journal record") from exc
                if not isinstance(record, dict):
                    raise TypeError("recovery journal record must be an object")
                transition_id = record.get("transition_id")
                if not isinstance(transition_id, str) or not transition_id:
                    raise ValueError("recovery record has no transition_id")
                if record.get("phase") == "begin":
                    pending[transition_id] = record
                elif record.get("phase") == "commit":
                    pending.pop(transition_id, None)
                else:
                    raise ValueError("invalid recovery record phase")
            return list(pending.values())

    def _truncate(self, size: int) -> None:
        with self.path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
        if sys.platform != "win32":
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _append(self, record: dict) -> None:
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if sys.platform != "win32":
                self.path.chmod(0o600)
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

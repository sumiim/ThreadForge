"""Run artifact query service — all output is redacted before it leaves this layer."""

from __future__ import annotations

import json

from pico.security import redact_artifact

from ..infrastructure.json_repositories import JsonTaskRepository
from ..infrastructure.run_store_reader import RunStoreReader


class ArtifactService:
    def __init__(self, run_store_reader: RunStoreReader, task_repo: JsonTaskRepository):
        self._reader = run_store_reader
        self._task_repo = task_repo

    def list_artifacts(self, run_id: str, owner_id: str) -> list[dict]:
        self._task_repo.get_by_run_for_owner(run_id, owner_id)
        return self._reader.list_artifacts(run_id)

    def get_artifact(self, run_id: str, name: str, owner_id: str) -> dict:
        self._task_repo.get_by_run_for_owner(run_id, owner_id)
        try:
            text = self._reader.read_artifact_text(run_id, name)
        except OSError:
            return {"name": name, "found": False}
        if text is None:
            return {"name": name, "found": False}
        if name in ("task_state", "report"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return {"name": name, "found": True, "text": text}
            redacted = redact_artifact(data)
            return {"name": name, "found": True, "text": json.dumps(redacted, ensure_ascii=False)}
        # trace is line-oriented NDJSON — redact each line
        redacted_lines = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                    # append_trace may be interrupted before its final newline;
                    # do not expose a malformed NDJSON tail to clients.
                    continue
                redacted_lines.append(redact_artifact(line))
                continue
            redacted_lines.append(json.dumps(redact_artifact(event), ensure_ascii=False))
        return {"name": name, "found": True, "text": "\n".join(redacted_lines)}

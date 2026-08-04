"""Persistent identity for a single-owner V1.5 data directory."""

from __future__ import annotations

import uuid
from pathlib import Path
from uuid import UUID

from ..domain.identity import canonical_owner_id
from .jsonutil import read_json, write_json_atomic

OWNER_FILE_NAME = "instance-owner.json"


def resolve_instance_owner(data_dir: Path, configured: UUID | None) -> str:
    """Load or create the stable owner UUID for one data directory."""
    path = Path(data_dir) / OWNER_FILE_NAME
    configured_id = canonical_owner_id(configured) if configured is not None else None
    if path.exists():
        persisted_id = canonical_owner_id(read_json(path)["owner_id"])
        if configured_id is not None and configured_id != persisted_id:
            raise ValueError("configured instance owner does not match persisted owner")
        return persisted_id

    owner_id = configured_id or str(uuid.uuid4())
    write_json_atomic(path, {"schema_version": 1, "owner_id": owner_id})
    return owner_id

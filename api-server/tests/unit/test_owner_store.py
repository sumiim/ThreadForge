"""Stable single-owner identity persistence."""

from __future__ import annotations

from uuid import UUID

import pytest

from threadforge_api.infrastructure.owner_store import resolve_instance_owner


def test_generated_owner_is_stable_for_data_directory(tmp_path):
    first = resolve_instance_owner(tmp_path, None)
    second = resolve_instance_owner(tmp_path, None)
    assert UUID(first)
    assert second == first


def test_configured_owner_must_match_persisted_owner(tmp_path):
    first = UUID("11111111-1111-4111-8111-111111111111")
    other = UUID("22222222-2222-4222-8222-222222222222")
    assert resolve_instance_owner(tmp_path, first) == str(first)
    with pytest.raises(ValueError, match="does not match"):
        resolve_instance_owner(tmp_path, other)

"""Stable machine fingerprint generation (device_id 加固)."""

from __future__ import annotations

import hashlib
import re

from threadforge_worker.machine import (
    _fallback_fingerprint,
    _read_machine_id_file,
    _read_windows_machine_guid,
    machine_fingerprint,
)


def test_fingerprint_has_shape_and_is_stable():
    fp1 = machine_fingerprint()
    fp2 = machine_fingerprint()
    assert re.fullmatch(r"fp_[0-9a-f]{32}", fp1)
    assert fp1 == fp2


def test_fingerprint_is_derived_from_os_identity():
    # 指纹应来自系统级标识（Windows MachineGuid / machine-id），而非随机。
    raw = _read_windows_machine_guid() or _read_machine_id_file() or _fallback_fingerprint()
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    assert machine_fingerprint() == f"fp_{expected}"


def test_fallback_uses_node_and_hostname():
    fallback = _fallback_fingerprint()
    assert "-" in fallback

"""Device credentials, pairing codes and per-owner isolation."""

from __future__ import annotations

import json

import pytest

from threadforge_api.domain.errors import (
    AuthorizationDeniedError,
    DeviceNotFoundError,
    PairingCodeInvalidError,
    RenameConflictError,
)
from threadforge_api.infrastructure.device_store import (
    DeviceStore,
    PairingCodeStore,
    WorkerWorkspace,
)

OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"


def test_device_token_is_hashed_and_authenticates(tmp_path):
    store = DeviceStore(tmp_path)
    device, token = store.create(OWNER_A, "Laptop")

    payload = json.loads((tmp_path / f"{device.device_id}.json").read_text(encoding="utf-8"))
    assert token not in json.dumps(payload)
    assert payload["token_digest"] != token
    assert store.authenticate(token).device_id == device.device_id
    with pytest.raises(AuthorizationDeniedError):
        store.authenticate(token + "forged")


def test_device_owner_scope_and_identifier_validation(tmp_path):
    store = DeviceStore(tmp_path)
    device, _ = store.create(OWNER_A, "Laptop")

    with pytest.raises(DeviceNotFoundError):
        store.get_for_owner(device.device_id, OWNER_B)
    with pytest.raises(DeviceNotFoundError):
        store.get("../../outside")
    with pytest.raises(DeviceNotFoundError):
        store.revoke(device.device_id, OWNER_B)


def test_pairing_code_is_single_use_and_has_64_bits():
    store = PairingCodeStore(ttl_seconds=600)
    code, ttl = store.create(OWNER_A)

    assert ttl == 600
    assert len(code.split("-")) == 4
    assert store.consume(code.lower()) == OWNER_A
    with pytest.raises(PairingCodeInvalidError):
        store.consume(code)


def test_expired_pairing_code_is_rejected(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("threadforge_api.infrastructure.device_store.time.time", lambda: now[0])
    store = PairingCodeStore(ttl_seconds=60)
    code, _ = store.create(OWNER_A)
    now[0] += 61

    with pytest.raises(PairingCodeInvalidError):
        store.consume(code)


def test_worker_update_status_is_persisted(tmp_path):
    store = DeviceStore(tmp_path)
    device, _ = store.create(OWNER_A, "Laptop")
    update_status = {
        "status": "downloading",
        "current_version": "0.3.0",
        "target_version": "0.3.1",
        "downloaded_bytes": 10,
        "total_bytes": 100,
        "error": "",
        "updated_at": "2026-08-07T12:00:00+00:00",
    }

    store.update_worker_status(device.device_id, update_status)

    assert DeviceStore(tmp_path).get(device.device_id).update_status == update_status


def test_display_name_version_is_independent_from_worker_presence(tmp_path):
    store = DeviceStore(tmp_path)
    device, _ = store.create(OWNER_A, "Laptop")
    workspace = WorkerWorkspace("ws_" + "a" * 32, "Original", True)
    store.update_presence(
        device.device_id,
        model="model",
        model_configured=True,
        version="0.3.2",
        protocol_version=1,
        platform="windows",
        architecture="x86_64",
        capabilities=[],
        workspaces=[workspace],
    )
    current = store.get(device.device_id)
    renamed_at = current.workspaces[0].display_name_updated_at
    store.set_workspace_display_name(device.device_id, OWNER_A, workspace.workspace_id, "My repo")

    store.update_presence(
        device.device_id,
        model="model",
        model_configured=True,
        version="0.3.2",
        protocol_version=1,
        platform="windows",
        architecture="x86_64",
        capabilities=[],
        workspaces=[WorkerWorkspace(workspace.workspace_id, "Original", True)],
    )
    refreshed = store.get(device.device_id)
    assert refreshed.workspaces[0].name == "My repo"
    assert refreshed.workspaces[0].display_name_source == "user"
    assert refreshed.workspaces[0].display_name_updated_at != renamed_at


def test_device_rename_uses_optimistic_display_name_version(tmp_path):
    store = DeviceStore(tmp_path)
    device, _ = store.create(OWNER_A, "Laptop")
    version = device.display_name_updated_at
    store.rename(device.device_id, OWNER_A, "Desk", expected_updated_at=version)

    with pytest.raises(RenameConflictError):
        store.rename(device.device_id, OWNER_A, "Other", expected_updated_at=version)

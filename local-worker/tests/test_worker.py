"""Local Worker configuration, approval and offline Runtime tests."""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest
from pico.approval import ApprovalOutcome, ApprovalRequest
from pico.features.memory import default_memory_state
from pico.providers.clients import FakeModelClient

from threadforge_worker.client import (
    _stable_failure_reason,
    _validated_server_url,
    _websocket_url,
)
from threadforge_worker.config import ConfigStore, WorkerConfig
from threadforge_worker.runtime import (
    ActiveRun,
    CancellationToken,
    RemoteApprovalStrategy,
    run_task,
)


def test_config_roundtrip_does_not_store_plaintext_token(tmp_path):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="super-secret")
    store.save(config)

    raw = store.path.read_text(encoding="utf-8")
    if sys.platform == "win32":
        assert "super-secret" not in raw
        assert "dpapi:" in raw
    else:
        assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.load().device_token == "super-secret"


def test_workspace_registration_uses_canonical_directory(tmp_path):
    store = ConfigStore(tmp_path / "state")
    config = WorkerConfig()
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    workspace = store.add_workspace(config, str(workspace_root))

    assert workspace.workspace_id.startswith("ws_")
    assert workspace.path == str(workspace_root.resolve())
    assert store.add_workspace(config, str(workspace_root)).workspace_id == workspace.workspace_id
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        store.add_workspace(config, str(file_path))


def test_websocket_url_and_stable_failure_codes():
    assert _websocket_url("https://example.test/base/") == "wss://example.test/base"
    assert _websocket_url("http://127.0.0.1:18000") == "ws://127.0.0.1:18000"
    with pytest.raises(ValueError, match="must use HTTPS"):
        _validated_server_url("http://example.test")
    assert _stable_failure_reason(KeyError("task")) == "invalid_task_payload"
    assert _stable_failure_reason(RuntimeError("PICO_OPENAI_API_KEY is not configured")) == "model_not_configured"
    assert _stable_failure_reason(OSError("private local path")) == "worker_runtime_error"


def test_remote_approval_blocks_until_exact_decision():
    sent = []
    token = CancellationToken()
    strategy = RemoteApprovalStrategy(sent.append, "task_1", token)
    result = []

    thread = threading.Thread(
        target=lambda: result.append(
            strategy.decide(ApprovalRequest("run_shell", {"command": "echo ok"}, "call_1"))
        )
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not sent and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sent[0]["type"] == "approval.requested"
    with pytest.raises(RuntimeError, match="does not match"):
        strategy.resolve("another_call", "approved", sent[0]["args_digest"])
    with pytest.raises(RuntimeError, match="does not match"):
        strategy.resolve("call_1", "approved", "forged")
    time.sleep(0.02)
    assert thread.is_alive()
    strategy.resolve("call_1", "approved", sent[0]["args_digest"])
    thread.join(timeout=1)
    assert result == [ApprovalOutcome.APPROVED]


def test_runtime_completes_with_fake_model_without_provider_call(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = {
        "task_id": "task_" + "a" * 32,
        "run_id": "run_" + "b" * 32,
        "workspace_id": "ws_" + "c" * 32,
        "input": "say done",
        "max_steps": 2,
        "settings": {"max_new_tokens": 128, "model_timeout_seconds": 1},
        "session": {
            "id": "ses_" + "d" * 32,
            "owner_id": "11111111-1111-4111-8111-111111111111",
            "workspace_id": "ws_" + "c" * 32,
            "workspace_root": "worker://placeholder",
            "title": "test",
            "history": [],
            "memory": default_memory_state(),
            "checkpoints": {},
        },
    }
    sent = []
    token = CancellationToken()
    approval = RemoteApprovalStrategy(sent.append, task["task_id"], token)
    active = ActiveRun(task["task_id"], token, approval)

    run_task(
        task=task,
        workspace_path=workspace,
        data_dir=tmp_path / "worker-state",
        send=sent.append,
        active=active,
        model_client_factory=lambda: FakeModelClient(["<final>done</final>"]),
    )

    terminal = sent[-1]
    assert terminal["type"] == "terminal"
    assert terminal["status"] == "completed"
    assert terminal["final_answer"] == "done"
    assert json.dumps(terminal["session"])

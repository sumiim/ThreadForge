"""Local Worker configuration, approval and offline Runtime tests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from unittest.mock import patch

import pytest
from pico.approval import ApprovalOutcome, ApprovalRequest, AutoApprovalStrategy
from pico.execution_hooks import RunCancelled
from pico.features.memory import default_memory_state
from pico.providers.clients import FakeModelClient
from pico.session_store import SessionStore
from pico.tool_executor import ToolExecutionResult
from websockets.datastructures import Headers
from websockets.exceptions import InvalidMessage, InvalidStatus
from websockets.http11 import Response

import threadforge_worker.config as config_module
import threadforge_worker.service as service_module
from threadforge_worker import __version__
from threadforge_worker.auto_update import run_auto_update_loop
from threadforge_worker.cli import _parse_protocol_uri
from threadforge_worker.cli import main as worker_main
from threadforge_worker.client import (
    WorkerClient,
    WorkerProtocolRejectedError,
    _capability_models_from_provider,
    _extract_model_ids,
    _list_provider_models,
    _model_capabilities,
    _stable_failure_reason,
    _timestamp_expired,
    _validated_server_url,
    _websocket_url,
)
from threadforge_worker.config import (
    ConfigStore,
    LocalWorkspace,
    WorkerConfig,
    WorkspaceConfigWriteError,
    WorkspacePathError,
)
from threadforge_worker.runtime import (
    ActiveRun,
    CancellableModelClient,
    CancellationToken,
    ModelProviderFactory,
    ProviderNotConfiguredError,
    RemoteApprovalStrategy,
    RemoteExecutionHooks,
    _sandbox_shell_factory,
    _supported_reasoning_efforts,
    run_task,
)
from threadforge_worker.service import (
    ServiceAlreadyRunningError,
    ServiceLock,
    _bring_window_to_front,
    _remaining_selection_seconds,
    run_service,
    select_directory,
    start_service_background,
    start_uninstaller,
)


def test_protocol_links_are_strict_and_support_automatic_pairing():
    action, parameters = _parse_protocol_uri(
        "threadforge://worker/pair?server=https%3A%2F%2Fthreadforge.example&code=ABCD-1234-EF56-7890"
    )
    assert action == "pair"
    assert parameters == {
        "server": "https://threadforge.example",
        "code": "ABCD-1234-EF56-7890",
    }
    assert _parse_protocol_uri("threadforge://worker/start") == ("start", {})
    assert _parse_protocol_uri("threadforge://worker/uninstall") == ("uninstall", {})

    invalid_links = [
        "threadforge://worker/run?command=whoami",
        "threadforge://worker/start?path=C%3A%5CUsers",
        "threadforge://worker/uninstall?keep_data=false",
        "threadforge://worker/pair?server=https%3A%2F%2Fthreadforge.example&code=bad",
        "threadforge://worker/pair?server=https%3A%2F%2Fthreadforge.example&server=https%3A%2F%2Fevil.example&code=ABCD-1234-EF56-7890",
        "threadforge://other/start",
    ]
    for link in invalid_links:
        with pytest.raises(ValueError):
            _parse_protocol_uri(link)


def test_uninstall_protocol_does_not_require_readable_worker_config(monkeypatch):
    calls = []
    monkeypatch.setattr(
        config_module.ConfigStore,
        "load",
        lambda _store: (_ for _ in ()).throw(AssertionError("config must not be loaded")),
    )
    monkeypatch.setattr(service_module, "start_uninstaller", lambda: calls.append("uninstall"))

    assert worker_main(["protocol", "threadforge://worker/uninstall"]) == 0
    assert calls == ["uninstall"]


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
    assert store.remove_workspace(config, workspace.workspace_id) is True
    assert config.workspaces == []
    assert store.remove_workspace(config, workspace.workspace_id) is False


def test_workspace_registration_persists_in_separate_state_file(tmp_path):
    store = ConfigStore(tmp_path / "state")
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    store.save(config)
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()

    workspace = store.add_workspace(config, str(workspace_root))

    assert store.workspaces_path.is_file()
    assert workspace.workspace_id in store.workspaces_path.read_text(encoding="utf-8")
    restored = store.load()
    assert [item.workspace_id for item in restored.workspaces] == [workspace.workspace_id]
    assert restored.device_id == config.device_id


def test_workspace_load_prefers_separate_state_file(tmp_path):
    store = ConfigStore(tmp_path / "state")
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    legacy = WorkerConfig(
        device_id="dev_" + "a" * 32,
        device_token="token",
        workspaces=[LocalWorkspace("ws_" + "a" * 32, "legacy", str(workspace_root))],
    )
    store.save(legacy)
    current = WorkerConfig(
        device_id=legacy.device_id,
        device_token=legacy.device_token,
        workspaces=[LocalWorkspace("ws_" + "b" * 32, "current", str(workspace_root))],
    )
    store.save_workspaces(current)

    restored = store.load()
    assert [item.name for item in restored.workspaces] == ["current"]


def test_config_save_retries_a_transient_replace_lock(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "state")
    original_replace = config_module.Path.replace
    attempts = []

    def replace(source, target):
        if config_module.Path(target) == store.path:
            attempts.append(target)
            if len(attempts) < 3:
                raise PermissionError("temporarily locked")
        return original_replace(source, target)

    monkeypatch.setattr(config_module.Path, "replace", replace)
    monkeypatch.setattr(config_module.time, "sleep", lambda _delay: None)

    store.save(WorkerConfig())

    assert len(attempts) == 3
    assert store.path.is_file()


def test_workspace_registration_rolls_back_after_config_write_failure(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "state")
    config = WorkerConfig()
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    monkeypatch.setattr(store, "save_workspaces", lambda _config: (_ for _ in ()).throw(PermissionError()))

    with pytest.raises(WorkspaceConfigWriteError):
        store.add_workspace(config, str(workspace_root))

    assert config.workspaces == []


def test_workspace_registration_rejects_an_unavailable_path(tmp_path):
    store = ConfigStore(tmp_path / "state")

    with pytest.raises(WorkspacePathError):
        store.add_workspace(WorkerConfig(), str(tmp_path / "missing"))


def test_model_configuration_is_written_to_worker_env_and_loaded(tmp_path):
    store = ConfigStore(tmp_path / "state")
    with patch.dict("os.environ", {}, clear=True):
        store.save_model_env(
            base_url="https://provider.example/v1",
            api_key="local-secret",
            model="model-a",
        )
        assert store.load_model_env()["PICO_OPENAI_MODEL"] == "model-a"
        assert "local-secret" in store.model_env_path.read_text(encoding="utf-8")
        assert __import__("os").environ["PICO_OPENAI_API_KEY"] == "local-secret"
    if sys.platform != "win32":
        assert store.model_env_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="single line|invalid"):
        store.save_model_env(
            base_url="https://provider.example/v1",
            api_key="bad\nvalue",
            model="model-a",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        store.save_model_env(
            base_url="http://provider.example/v1",
            api_key="secret",
            model="model-a",
        )


def test_reasoning_capabilities_follow_model_family_or_explicit_override():
    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://provider.example/v1",
            "PICO_OPENAI_MODEL": "gpt-5.5",
        },
        clear=True,
    ):
        assert _supported_reasoning_efforts() == (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        )

    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://provider.example/v1",
            "PICO_OPENAI_MODEL": "model-a",
        },
        clear=True,
    ):
        assert _supported_reasoning_efforts() == ("none",)

    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://provider.example/v1",
            "PICO_OPENAI_MODEL": "model-a",
            "PICO_REASONING_EFFORTS": "low,high,low",
        },
        clear=True,
    ):
        assert _supported_reasoning_efforts() == ("low", "high")


def test_provider_factory_resolves_env_fallback_without_provider(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://provider.example/v1",
            "PICO_OPENAI_MODEL": "gpt-5.5",
        },
        clear=True,
    ):
        factory = ModelProviderFactory(data_dir=tmp_path / "state", settings={})
        profile = factory.resolve()
        assert profile["provider_id"] == ""
        assert profile["model"] == "gpt-5.5"
        assert profile["base_url"] == ""  # env 延迟解析
        assert "high" in profile["supported_reasoning_efforts"]


def test_provider_factory_rejects_unsupported_reasoning_effort(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://provider.example/v1",
            "PICO_OPENAI_MODEL": "model-a",  # 只有 none
        },
        clear=True,
    ):
        factory = ModelProviderFactory(
            data_dir=tmp_path / "state",
            settings={"reasoning_effort": "high"},
        )
        with pytest.raises(RuntimeError, match="not supported"):
            factory.resolve()


def test_provider_factory_uses_provider_cfg_when_present(tmp_path):
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_test",
        base_url="https://api.deepseek.com",
        api_key="sk-local",
        model="deepseek-v4-flash",
        protocol="deepseek",
        reasoning_efforts=("none", "low", "high"),
    )
    factory = ModelProviderFactory(
        data_dir=tmp_path / "state",
        settings={"provider_id": "prv_test", "reasoning_effort": "high"},
    )
    profile = factory.resolve()
    assert profile["provider_id"] == "prv_test"
    assert profile["model"] == "deepseek-v4-flash"
    assert profile["model_provider"] == "chat_completions"
    assert profile["base_url"] == "https://api.deepseek.com"
    assert profile["api_key"] == "sk-local"
    assert profile["reasoning_effort"] == "high"


def test_provider_factory_refuses_silent_env_fallback_when_provider_configured(tmp_path):
    """本机已配置 Provider 但任务未携带可用的 provider_id：禁止回退旧 .env。"""
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_local",
        base_url="https://api.deepseek.com",
        api_key="sk-local",
        model="deepseek-v4-flash",
        protocol="deepseek",
        reasoning_efforts=("none", "high"),
    )
    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://api.deepseek.com",
            "PICO_OPENAI_MODEL": "deepseek-v4-flash",
        },
        clear=True,
    ):
        # 任务没带 provider_id
        factory = ModelProviderFactory(data_dir=tmp_path / "state", settings={})
        with pytest.raises(ProviderNotConfiguredError, match="configure it for this device") as missing:
            factory.resolve()
        assert missing.value.code == "provider_not_configured"
        # 任务带了本机不存在的 provider_id
        factory = ModelProviderFactory(
            data_dir=tmp_path / "state",
            settings={"provider_id": "prv_unknown"},
        )
        with pytest.raises(ProviderNotConfiguredError, match="prv_unknown"):
            factory.resolve()

        assert _stable_failure_reason(ProviderNotConfiguredError("prv_unknown")) == (
            "provider_not_configured"
        )


def test_save_provider_allows_empty_model(tmp_path):
    """§2.2/供应商管理：模型尚未发现时 save_provider 应允许空 model，仅拒超长/换行。"""
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_empty",
        base_url="https://codex.ximuai.com/v1",
        api_key="sk-local",
        model="",
        protocol="openai_compatible",
        reasoning_efforts=("none", "high"),
    )
    loaded = store.load_provider("prv_empty")
    assert loaded is not None
    assert loaded["model"] == ""
    assert loaded["base_url"] == "https://codex.ximuai.com/v1"


def test_save_provider_rejects_newline_in_model(tmp_path):
    """模型仍不允许换行/超长（即使允许空）。"""
    store = ConfigStore(tmp_path / "state")
    with pytest.raises(ValueError, match="invalid model"):
        store.save_provider(
            "prv_bad",
            base_url="https://api.deepseek.com",
            api_key="sk-local",
            model="a\nb",
            protocol="deepseek",
            reasoning_efforts=("none",),
        )


def test_save_provider_allows_empty_api_key_and_base_url(tmp_path):
    """§2.2/供应商管理：编辑时 api_key/base_url 留空 = 沿用本地 saved 值；
    新建未填 key 也是合法状态。save_provider 不应因空 api_key/base_url 抛错。"""
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_nokey",
        base_url="",
        api_key="",
        model="",
        protocol="openai_compatible",
        reasoning_efforts=("none", "high"),
    )
    loaded = store.load_provider("prv_nokey")
    assert loaded is not None
    assert loaded["api_key"] == ""
    assert loaded["base_url"] == ""
    assert loaded["model"] == ""


def test_provider_factory_still_allows_env_fallback_without_local_providers(tmp_path):
    """本机从未配置 Provider 的纯 env 旧模式仍然允许回退。"""
    with patch.dict(
        "os.environ",
        {
            "PICO_OPENAI_API_BASE": "https://provider.example/v1",
            "PICO_OPENAI_MODEL": "gpt-5.5",
        },
        clear=True,
    ):
        factory = ModelProviderFactory(data_dir=tmp_path / "state", settings={})
        profile = factory.resolve()
        assert profile["provider_id"] == ""
        assert profile["model"] == "gpt-5.5"


def test_provider_factory_create_clients_uses_injected_factory(tmp_path):
    from pico.providers.clients import FakeModelClient

    captured = []

    def injected():
        captured.append(1)
        return FakeModelClient(["ok"])

    factory = ModelProviderFactory(
        data_dir=tmp_path / "state",
        settings={},
        model_client_factory=injected,
    )
    main_client, router_client = factory.create_clients(
        temperature=0.2, timeout=30, max_attempts=3
    )
    assert main_client is router_client
    assert captured == [1]  # factory 只调用一次


def test_provider_factory_review_client_only_when_configured(tmp_path):
    """§review 双 provider（2026-09-03）：未配 review_provider_id → None（回退主循环）；
    配置了且本机存在该 provider → 构建独立 review client（review_model_id 覆盖默认模型）。
    """
    # 未配置 review_provider_id → review client None。
    factory = ModelProviderFactory(
        data_dir=tmp_path / "state",
        settings={},
        model_client_factory=None,
    )
    assert factory.create_review_client(temperature=0.2, timeout=30, max_attempts=3) is None
    assert factory.resolve_review() is None

    # 配置 review_provider_id + 本机存在该 provider → 独立 review client。
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_review",
        base_url="https://review.example/v1",
        api_key="test-key",
        model="review-model",
        protocol="openai_compatible",
    )
    factory2 = ModelProviderFactory(
        data_dir=tmp_path / "state",
        settings={"review_provider_id": "prv_review", "review_model_id": "special-review-model"},
        model_client_factory=None,
    )
    profile = factory2.resolve_review()
    assert profile is not None
    assert profile["provider_id"] == "prv_review"
    # review_model_id 覆盖 provider 默认模型
    assert profile["model"] == "special-review-model"
    rev_client = factory2.create_review_client(temperature=0.2, timeout=30, max_attempts=3)
    assert rev_client is not None
    assert getattr(rev_client, "model", None) == "special-review-model"


def test_service_lock_prevents_duplicate_worker_processes(tmp_path):
    outer = ServiceLock(tmp_path)
    inner = ServiceLock(tmp_path)
    with outer, pytest.raises(ServiceAlreadyRunningError), inner:
        pass


def test_service_exits_cleanly_before_first_pairing(tmp_path):
    assert run_service(str(tmp_path)) == 0


def test_service_logs_unexpected_failures_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service_module.ConfigStore,
        "load",
        lambda _store: (_ for _ in ()).throw(RuntimeError("broken local config")),
    )

    assert run_service(str(tmp_path)) == 1
    assert "broken local config" in (tmp_path / "worker.log").read_text(encoding="utf-8")


def test_worker_retries_transient_websocket_handshake_failure(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    statuses = []
    client = WorkerClient(store, config, status_callback=statuses.append)
    attempts = []

    def run_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise InvalidStatus(Response(502, "Bad Gateway", Headers()))
        client.stop()

    monkeypatch.setattr(client, "_run_once", run_once)
    monkeypatch.setattr(client._stop_event, "wait", lambda _timeout: False)

    client.run_forever()

    assert len(attempts) == 2
    assert statuses == ["connecting", "retrying", "connecting", "stopped"]


def test_worker_retries_connection_closed_before_http_response(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    statuses = []
    client = WorkerClient(store, config, status_callback=statuses.append)
    attempts = []

    def run_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise InvalidMessage("connection closed while reading HTTP status line")
        client.stop()

    monkeypatch.setattr(client, "_run_once", run_once)
    monkeypatch.setattr(client._stop_event, "wait", lambda _timeout: False)

    client.run_forever()

    assert len(attempts) == 2
    assert statuses == ["connecting", "retrying", "connecting", "stopped"]


def test_worker_retries_unexpected_transport_exception(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    statuses = []
    client = WorkerClient(store, config, status_callback=statuses.append)
    attempts = []

    def run_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("timed out while closing connection")
        client.stop()

    monkeypatch.setattr(client, "_run_once", run_once)
    monkeypatch.setattr(client._stop_event, "wait", lambda _timeout: False)

    client.run_forever()

    assert len(attempts) == 2
    assert statuses == ["connecting", "retrying", "connecting", "stopped"]


def test_worker_rejects_permanent_websocket_handshake_failure(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    statuses = []
    client = WorkerClient(store, config, status_callback=statuses.append)
    monkeypatch.setattr(
        client,
        "_run_once",
        lambda: (_ for _ in ()).throw(
            InvalidStatus(Response(401, "Unauthorized", Headers()))
        ),
    )

    client.run_forever()

    assert statuses == ["connecting", "rejected"]


def test_worker_reports_protocol_detail_and_stops_without_raising(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    statuses = []
    client = WorkerClient(store, config, status_callback=statuses.append)

    with pytest.raises(
        WorkerProtocolRejectedError,
        match="local session identity conflicts",
    ):
        client._handle(
            {
                "type": "protocol.error",
                "code": "worker_protocol_error",
                "message": "local session identity conflicts with control-plane data",
            }
        )

    monkeypatch.setattr(
        client,
        "_run_once",
        lambda: (_ for _ in ()).throw(
            WorkerProtocolRejectedError(
                "worker_protocol_error",
                "local session identity conflicts with control-plane data",
            )
        ),
    )
    client.run_forever()

    assert statuses == ["connecting", "rejected"]


def test_frozen_windows_service_uses_a_fresh_pyinstaller_environment(monkeypatch):
    popen_calls = []

    monkeypatch.setattr(service_module.sys, "platform", "win32")
    monkeypatch.setattr(service_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_module.sys, "executable", r"C:\ThreadForge\worker-service.exe")
    monkeypatch.setattr(service_module.subprocess, "CREATE_NO_WINDOW", 1, raising=False)
    monkeypatch.setattr(service_module.subprocess, "DETACHED_PROCESS", 2, raising=False)
    monkeypatch.setattr(
        service_module.subprocess,
        "Popen",
        lambda command, **kwargs: popen_calls.append((command, kwargs)),
    )
    monkeypatch.setenv("_MEIPASS2", r"C:\Users\test\AppData\Local\Temp\_MEIparent")

    start_service_background()

    assert popen_calls[0][0] == [r"C:\ThreadForge\worker-service.exe", "service"]
    assert popen_calls[0][1]["creationflags"] == 3
    assert popen_calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert popen_calls[0][1]["env"]["_MEIPASS2"].endswith("_MEIparent")


def test_frozen_windows_worker_launches_sibling_uninstaller(monkeypatch, tmp_path):
    popen_calls = []
    executable = tmp_path / "threadforge-worker-service.exe"
    executable.write_bytes(b"worker")
    uninstaller = tmp_path / "uninstall.exe"
    uninstaller.write_bytes(b"uninstaller")

    monkeypatch.setattr(service_module.sys, "platform", "win32")
    monkeypatch.setattr(service_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_module.sys, "executable", str(executable))
    monkeypatch.setattr(service_module.subprocess, "CREATE_NO_WINDOW", 1, raising=False)
    monkeypatch.setattr(service_module.subprocess, "DETACHED_PROCESS", 2, raising=False)
    monkeypatch.setattr(
        service_module.subprocess,
        "Popen",
        lambda command, **kwargs: popen_calls.append((command, kwargs)),
    )

    start_uninstaller()

    assert popen_calls[0][0] == [str(uninstaller.resolve())]
    assert popen_calls[0][1]["creationflags"] == 3
    assert popen_calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_auto_update_checks_immediately_and_stops_after_verified_update(tmp_path):
    events = []

    class Client:
        def wait_for_stop(self, timeout):
            events.append(("wait", timeout))
            return False

        def begin_update(self):
            events.append(("begin",))
            return True

        def end_update(self):
            events.append(("end",))

        def stop(self):
            events.append(("stop",))

    run_auto_update_loop(
        ConfigStore(tmp_path),
        Client(),
        apply_update_fn=lambda _store, _callback: True,
        check_update_fn=lambda _store: (True, {}),
    )

    assert events == [("wait", 0.0), ("begin",), ("stop",), ("end",)]


def test_auto_update_retries_after_failure_and_can_be_stopped(tmp_path):
    events = []
    waits = iter([False, True])

    class Client:
        def wait_for_stop(self, timeout):
            events.append(("wait", timeout))
            return next(waits)

        def begin_update(self):
            events.append(("begin",))
            return True

        def end_update(self):
            events.append(("end",))

        def stop(self):
            events.append(("stop",))

    run_auto_update_loop(
        ConfigStore(tmp_path),
        Client(),
        check_interval_seconds=10,
        retry_interval_seconds=20,
        apply_update_fn=lambda _store, _callback: (_ for _ in ()).throw(RuntimeError("offline")),
        check_update_fn=lambda _store: (True, {}),
    )

    assert events == [
        ("wait", 0.0),
        ("begin",),
        ("end",),
        ("wait", 20),
    ]


def test_windows_directory_selection_uses_native_picker(monkeypatch):
    monkeypatch.setattr(service_module.sys, "platform", "win32")
    timeouts = []
    monkeypatch.setattr(
        service_module,
        "_select_directory_windows",
        lambda timeout: timeouts.append(timeout) or r"C:\repo",
    )

    assert select_directory("invalid") == r"C:\repo"
    assert timeouts == [120.0]


def test_directory_selection_timeout_is_bounded():
    assert _remaining_selection_seconds("") == 120.0
    assert _remaining_selection_seconds("invalid") == 120.0
    assert _remaining_selection_seconds("2000-01-01T00:00:00Z") == 1.0
    assert _timestamp_expired("2000-01-01T00:00:00Z") is True
    assert _timestamp_expired("") is False


def test_windows_directory_picker_is_raised_without_staying_topmost():
    calls = []

    class User32:
        def GetForegroundWindow(self):
            return 100

        def GetWindowThreadProcessId(self, hwnd, _process_id):
            calls.append(("foreground_thread", hwnd))
            return 20

        def AttachThreadInput(self, source, target, attach):
            calls.append(("attach", source, target, attach))
            return True

        def ShowWindow(self, hwnd, command):
            calls.append(("show", hwnd, command))

        def SetWindowPos(self, hwnd, position, *_args):
            calls.append(("position", hwnd, position))

        def BringWindowToTop(self, hwnd):
            calls.append(("raise", hwnd))

        def SetForegroundWindow(self, hwnd):
            calls.append(("foreground", hwnd))

    class Kernel32:
        @staticmethod
        def GetCurrentThreadId():
            return 10

    _bring_window_to_front(User32(), Kernel32(), 200)

    assert ("attach", 10, 20, True) in calls
    assert ("position", 200, -1) in calls
    assert ("position", 200, -2) in calls
    assert ("foreground", 200) in calls
    assert calls[-1] == ("attach", 10, 20, False)


def test_ole_apartment_is_released_after_directory_selection():
    events = []

    class Function:
        def __init__(self, result=None):
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *_args):
            events.append(self.result)
            return self.result

    class Ole32:
        OleInitialize = Function(0)
        OleUninitialize = Function()

    with service_module._ole_apartment(Ole32()):
        events.append("selected")

    assert events == [0, "selected", None]


def test_companion_selection_registers_workspace_without_sending_local_path(tmp_path):
    store = ConfigStore(tmp_path / "state")
    config = WorkerConfig()
    workspace_root = tmp_path / "private" / "repo"
    workspace_root.mkdir(parents=True)
    messages = []
    expiries = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    client = WorkerClient(
        store,
        config,
        workspace_selector=lambda expires_at: expiries.append(expires_at) or str(workspace_root),
    )
    client._socket = Socket()
    client._handle(
        {
            "type": "workspace.select",
            "request_id": "wsel_test",
            "expires_at": "2099-08-06T09:00:00Z",
        }
    )
    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.01)

    result = messages[0]
    assert result["type"] == "workspace.selection.completed"
    assert result["status"] == "selected"
    assert result["workspace_id"].startswith("ws_")
    assert result["workspaces"][0]["name"] == "repo"
    assert str(workspace_root) not in json.dumps(result)
    assert expiries == ["2099-08-06T09:00:00Z"]


def test_companion_does_not_save_workspace_after_selection_expires(tmp_path):
    workspace_root = tmp_path / "private" / "repo"
    workspace_root.mkdir(parents=True)
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    store = ConfigStore(tmp_path / "state")
    config = WorkerConfig()
    client = WorkerClient(
        store,
        config,
        workspace_selector=lambda _expires_at: str(workspace_root),
    )
    client._socket = Socket()
    client._handle(
        {
            "type": "workspace.select",
            "request_id": "wsel_expired",
            "expires_at": "2000-01-01T00:00:00Z",
        }
    )
    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.01)

    assert messages[0]["status"] == "failed"
    assert messages[0]["error"] == "selection_expired"
    assert config.workspaces == []


def test_companion_selection_reuses_live_config_when_store_reload_is_unavailable(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "state")
    config = WorkerConfig()
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    client = WorkerClient(
        store,
        config,
        workspace_selector=lambda _expires_at: str(workspace_root),
    )
    client._socket = Socket()
    monkeypatch.setattr(store, "load", lambda: (_ for _ in ()).throw(RuntimeError("reload race")))
    client._handle({"type": "workspace.select", "request_id": "wsel_live_config"})

    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.01)
    assert messages[0]["status"] == "selected"
    assert config.workspaces[0].path == str(workspace_root.resolve())


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (WorkspacePathError("missing"), "workspace_path_unavailable"),
        (WorkspaceConfigWriteError("locked"), "workspace_config_write_failed"),
    ],
)
def test_companion_selection_reports_stable_workspace_errors(
    tmp_path, monkeypatch, exception, expected_error
):
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    store = ConfigStore(tmp_path / "state")
    monkeypatch.setattr(
        store,
        "add_workspace",
        lambda _config, _path: (_ for _ in ()).throw(exception),
    )
    client = WorkerClient(
        store,
        WorkerConfig(),
        workspace_selector=lambda _expires_at: str(tmp_path),
    )
    client._socket = Socket()
    client._handle({"type": "workspace.select", "request_id": "wsel_error"})

    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.01)

    assert messages[0]["status"] == "failed"
    assert messages[0]["error"] == expected_error


def test_worker_rejects_new_task_while_update_is_installing(tmp_path):
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    workspace_id = "ws_" + "a" * 32
    config = WorkerConfig(
        workspaces=[LocalWorkspace(workspace_id, "Repo", str(tmp_path))]
    )
    client = WorkerClient(ConfigStore(tmp_path / "state"), config)
    client._socket = Socket()

    assert client.begin_update() is True
    assert client.begin_update() is False
    client._start_task(
        {
            "task_id": "task_" + "b" * 32,
            "session_id": "ses_" + "c" * 32,
            "workspace_id": workspace_id,
        }
    )
    client.end_update()

    assert messages[-1]["type"] == "terminal"
    assert messages[-1]["stop_reason"] == "worker_updating"


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


def test_blocking_model_call_is_interrupted_by_worker_cancellation():
    started = threading.Event()
    release = threading.Event()

    class BlockingModelClient:
        def complete(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            started.set()
            release.wait(timeout=2)
            return "late response"

    token = CancellationToken()
    client = CancellableModelClient(BlockingModelClient(), token, poll_interval=0.01)
    errors = []

    def invoke():
        try:
            client.complete("wait", 32)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert started.wait(timeout=1)
    token.cancel()
    thread.join(timeout=0.5)
    release.set()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RunCancelled)


def test_active_run_cancel_supports_non_remote_approval_strategy():
    token = CancellationToken()
    active = ActiveRun("task_cancel", token, AutoApprovalStrategy())

    active.cancel(0)

    assert token.is_cancelled()


def test_remote_execution_hooks_publish_read_only_and_shell_result_previews():
    events = []
    hooks = RemoteExecutionHooks(lambda event_type, data: events.append((event_type, data)), CancellationToken())

    hooks.tool_requested(
        None,
        {
            "id": "call_read",
            "name": "read_file",
            "args": {"path": "README.md", "start": 1, "end": 4},
        },
    )
    hooks.before_tool(
        None,
        {"id": "call_read", "name": "read_file", "args": {"path": "README.md"}},
    )
    hooks.after_tool(
        None,
        ToolExecutionResult(content="# README.md\nhello", metadata={"tool_status": "ok"}),
    )

    assert events[0][1]["args_preview"] == {"path": "README.md", "start": 1, "end": 4}
    assert events[-1][1]["result_preview"] == "# README.md\nhello"

    events.clear()
    hooks.tool_requested(
        None,
        {"id": "call_shell", "name": "run_shell", "args": {"command": "whoami"}},
    )
    hooks.before_tool(None, {"id": "call_shell", "name": "run_shell", "args": {}})
    hooks.after_tool(
        None,
        ToolExecutionResult(content="private output", metadata={"tool_status": "ok"}),
    )
    assert events[0][1]["args_preview"] == {"command": "whoami"}
    assert events[-1][1]["result_preview"] == "private output"
    hooks.commentary(None, "still working")
    assert events[-1] == ("assistant.commentary", {"text": "still working"})


def test_remote_execution_hooks_stream_only_final_answer_projection():
    events = []
    hooks = RemoteExecutionHooks(lambda event_type, data: events.append((event_type, data)), CancellationToken())

    hooks.before_model(None)
    hooks.model_text_delta(None, "execute", '<tool>{"name":"read_file"}</tool>')
    assert not [item for item in events if item[0] == "assistant.delta"]

    hooks.before_model(None)
    for delta in ("prefix", "<fi", "nal>hel", "lo</fi", "nal>"):
        hooks.model_text_delta(None, "execute", delta)
    assert "".join(data["text"] for event, data in events if event == "assistant.delta") == "hello"

    visible_count = len([item for item in events if item[0] == "assistant.delta"])
    hooks.before_model(None)
    hooks.model_text_delta(None, "review", "<final>private review</final>")
    assert len([item for item in events if item[0] == "assistant.delta"]) == visible_count
    hooks.model_retrying(
        None,
        "planning",
        {
            "attempt": 1,
            "max_attempts": 2,
            "error_code": "model_timeout",
            "retry_delay_seconds": 0.5,
        },
    )
    assert events[-1][0] == "model.retrying"
    assert events[-1][1]["stage"] == "planning"
    assert events[-1][1]["reset_stream"] is True


def test_remote_execution_hooks_stream_native_plain_text_final_candidate():
    events = []
    hooks = RemoteExecutionHooks(
        lambda event_type, data: events.append((event_type, data)),
        CancellationToken(),
        allow_plain_text_final=True,
    )

    hooks.begin_answer_candidate(None)
    hooks.before_model(None)
    hooks.model_text_delta(None, "execute", "项目")
    hooks.model_text_delta(None, "execute", "总结")
    hooks.after_model(None, {})
    assert not [item for item in events if item[0] == "assistant.delta"]

    hooks.commit_answer_candidate(None)

    visible = "".join(data["text"] for event, data in events if event == "assistant.delta")
    assert visible == "项目总结"


def test_remote_execution_hooks_publish_protocol_retry_without_model_output():
    events = []
    hooks = RemoteExecutionHooks(lambda event_type, data: events.append((event_type, data)), CancellationToken())

    hooks.model_protocol_retrying(
        None,
        "execute",
        {
            "attempt": 1,
            "max_attempts": 2,
            "response_chars": 84,
            "detected_format": "json_object",
            "top_level_keys": ["answer"],
            "response_hash": "0123456789abcdef",
            "raw_model_output": "must not pass",
        },
    )

    assert events == [
        (
            "model.protocol_retrying",
            {
                "stage": "execute",
                "attempt": 1,
                "max_attempts": 2,
                "error_code": "model_protocol_invalid",
                "response_chars": 84,
                "detected_format": "json_object",
                "top_level_keys": ["answer"],
                "response_hash": "0123456789abcdef",
                "reset_stream": True,
            },
        )
    ]


def test_remote_execution_hooks_heartbeat_keeps_run_elapsed_and_model_round():
    events = []
    hooks = RemoteExecutionHooks(lambda event_type, data: events.append((event_type, data)), CancellationToken())

    hooks.before_model(None)
    hooks._last_heartbeat_at -= 2
    hooks._model_started_at -= 2
    hooks._run_started_at -= 5
    hooks.model_text_delta(None, "execute", "<talk>checking</talk>")

    heartbeat = next(data for event_type, data in events if event_type == "model.heartbeat")
    assert heartbeat["round"] == 1
    assert heartbeat["run_elapsed_seconds"] >= 5
    assert heartbeat["elapsed_seconds"] >= 1

    hooks.before_model(None)
    assert events[-1][0] == "model.started"
    assert events[-1][1]["round"] == 2


def test_remote_execution_hooks_commit_only_accepted_answer_candidate():
    events = []
    hooks = RemoteExecutionHooks(
        lambda event_type, data: events.append((event_type, data)),
        CancellationToken(),
    )

    hooks.begin_answer_candidate(None)
    hooks.before_model(None)
    hooks.model_text_delta(None, "execute", "<final>rejected answer</final>")
    assert not [item for item in events if item[0] == "assistant.delta"]
    hooks.discard_answer_candidate(None)
    assert not [item for item in events if item[0] == "assistant.delta"]

    hooks.begin_answer_candidate(None)
    hooks.before_model(None)
    hooks.model_text_delta(None, "execute", "<final>accepted answer</final>")
    hooks.commit_answer_candidate(None)

    visible = "".join(data["text"] for event, data in events if event == "assistant.delta")
    assert visible == "accepted answer"


def test_remote_execution_hooks_review_retry_keeps_answer_candidate():
    events = []
    hooks = RemoteExecutionHooks(
        lambda event_type, data: events.append((event_type, data)),
        CancellationToken(),
    )

    hooks.begin_answer_candidate(None)
    hooks.before_model(None)
    hooks.model_text_delta(None, "execute", "<final>accepted after review</final>")
    hooks.model_retrying(
        None,
        "review",
        {
            "attempt": 1,
            "max_attempts": 2,
            "error_code": "model_timeout",
            "retry_delay_seconds": 0.5,
        },
    )
    hooks.commit_answer_candidate(None)

    visible = "".join(data["text"] for event, data in events if event == "assistant.delta")
    assert visible == "accepted after review"


def test_remote_execution_hooks_redact_secret_split_across_deltas():
    events = []
    secret = "split-secret-value"
    with patch.dict(os.environ, {"PICO_OPENAI_API_KEY": secret}, clear=False):
        hooks = RemoteExecutionHooks(
            lambda event_type, data: events.append((event_type, data)),
            CancellationToken(),
        )
        hooks.before_model(None)
        hooks.model_text_delta(None, "execute", "<final>public split-se")
        hooks.model_text_delta(None, "execute", "cret-value done</final>")

    visible = "".join(data["text"] for event, data in events if event == "assistant.delta")
    assert visible == "public <redacted> done"
    assert secret not in visible


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
        model_client_factory=lambda: FakeModelClient(
            [
                # 原生路径（run_native）：router 先分类 intent，主循环走文本协议
                json.dumps({"intent": "conversation", "requires_research": False}),
                "<final>done</final>",
            ]
        ),
    )

    terminal = sent[-1]
    assert terminal["type"] == "terminal"
    assert terminal["status"] == "completed"
    assert terminal["final_answer"] == "done"
    assert "session" not in terminal
    assert terminal["message_total"] == 2
    stored = json.loads(
        (tmp_path / "worker-state" / "sessions" / f"{task['session']['id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["content"] for item in stored["history"]] == ["say done", "done"]


def test_runtime_cancels_while_planning_model_request_is_blocked(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = {
        "task_id": "task_" + "1" * 32,
        "run_id": "run_" + "2" * 32,
        "workspace_id": "ws_" + "3" * 32,
        "input": "inspect the repository",
        "max_steps": 2,
        "settings": {"max_new_tokens": 128, "model_timeout_seconds": 120},
        "session": {
            "id": "ses_" + "4" * 32,
            "owner_id": "11111111-1111-4111-8111-111111111111",
            "workspace_id": "ws_" + "3" * 32,
            "workspace_root": "worker://placeholder",
            "title": "test",
            "history": [],
            "memory": default_memory_state(),
            "checkpoints": {},
        },
    }
    started = threading.Event()
    release = threading.Event()

    class BlockingModelClient:
        def complete(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            started.set()
            release.wait(timeout=2)
            return "late response"

    sent = []
    token = CancellationToken()
    approval = RemoteApprovalStrategy(sent.append, task["task_id"], token)
    active = ActiveRun(task["task_id"], token, approval)
    thread = threading.Thread(
        target=lambda: run_task(
            task=task,
            workspace_path=workspace,
            data_dir=tmp_path / "worker-state",
            send=sent.append,
            active=active,
            model_client_factory=BlockingModelClient,
        )
    )

    thread.start()
    # 慢机器（Windows / CI runner）上 run_task 启动到首次模型调用可能 >1s，
    # 放宽等待避免时序 flaky；取消语义不变。
    assert started.wait(timeout=5)
    active.cancel(0)
    thread.join(timeout=5)
    release.set()

    assert not thread.is_alive()
    assert sent[-1]["type"] == "terminal"
    assert sent[-1]["status"] == "cancelled"
    assert sent[-1]["stop_reason"] == "user_cancelled"
    assert not any(
        item.get("type") == "event" and item.get("event_type", "").startswith("tool.")
        for item in sent
    )


def test_history_read_and_model_configuration_protocol(tmp_path):
    store = ConfigStore(tmp_path / "state")
    session_store = SessionStore(store.root / "sessions")
    session_id = "ses_" + "a" * 32
    session_store.save(
        {
            "id": session_id,
            "workspace_id": "ws_" + "b" * 32,
            "history": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
        }
    )
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    client = WorkerClient(store, WorkerConfig())
    client._socket = Socket()
    client._handle(
        {
            "type": "session.history.get",
            "request_id": "hist_test",
            "session_id": session_id,
            "message_limit": 100,
        }
    )
    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [item["content"] for item in messages[0]["messages"]] == [
        "old question",
        "old answer",
    ]

    messages.clear()
    with patch.dict("os.environ", {}, clear=True):
        client._handle(
            {
                "type": "model.configure",
                "request_id": "model_test",
                "base_url": "https://provider.example/v1",
                "api_key": "new-secret",
                "model": "model-b",
            }
        )
        assert messages == [
            {
                "type": "model.configuration.completed",
                "request_id": "model_test",
                "status": "completed",
                "model": "model-b",
                "model_provider": "",
                "model_capabilities": {
                    "provider": "openai-compatible",
                    "models": [
                        {
                            "id": "model-b",
                            "display_name": "model-b",
                            "reasoning_efforts": ["none"],
                            "max_output_tokens": 4096,
                            "usage_fields": [
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                                "cached_tokens",
                                "cache_hit",
                            ],
                            "supports_temperature": True,
                        }
                    ],
                },
            }
        ]
        messages.clear()
        client._handle(
            {
                "type": "session.history.get",
                "request_id": "hist_after_provider_switch",
                "session_id": session_id,
                "message_limit": 100,
            }
        )
        deadline = time.monotonic() + 1
        while not messages and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [item["content"] for item in messages[0]["messages"]] == [
            "old question",
            "old answer",
        ]


def test_history_read_returns_empty_history_when_local_session_missing(tmp_path):
    # 任务失败且本地从未持久化时，控制面仍有 session + task 失败记录；
    # worker 应返回 completed + 空历史（而非 failed），避免 get_session 整条 422。
    store = ConfigStore(tmp_path / "state")
    session_id = "ses_" + "f" * 32
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    client = WorkerClient(store, WorkerConfig())
    client._socket = Socket()
    client._handle(
        {
            "type": "session.history.get",
            "request_id": "hist_missing",
            "session_id": session_id,
            "message_limit": 100,
        }
    )
    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.01)
    assert messages[0]["status"] == "completed"
    assert messages[0]["messages"] == []
    assert messages[0]["message_total"] == 0
    assert messages[0]["error"] == "history_unavailable"


def test_delete_workspace_removes_local_sessions_and_run_artifacts(tmp_path):
    store = ConfigStore(tmp_path / "state")
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    workspace_id = "ws_" + "b" * 32
    session_id = "ses_" + "c" * 32
    run_id = "run_" + "d" * 32
    config = WorkerConfig(
        workspaces=[LocalWorkspace(workspace_id, "Repo", str(workspace_root))]
    )
    store.save(config)
    store.save_workspaces(config)
    session_store = SessionStore(store.root / "sessions")
    session_store.save({"id": session_id, "workspace_id": workspace_id, "history": []})
    run_dir = store.root / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "task_state.json").write_text("{}", encoding="utf-8")
    messages = []

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

    client = WorkerClient(store, config)
    client._socket = Socket()
    client._handle(
        {
            "type": "entity.delete",
            "request_id": "delete_test",
            "entity_type": "workspace",
            "entity_id": workspace_id,
            "session_ids": [session_id],
            "run_ids": [run_id],
        }
    )

    assert messages[0]["status"] == "completed"
    assert messages[0]["deleted_session_ids"] == [session_id]
    assert session_store.exists(session_id) is False
    assert run_dir.exists() is False
    assert store.load().workspaces == []


def test_remote_uninstall_acknowledges_before_launching_uninstaller(tmp_path):
    messages = []
    launched = threading.Event()

    class Socket:
        def send(self, raw):
            messages.append(json.loads(raw))

        def close(self):
            return None

    client = WorkerClient(
        ConfigStore(tmp_path / "state"),
        WorkerConfig(),
        uninstall_callback=launched.set,
    )
    client._socket = Socket()
    client._handle({"type": "worker.uninstall", "request_id": "uninstall_test"})

    assert messages == [
        {
            "type": "worker.uninstall.completed",
            "request_id": "uninstall_test",
            "status": "completed",
        }
    ]
    assert launched.wait(1)


def test_large_unicode_history_stays_within_worker_message_limit(tmp_path):
    store = ConfigStore(tmp_path / "state")
    session_store = SessionStore(store.root / "sessions")
    session_id = "ses_" + "f" * 32
    session_store.save(
        {
            "id": session_id,
            "workspace_id": "ws_" + "e" * 32,
            "history": [
                {"role": "user", "content": "中" * 4000}
                for _ in range(500)
            ],
        }
    )
    raw_messages = []

    class Socket:
        def send(self, raw):
            raw_messages.append(raw)

    client = WorkerClient(store, WorkerConfig())
    client._socket = Socket()
    client._handle(
        {
            "type": "session.history.get",
            "request_id": "hist_large",
            "session_id": session_id,
            "message_limit": 500,
        }
    )
    deadline = time.monotonic() + 3
    while not raw_messages and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(raw_messages[0].encode("utf-8")) <= 2 * 1024 * 1024
    assert len(json.loads(raw_messages[0])["messages"]) == 500

def test_list_provider_models_normalizes_base_url_for_v1(monkeypatch):
    """OpenAI/deepseek 用户可填带或不带 /v1 的 Base URL；Anthropic 避免 /v1/v1。"""
    captured_urls = []
    captured_user_agents = []

    class FakeResponse:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured_urls.append(request.full_url)
        captured_user_agents.append(request.get_header("User-agent"))
        return FakeResponse(b'{"data":[{"id":"gpt-5.6-sol"}]}')

    monkeypatch.setattr("threadforge_worker.client.urllib.request.urlopen", fake_urlopen)

    models, error = _list_provider_models("https://codex.ximuai.com/v1", "sk-test", "openai_compatible")
    assert error == ""
    assert models == ["gpt-5.6-sol"]
    assert captured_urls[-1] == "https://codex.ximuai.com/v1/models"
    assert captured_user_agents[-1] == f"ThreadForge-Worker/{__version__}"

    models, error = _list_provider_models("https://api.anthropic.com", "k", "anthropic")
    assert error == ""
    assert captured_urls[-1] == "https://api.anthropic.com/v1/models"

    models, error = _list_provider_models("https://api.anthropic.com/v1", "k", "anthropic")
    assert error == ""
    assert captured_urls[-1] == "https://api.anthropic.com/v1/models"


def test_extract_model_ids_accepts_standard_data_shape():
    """§2.2：OpenAI 兼容标准返回 data[].id。"""
    models, error = _extract_model_ids({"data": [{"id": "gpt-5.6-sol"}, {"id": "deepseek-v4-flash"}]}, "openai_compatible")
    assert error == ""
    assert models == ["gpt-5.6-sol", "deepseek-v4-flash"]


def test_extract_model_ids_accepts_flat_models_and_string_list():
    """§2.2：兼容 models[] 与裸字符串数组，避免「连上但 0 模型」假阳性。"""
    models, error = _extract_model_ids({"models": [{"name": "a"}, {"name": "b"}]}, "openai_compatible")
    assert error == ""
    assert models == ["a", "b"]
    models, error = _extract_model_ids(["gpt-5.5", "gpt-5.6-sol"], "openai_compatible")
    assert error == ""
    assert models == ["gpt-5.5", "gpt-5.6-sol"]


def test_extract_model_ids_accepts_valid_empty_model_lists():
    """合法的空模型列表表示端点可达，但供应商未公开模型目录。"""
    models, error = _extract_model_ids({"object": "list", "data": []}, "openai_compatible")
    assert models == []
    assert error == ""
    models, error = _extract_model_ids({"models": []}, "openai_compatible")
    assert models == []
    assert error == ""


def test_extract_model_ids_reports_invalid_on_garbage():
    """§2.2：无法解析的响应应报 model_response_invalid，而不是 0 模型。"""
    models, error = _extract_model_ids({"foo": []}, "openai_compatible")
    assert models == []
    assert error == "model_response_invalid"
    models, error = _extract_model_ids(None, "openai_compatible")
    assert error == "model_response_invalid"


def test_capability_models_from_provider_per_model_efforts():
    """§2.2 模型×档位矩阵：每个模型各自的 reasoning_efforts，缺省回退 provider 级。"""
    provider = {
        "model": "default",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "reasoning_efforts": ["none", "high"],
        "model_efforts": {"deepseek-v4-pro": ["none", "low", "medium", "high", "xhigh", "max"]},
        "max_output_tokens": 8192,
        "context_window": 128000,
    }
    out = _capability_models_from_provider(provider, "fallback")
    assert len(out) == 2
    # 无 model_efforts 的模型回退到 provider 级
    assert out[0]["id"] == "deepseek-v4-flash"
    assert out[0]["reasoning_efforts"] == ["none", "high"]
    # 有 model_efforts 的模型用各自档位
    assert out[1]["id"] == "deepseek-v4-pro"
    assert out[1]["reasoning_efforts"] == ["none", "low", "medium", "high", "xhigh", "max"]
    assert out[1]["max_output_tokens"] == 8192
    assert out[1]["context_window"] == 128000


def test_capability_models_from_provider_empty_models_forces_default():
    """§2.2：无 models 时回退单条默认模型（向后兼容）。"""
    provider = {"model": "gpt-5.6-sol", "reasoning_efforts": ["none", "high"]}
    out = _capability_models_from_provider(provider, "fallback")
    assert len(out) == 1
    assert out[0]["id"] == "gpt-5.6-sol"
    assert out[0]["reasoning_efforts"] == ["none", "high"]


def test_model_capabilities_include_models_from_all_providers(tmp_path):
    """Worker 上报能力时不能遗漏非首个 Provider 的模型。"""
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_deepseek",
        base_url="https://deepseek.example/v1",
        api_key="deep-secret",
        model="deepseek-v4-flash",
        protocol="deepseek",
        reasoning_efforts=("none", "high"),
    )
    store.save_provider(
        "prv_openai",
        base_url="https://openai.example/v1",
        api_key="openai-secret",
        model="gpt-5.6-sol",
        protocol="openai_compatible",
        reasoning_efforts=("none", "low", "medium", "high"),
    )

    capabilities = _model_capabilities(store)
    models = {item["id"]: item for item in capabilities["models"]}

    assert set(models) == {"deepseek-v4-flash", "gpt-5.6-sol"}
    assert "medium" in models["gpt-5.6-sol"]["reasoning_efforts"]


def test_model_capabilities_skip_empty_provider(tmp_path):
    """尚未填写模型的 Provider 不应生成空模型 ID。"""
    store = ConfigStore(tmp_path / "state")
    store.save_provider(
        "prv_empty",
        base_url="",
        api_key="",
        model="",
        protocol="openai_compatible",
    )

    with patch.dict("os.environ", {"PICO_OPENAI_MODEL": "env-model"}, clear=True):
        capabilities = _model_capabilities(store)

    assert all(item["id"] for item in capabilities["models"])


def test_sandbox_os_backend_builds_native_shell_factory():
    """OS-native backend returns a factory producing a resource-limited ShellProcess.

    sandbox_backend='os' must not require Docker: the worker factory builds a
    ShellProcess with Job Object / setrlimit resource caps instead of a Docker
    per-command container. sandbox_backend='docker' still imports the Docker path.
    """
    factory = _sandbox_shell_factory(
        {
            "sandbox_enabled": True,
            "sandbox_backend": "os",
            "sandbox_memory_limit": "256m",
            "sandbox_pids_limit": 8,
        },
        send_runtime_event=lambda *a, **k: None,
    )
    assert factory is not None
    assert callable(factory)
    # The factory builds a ShellProcess (not a Docker) that carries resource limits.
    import os
    import tempfile

    shell = factory(
        "echo hi",
        cwd=tempfile.gettempdir(),
        env=dict(os.environ),
        timeout=10,
        output_max_bytes=1024,
    )
    assert shell._resource_limits["max_processes"] == 8
    assert shell._resource_limits["memory_bytes"] == 256 * 1024 * 1024


def test_sandbox_disabled_returns_none():
    """sandbox_enabled=False keeps the legacy host ShellProcess path."""
    factory = _sandbox_shell_factory(
        {"sandbox_enabled": False, "sandbox_backend": "os"},
        send_runtime_event=lambda *a, **k: None,
    )
    assert factory is None


"""Local Worker configuration, approval and offline Runtime tests."""

from __future__ import annotations

import json
import sys
import threading
import time
from unittest.mock import patch

import pytest
from pico.approval import ApprovalOutcome, ApprovalRequest
from pico.features.memory import default_memory_state
from pico.providers.clients import FakeModelClient
from pico.session_store import SessionStore
from pico.tool_executor import ToolExecutionResult
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

import threadforge_worker.config as config_module
import threadforge_worker.service as service_module
from threadforge_worker.auto_update import run_auto_update_loop
from threadforge_worker.cli import _parse_protocol_uri
from threadforge_worker.cli import main as worker_main
from threadforge_worker.client import (
    WorkerClient,
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
    CancellationToken,
    RemoteApprovalStrategy,
    RemoteExecutionHooks,
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


def test_service_lock_prevents_duplicate_worker_processes(tmp_path):
    outer = ServiceLock(tmp_path)
    inner = ServiceLock(tmp_path)
    with outer, pytest.raises(ServiceAlreadyRunningError), inner:
        pass


def test_service_exits_cleanly_before_first_pairing(tmp_path):
    assert run_service(str(tmp_path)) == 0


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


def test_worker_rejects_permanent_websocket_handshake_failure(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    config = WorkerConfig(device_id="dev_" + "a" * 32, device_token="token")
    client = WorkerClient(store, config)
    monkeypatch.setattr(
        client,
        "_run_once",
        lambda: (_ for _ in ()).throw(
            InvalidStatus(Response(401, "Unauthorized", Headers()))
        ),
    )

    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.run_forever()


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
        apply_update_fn=lambda _store: True,
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
        apply_update_fn=lambda _store: (_ for _ in ()).throw(RuntimeError("offline")),
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


def test_remote_execution_hooks_publish_read_only_preview_and_hide_risky_result():
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
    assert "args_preview" not in events[0][1]
    assert "result_preview" not in events[-1][1]
    hooks.commentary(None, "still working")
    assert events[-1] == ("assistant.commentary", {"text": "still working"})


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
                json.dumps(
                    {
                        "schema_version": "1",
                        "plan_id": "plan_worker",
                        "revision": 1,
                        "intent": "conversation",
                        "summary": "Answer the user.",
                        "steps": [
                            {
                                "id": "answer",
                                "goal": "Answer the user",
                                "dependencies": [],
                                "required_tools": [],
                                "required_evidence": [],
                                "done_when": ["the answer is returned"],
                            }
                        ],
                        "acceptance": ["the answer is returned"],
                        "risk_level": "low",
                        "budgets": {
                            "model_rounds": 2,
                            "tool_calls": 1,
                            "input_tokens": 1000,
                            "output_tokens": 256,
                            "elapsed_seconds": 30,
                        },
                    }
                ),
                '{"answer":"done"}',
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
                "model_capabilities": {
                    "provider": "openai-compatible",
                    "models": [
                        {
                            "id": "model-b",
                            "display_name": "model-b",
                            "reasoning_efforts": ["none"],
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

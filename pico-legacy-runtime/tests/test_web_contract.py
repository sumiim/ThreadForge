"""Offline tests for the Web-facing runtime extensions (backward compatible)."""

from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

from pico import Pico
from pico.approval import ApprovalOutcome
from pico.execution_hooks import ProcessCleanupFailed, RunCancelled
from pico.providers.clients import FakeModelClient, OpenAICompatibleModelClient
from pico.session_store import InMemorySessionStore
from pico.task_state import STATUS_FAILED, STOP_REASON_PROCESS_CLEANUP_FAILED, STOP_REASON_USER_CANCELLED
from pico.workspace import WorkspaceContext


class CancelToken:
    def __init__(self):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def is_cancelled(self):
        return self._cancelled

    def raise_if_cancelled(self):
        if self._cancelled:
            raise RunCancelled()


class DelayedModel:
    supports_prompt_cache = False

    def __init__(self, output, delay):
        self._output = output
        self._delay = delay
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        time.sleep(self._delay)
        return self._output


def make_agent(tmp_path, outputs, approval_policy="never", **kwargs):
    workspace = WorkspaceContext.build(str(tmp_path))
    model = FakeModelClient(outputs=list(outputs))
    store = InMemorySessionStore()
    agent = Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )
    return agent


def test_preassigned_ids_are_used(tmp_path):
    agent = make_agent(tmp_path, ["<final>done</final>"])
    agent.ask("hello", task_id="task_custom", run_id="run_custom")
    task_state = agent.current_task_state
    assert task_state.task_id == "task_custom"
    assert task_state.run_id == "run_custom"
    assert agent.run_store.run_dir("run_custom").is_dir()


def test_ask_without_ids_auto_generates(tmp_path):
    agent = make_agent(tmp_path, ["<final>done</final>"])
    agent.ask("hello")
    assert agent.current_task_state.task_id.startswith("task_")
    assert agent.current_task_state.run_id.startswith("run_")


def test_tool_registry_and_control_plane_share_one_context(tmp_path):
    agent = make_agent(tmp_path, [], approval_policy="auto")
    bound_context = agent.tools["run_shell"]["run"].args[0]
    assert bound_context is agent.tool_context()


def test_shell_cleanup_failure_reaches_task_state(tmp_path):
    agent = make_agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"echo x","timeout":20}}</tool>'],
        approval_policy="auto",
    )
    failed_result = type(
        "ShellResult",
        (),
        {
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "output_truncated": False,
            "cancelled": True,
            "cleanup_succeeded": False,
        },
    )()
    with patch("pico.tools.ShellProcess") as shell_cls:
        shell_cls.return_value.run.return_value = failed_result
        answer = agent.ask("run it")
    assert "cleanup could not be confirmed" in answer
    assert agent.current_task_state.status == STATUS_FAILED
    assert agent.current_task_state.stop_reason == STOP_REASON_PROCESS_CLEANUP_FAILED


def test_tool_executor_does_not_swallow_cleanup_failure(tmp_path):
    agent = make_agent(tmp_path, [], approval_policy="auto")
    with patch("pico.tools.ShellProcess") as shell_cls:
        shell_cls.return_value.run.return_value = type(
            "ShellResult",
            (),
            {"cleanup_succeeded": False},
        )()
        with pytest.raises(ProcessCleanupFailed):
            agent.execute_tool("run_shell", {"command": "echo x", "timeout": 20})


def test_cancel_before_run_converges_to_user_cancelled(tmp_path):
    token = CancelToken()
    token.cancel()
    agent = make_agent(tmp_path, ["<final>done</final>"], cancellation_token=token)
    agent.ask("hello")
    task_state = agent.current_task_state
    assert task_state.status == "stopped"
    assert task_state.stop_reason == STOP_REASON_USER_CANCELLED


def test_cancel_during_model_wait_discards_late_response(tmp_path):
    token = CancelToken()
    workspace = WorkspaceContext.build(str(tmp_path))
    model = DelayedModel("<final>should be discarded</final>", delay=0.3)
    store = InMemorySessionStore()
    agent = Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy="never",
        cancellation_token=token,
    )
    thread = threading.Thread(target=agent.ask, args=("hello",))
    thread.start()
    time.sleep(0.05)
    token.cancel()
    thread.join(timeout=5)
    task_state = agent.current_task_state
    assert task_state.status == "stopped"
    assert task_state.stop_reason == STOP_REASON_USER_CANCELLED
    assistant_texts = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
    assert "should be discarded" not in "".join(assistant_texts)


def test_approve_delegates_to_strategy_auto_never(tmp_path):
    agent = make_agent(tmp_path, [], approval_policy="auto")
    assert agent.approve("write_file", {}) is True
    assert agent.approve_outcome("write_file", {}) is ApprovalOutcome.APPROVED

    agent2 = make_agent(tmp_path, [], approval_policy="never")
    assert agent2.approve("write_file", {}) is False
    assert agent2.approve_outcome("write_file", {}) is ApprovalOutcome.REJECTED


def test_max_attempts_default_and_override():
    client = OpenAICompatibleModelClient(
        model="m", base_url="http://example.test/v1", api_key="", temperature=0.2, timeout=10
    )
    assert client.max_attempts == 3  # CLI default preserved

    web_client = OpenAICompatibleModelClient(
        model="m",
        base_url="http://example.test/v1",
        api_key="",
        temperature=0.2,
        timeout=10,
        max_attempts=1,
    )
    assert web_client.max_attempts == 1

    with pytest.raises(ValueError):
        OpenAICompatibleModelClient(
            model="m", base_url="http://example.test/v1", api_key="", temperature=0.2, timeout=10, max_attempts=0
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object containment is Windows-specific")
def test_windows_shell_cancellation_terminates(tmp_path):
    from pico.shell_process import ShellProcess

    token = CancelToken()
    shell = ShellProcess(
        "ping -n 60 127.0.0.1",
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=30,
        output_max_bytes=1024,
        cancellation_token=token,
    )
    threading.Thread(target=lambda: (time.sleep(0.2), token.cancel())).start()
    result = shell.run()
    assert result.cancelled is True
    assert result.cleanup_succeeded is True


def test_shell_timeout_is_distinguishable(tmp_path):
    from pico.shell_process import ShellProcess

    if sys.platform == "win32":
        command = "ping -n 60 127.0.0.1"
    else:
        command = "sleep 30"
    shell = ShellProcess(
        command,
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=1,
        output_max_bytes=1024,
    )
    result = shell.run()
    assert result.timed_out is True
    assert result.cleanup_succeeded is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups are not used on Windows")
def test_posix_cleanup_kills_descendant_after_shell_leader_exits(tmp_path):
    from pico.shell_process import ShellProcess

    command = f'''"{sys.executable}" -c "import subprocess; subprocess.Popen(['sleep', '30'])"'''
    shell = ShellProcess(
        command,
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=10,
        output_max_bytes=1024,
        cleanup_grace_seconds=1,
    )

    started = time.monotonic()
    result = shell.run()
    assert result.cleanup_succeeded is True
    assert time.monotonic() - started < 5


def test_shell_output_uses_one_combined_byte_budget(tmp_path):
    from pico.shell_process import ShellProcess

    command = (
        f'"{sys.executable}" -c "import sys; '
        "sys.stdout.write('a'*5000); sys.stderr.write('b'*5000)\""
    )
    shell = ShellProcess(
        command,
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=10,
        output_max_bytes=1024,
    )
    result = shell.run()
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024
    assert result.output_truncated is True

"""run_native 原生单循环测试（§7.7.1 阶段 2）。

用脚本化 FakeModelClient 验证：
- conversation 直接回答（无工具）
- read_only 读证据后回答
- code_change 写证据 + review gate 收尾
- 显式 intent 跳过 router
- 产出与 run_agent 同形 BackendRunResult
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pico import Pico
from pico.event_sink import NullSink
from pico.providers.clients import FakeModelClient
from pico.run_store import RunStore
from pico.session_store import SessionStore
from pico.task_state import STOP_REASON_FINAL_ANSWER_RETURNED
from pico.workspace import WorkspaceContext

from langgraph_pico import run_native
from langgraph_pico.intent import INTENT_CODE_CHANGE, INTENT_CONVERSATION, INTENT_READ_ONLY


def _build_runtime(tmp_path, outputs, *, allowed_tools=None):
    import pico
    from pathlib import Path as _Path

    pico_root = _Path(pico.__file__).resolve().parent.parent
    source = pico_root / "tests" / "fixtures" / "bench_repo_readme"
    fixture_root = tmp_path / "runtime-workspace"
    shutil.copytree(source, fixture_root)
    model_client = FakeModelClient(outputs)
    agent = Pico(
        model_client=model_client,
        workspace=WorkspaceContext.build(fixture_root, repo_root_override=fixture_root),
        session_store=SessionStore(fixture_root / ".pico" / "sessions"),
        run_store=RunStore(fixture_root / ".pico" / "runs"),
        approval_policy="auto",
        max_steps=6,
        allowed_tools=allowed_tools or ["list_files", "read_file", "search", "patch_file"],
        event_sink=NullSink(),
    )
    return agent, model_client, fixture_root


def test_native_conversation_answers_without_tools(tmp_path):
    agent, _, fixture_root = _build_runtime(tmp_path, ["<final>hello back</final>"])
    result = run_native(
        agent,
        "hello",
        task_mode=INTENT_CONVERSATION,
    )
    assert result.task_state.status == "completed"
    assert result.task_state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
    assert result.task_state.intent == INTENT_CONVERSATION
    assert result.run_metadata["resolved_intent"] == INTENT_CONVERSATION
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_read_only_requires_evidence(tmp_path):
    outputs = [
        '<tool>{"name": "read_file", "args": {"path": "README.md"}}</tool>',
        "<final>README says placeholder</final>",
    ]
    agent, _, fixture_root = _build_runtime(
        tmp_path, outputs, allowed_tools=["list_files", "read_file", "search"]
    )
    result = run_native(
        agent,
        "What does the README say?",
        task_mode=INTENT_READ_ONLY,
    )
    state = result.task_state
    assert state.status == "completed"
    assert state.intent == INTENT_READ_ONLY
    assert any(
        item.get("tool_name") == "read_file"
        for item in (getattr(state, "evidence", None) or [])
    )
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_code_change_applies_and_passes_gate(tmp_path):
    outputs = [
        '<tool>{"name": "read_file", "args": {"path": "README.md"}}</tool>',
        '<tool>{"name": "patch_file", "args": {"path": "README.md", "old_text": "This is a placeholder benchmark fixture.", "new_text": "This fixture is a locked benchmark workspace."}}</tool>',
        "<final>Done</final>",
    ]
    agent, _, fixture_root = _build_runtime(tmp_path, outputs)
    result = run_native(
        agent,
        "Replace the placeholder sentence with the locked text.",
        task_mode=INTENT_CODE_CHANGE,
    )
    state = result.task_state
    assert state.status == "completed"
    assert state.intent == INTENT_CODE_CHANGE
    assert not state.error_code
    assert (
        "locked benchmark workspace"
        in (fixture_root / "README.md").read_text(encoding="utf-8")
    )
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_code_change_rejects_fake_completion(tmp_path):
    # 模型声称完成但 patch 参数错误 → 无写证据 → review gate 拦截
    outputs = [
        '<tool>{"name": "read_file", "args": {"path": "README.md"}}</tool>',
        '<tool>{"name": "patch_file", "args": {"path": "README.md", "old_string": "placeholder", "new_string": "locked"}}</tool>',
        "<final>Done</final>",
    ]
    agent, _, fixture_root = _build_runtime(tmp_path, outputs)
    result = run_native(
        agent,
        "Replace the placeholder.",
        task_mode=INTENT_CODE_CHANGE,
    )
    state = result.task_state
    assert state.review_status == "needs_fix"
    assert state.error_code == "completion_gate_failed"
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_auto_router_uses_router_client(tmp_path):
    # auto 模式：router 返回 code_change，主 client 执行改文件
    router_outputs = [json.dumps({"intent": "code_change", "requires_research": False})]
    main_outputs = [
        '<tool>{"name": "read_file", "args": {"path": "README.md"}}</tool>',
        '<tool>{"name": "patch_file", "args": {"path": "README.md", "old_text": "This is a placeholder benchmark fixture.", "new_text": "This fixture is a locked benchmark workspace."}}</tool>',
        "<final>Done</final>",
    ]
    agent, _, fixture_root = _build_runtime(tmp_path, main_outputs)
    router = FakeModelClient(router_outputs)
    result = run_native(
        agent,
        "Change the README placeholder.",
        task_mode="auto",
        router_model_client=router,
    )
    state = result.task_state
    assert state.status == "completed"
    assert state.intent == INTENT_CODE_CHANGE
    assert result.run_metadata["intent_source"] == "router"
    assert result.run_metadata["intent_attempts"] == 1
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_invalid_mode_and_focus_combinations(tmp_path):
    agent, _, fixture_root = _build_runtime(tmp_path, ["ignored"])
    with pytest.raises(ValueError, match="focus_paths are not valid"):
        run_native(
            agent,
            "read only",
            task_mode=INTENT_READ_ONLY,
            focus_paths=["README.md"],
        )
    with pytest.raises(ValueError, match="acceptance is only valid"):
        run_native(
            agent,
            "conversation",
            task_mode=INTENT_CONVERSATION,
            acceptance="done",
        )
    with pytest.raises(ValueError, match="router_model_client is only valid"):
        run_native(
            agent,
            "explicit",
            task_mode=INTENT_CODE_CHANGE,
            router_model_client=object(),
        )
    shutil.rmtree(fixture_root, ignore_errors=True)


def test_native_model_error_in_loop_classifies_correctly(tmp_path):
    # AgentLoop 内模型调用抛带 stop_reason=model_error 的异常时，
    # run_native 应正确分类（此前误用临时 TaskState 导致 runtime_error）。
    import urllib.error

    import pico
    from pico.evaluation.backends import ModelBoundaryError
    from pathlib import Path as _Path

    class FailingClient:
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}

        def complete(self, *args, **kwargs):
            cause = urllib.error.HTTPError(
                "https://provider.example/v1", 401, "secret body", {}, None
            )
            raise RuntimeError("provider body must not be public") from cause

    pico_root = _Path(pico.__file__).resolve().parent.parent
    source = pico_root / "tests" / "fixtures" / "bench_repo_readme"
    fixture_root = tmp_path / "model-error-workspace"
    shutil.copytree(source, fixture_root)
    agent = Pico(
        model_client=FailingClient(),
        workspace=WorkspaceContext.build(fixture_root, repo_root_override=fixture_root),
        session_store=SessionStore(fixture_root / ".pico" / "sessions"),
        run_store=RunStore(fixture_root / ".pico" / "runs"),
        approval_policy="auto",
        max_steps=6,
        allowed_tools=["read_file", "patch_file"],
        event_sink=NullSink(),
    )
    result = run_native(agent, "hello", task_mode=INTENT_CODE_CHANGE)
    state = result.task_state
    assert state.status == "failed"
    assert state.stop_reason == "model_error"
    assert state.error_code == "model_call_failed"
    shutil.rmtree(fixture_root, ignore_errors=True)

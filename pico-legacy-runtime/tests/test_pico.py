import io
import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

import pico as pico_pkg
from pico import cli as cli_module
from pico import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OpenAICompletionsModelClient,
    Pico,
    OllamaModelClient,
    OpenAICompatibleModelClient,
    SessionStore,
    WorkspaceContext,
    build_welcome,
)


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Read the file successfully.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    assert "hello.txt" in agent.session["memory"]["files"]


def test_agent_updates_task_summary_on_each_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>First pass.</final>",
            "<final>Second pass.</final>",
        ],
    )

    assert agent.ask("First request") == "First pass."
    assert agent.session["memory"]["working"]["task_summary"] == "First request"

    assert agent.ask("Second request") == "Second pass."
    assert agent.session["memory"]["working"]["task_summary"] == "Second request"


def test_agent_only_stores_reusable_epistemic_notes(tmp_path):
    (tmp_path / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"facts.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
            "<final>It is red.</final>",
        ],
    )

    assert agent.ask("Read the file and remember the fact") == "Done."
    notes = agent.session["memory"]["episodic_notes"]
    assert any("deploy key is red" in note["text"] for note in notes)
    assert not any(note["text"] == "Done." for note in notes)
    assert not any(note["text"] == "Done." for note in notes)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>It is red.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("What color is the deploy key?") == "It is red."
    prompt = resumed.model_client.prompts[-1]
    assert "Relevant memory" in prompt
    assert "deploy key is red" in prompt


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    agent.memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    agent.memory.remember_file("./sample.txt")
    assert agent.memory.to_dict()["file_summaries"]["sample.txt"]["freshness"]

    assert "sample.txt: alpha" in agent.memory.render_memory_text()
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert "sample.txt: alpha" not in resumed.memory_text()
    resumed.memory.invalidate_file_summary("sample.txt")
    assert "sample.txt" not in resumed.memory.to_dict()["file_summaries"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after retry.</final>",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    assert "Runtime control feedback:" in agent.model_client.prompts[1]
    assert "empty response" in agent.model_client.prompts[1]
    assert not any(
        "Runtime notice" in item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    )


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":"bad"}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Recovered after malformed tool output.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    assert "valid <tool> call" in agent.model_client.prompts[1]
    assert agent.current_task_state.malformed_output_recovered == 1


def test_agent_stops_after_one_protocol_repair(tmp_path):
    agent = build_agent(tmp_path, ["plain output one", "plain output two", "<final>too late</final>"])

    answer = agent.ask("Do the task")

    assert "retry_limit_reached" in answer
    assert "运行中断" in answer
    assert agent.current_task_state.stop_reason == "retry_limit_reached"
    assert agent.current_task_state.attempts == 2
    assert agent.current_task_state.malformed_output_recovered == 2
    assert len(agent.model_client.outputs) == 1


def test_response_shape_diagnostics_do_not_retain_model_text():
    diagnostics = Pico.diagnose_response_shape('{"secret":"private model response","tool":"read_file"}')

    assert diagnostics["error_code"] == "model_protocol_invalid"
    assert diagnostics["detected_format"] == "json_object"
    assert diagnostics["top_level_keys"] == ["secret", "tool"]
    assert len(diagnostics["response_hash"]) == 16
    assert "private model response" not in json.dumps(diagnostics)


def test_agent_retries_when_final_only_announces_future_work(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            "<final>我会重新沿着实际代码读一遍，先定位模型调用和工具循环。</final>",
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>检查完成：hello.txt 的第一行是 alpha。</final>",
        ],
    )

    answer = agent.ask("重新读取代码并说明结论")

    assert answer == "检查完成：hello.txt 的第一行是 alpha。"
    assert any(
        item["role"] == "tool" and item["name"] == "read_file"
        for item in agent.session["history"]
    )
    assert agent.current_task_state.malformed_output_recovered == 1


def test_agent_retries_when_unwrapped_answer_only_announces_future_work(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "I will continue to inspect the runtime before answering.",
            "<final>The runtime requires an explicit tool or final decision.</final>",
        ],
    )

    assert (
        agent.ask("Inspect the runtime")
        == "The runtime requires an explicit tool or final decision."
    )
    assert agent.current_task_state.malformed_output_recovered == 1


def test_agent_accepts_xml_write_file_tool(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer == "Done."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == 'print("hi")\n'
    assert agent.current_task_state.affected_paths == ["hello.py"]


def test_checkpoint_and_durable_memory_writes_can_be_disabled(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Decision: Keep this only in the isolated child.</final>"],
        allow_checkpoint=False,
        allow_durable_memory_write=False,
    )

    answer = agent.ask("Remember this decision.")

    assert answer == "Decision: Keep this only in the isolated child."
    assert agent.current_task_state.checkpoint_id == ""
    assert agent.session["checkpoints"]["items"] == {}
    assert agent.last_durable_promotions == []
    assert not (tmp_path / ".pico" / "memory").exists()
    trace = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    assert not any(event["event"] == "checkpoint_created" for event in trace)


def test_one_protocol_repair_does_not_consume_the_tool_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after one repair.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after one repair."
    assert agent.current_task_state.attempts == 2
    assert agent.current_task_state.tool_steps == 0


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["<final>First pass.</final>"])
    assert agent.ask("Start a session") == "First pass."

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.session["history"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_tool_is_no_longer_exposed(tmp_path):
    # §7.8.9 阶段 4：取消可调用 delegate——delegate 工具不再暴露给模型，
    # 模型调用它会被当作未知工具（retry），而不是执行子 agent。
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"inspect README","max_steps":2}}</tool>',
            "<final>Parent result.</final>",
        ],
    )

    answer = agent.ask("Use delegation")

    # delegate 不在工具面 → 模型第一轮 delegate 调用被拒绝（unknown tool），
    # 第二轮给 final → completed
    assert answer == "Parent result."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    # delegate 未执行——history 里的 delegate 条目是"unknown tool"拒绝结果
    delegate_events = [item for item in tool_events if item["name"] == "delegate"]
    assert delegate_events, "delegate call should be recorded as rejected"
    assert "unknown tool" in delegate_events[0]["content"]


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: 'path'")
    assert 'example: <tool name="write_file"' in result
    mock_input.assert_not_called()


def test_approval_prompt_displays_unicode_arguments_without_ascii_escapes(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input", return_value="y") as mock_input:
        approved = agent.approve(
            "patch_file",
            {"path": "README.md", "new_text": "中文项目介绍"},
        )

    prompt = mock_input.call_args.args[0]
    assert approved is True
    assert "中文项目介绍" in prompt
    assert "\\u4e2d" not in prompt


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".pico").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".pico" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    """写工具同参重复 → 执行前拒绝（P4 重复拦截）。

    §7.8.9 修正（2026-08-18）：只读工具不做执行前拦截（结果可能已变，
    重读合理），改为执行后结果指纹判定；写工具（write/patch/shell）仍
    执行前拦截。
    """
    agent = build_agent(tmp_path, [])
    agent.record({"role": "tool", "name": "write_file", "args": {"path": "x.txt", "content": "hi"}, "content": "ok", "created_at": "1"})
    agent.record({"role": "tool", "name": "write_file", "args": {"path": "x.txt", "content": "hi"}, "content": "ok", "created_at": "2"})

    result = agent.run_tool("write_file", {"path": "x.txt", "content": "hi"})

    assert result == "error: repeated identical tool call for write_file; choose a different tool or return a final answer"


def test_repeated_readonly_tool_call_is_not_pre_rejected(tmp_path):
    """§7.8.9 修正（2026-08-18）：只读工具重复不再执行前拦截。

    文件内容可能已变（被写工具改过），同动作重读是合理的——放行执行，
    是否算重复/坏轮由 AgentLoop 执行后结果指纹判定（见 test_agent_loop.py
    的 result-fingerprint 测试）。
    """
    agent = build_agent(tmp_path, [])
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "1"})
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "2"})

    result = agent.run_tool("list_files", {})

    assert "repeated identical tool call" not in result
    assert "[F] README.md" in result


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = tmp_path / "very" / "long" / "path" / "for" / "the" / "pico" / "agent" / "welcome" / "screen"
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "(  o o  )" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" not in welcome
    assert "pico" in welcome
    assert "local coding agent" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


def test_ollama_client_posts_expected_payload():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OllamaModelClient(
        model="qwen3.5:4b",
        host="http://127.0.0.1:11434",
        temperature=0.2,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 30
    assert captured["body"]["model"] == "qwen3.5:4b"
    assert captured["body"]["prompt"] == "hello"
    assert captured["body"]["stream"] is False


def test_openai_compatible_client_posts_expected_responses_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://right.codes/v1/responses"
    assert captured["timeout"] == 30
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["headers"]["User-agent"] == "pico/0.1"
    assert captured["body"]["stream"] is True


def test_openai_compatible_client_sends_threadforge_instructions():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        instructions="Use the ThreadForge local tool protocol.",
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        assert client.complete("hello", 42) == "<final>ok</final>"

    assert captured["body"]["instructions"] == "Use the ThreadForge local tool protocol."


def test_openai_compatible_client_sends_native_tools_and_normalizes_function_call():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "list_files",
                            "arguments": '{"path":"."}',
                            "call_id": "call_native",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    tools = [
        {
            "type": "function",
            "name": "list_files",
            "description": "List files.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42, tool_definitions=tools)

    # §2.1 原生 tool calling：客户端直接返回 dict，不再 <tool> 文本
    assert result == {"name": "list_files", "args": {"path": "."}}
    assert captured["body"]["tools"] == tools
    assert captured["body"]["parallel_tool_calls"] is False
    assert client.last_completion_metadata["native_tool_call"] is True


def test_openai_compatible_reasoning_omits_temperature_and_records_effort():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        reasoning_effort="high",
        supported_reasoning_efforts=("low", "medium", "high"),
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        assert client.complete("hello", 42) == "<final>ok</final>"

    assert captured["body"]["reasoning"] == {"effort": "high"}
    assert "temperature" not in captured["body"]
    assert client.last_completion_metadata["effective_reasoning_effort"] == "high"
    assert captured["body"] == {
        "model": "gpt-5.4",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_output_tokens": 42,
        "stream": True,
        "reasoning": {"effort": "high"},
    }


def test_openai_compatible_reasoning_none_is_explicit_and_keeps_temperature():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        reasoning_effort="none",
        supported_reasoning_efforts=("none", "low"),
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        assert client.complete("hello", 42) == "<final>ok</final>"

    assert captured["body"]["reasoning"] == {"effort": "none"}
    assert captured["body"]["temperature"] == 0.2


def test_openai_compatible_client_retries_rate_limit_without_leaking_provider_body():
    calls = 0

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>recovered</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "too many",
                {"Retry-After": "0"},
                io.BytesIO(b"private provider response"),
            )
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        max_attempts=3,
    )

    retries = []
    with patch("urllib.request.urlopen", fake_urlopen), patch("time.sleep"):
        assert client.complete("hello", 42, on_retry=retries.append) == "<final>recovered</final>"
    assert calls == 2
    assert retries == [
        {
            "attempt": 1,
            "max_attempts": 3,
            "error_code": "model_rate_limited",
            "retry_delay_seconds": 0.5,
        }
    ]


def test_openai_compatible_client_does_not_retry_past_deadline():
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "too many",
            {"Retry-After": "2"},
            io.BytesIO(b"private provider response"),
        )

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        max_attempts=3,
    )

    with patch("urllib.request.urlopen", fake_urlopen), pytest.raises(Exception) as caught:
        client.complete("hello", 42, deadline_monotonic=time.monotonic() + 0.1)

    assert calls == 1
    assert getattr(caught.value, "code", "") == "model_rate_limited"


def test_openai_compatible_client_does_not_retry_auth_error_or_expose_body():
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "unauthorized",
            {},
            io.BytesIO(b"secret response body"),
        )

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        max_attempts=3,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(Exception) as caught:
            client.complete("hello", 42)
    assert getattr(caught.value, "code", "") == "model_auth_error"
    assert getattr(caught.value, "attempts", 0) == 1
    assert "secret response body" not in str(caught.value)


def test_openai_compatible_client_sends_prompt_cache_fields_and_records_usage():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output_text": "<final>ok</final>",
                    "usage": {
                        "input_tokens": 2048,
                        "input_tokens_details": {"cached_tokens": 1536},
                        "output_tokens": 32,
                        "total_tokens": 2080,
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete(
            "hello",
            42,
            prompt_cache_key="prefix-hash-123",
            prompt_cache_retention="in_memory",
        )

    assert result == "<final>ok</final>"
    assert captured["body"]["prompt_cache_key"] == "prefix-hash-123"
    assert captured["body"]["prompt_cache_retention"] == "in_memory"
    assert client.last_completion_metadata["prompt_cache_supported"] is True
    assert client.last_completion_metadata["cached_tokens"] == 1536
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["input_tokens"] == 2048


def test_openai_compatible_client_extracts_text_from_event_stream():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"type":"response.created","response":{"id":"resp_1","output":[]}}\n'
                'data: {"type":"response.completed","response":{"output":[{"content":[{"text":"<final>stream ok</final>"}]}],"usage":{"input_tokens":12,"output_tokens":4,"total_tokens":16}}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>stream ok</final>"
    assert client.last_completion_metadata["total_tokens"] == 16


def test_openai_compatible_client_extracts_function_call_from_event_stream():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"type":"response.created","response":{"id":"resp_1","output":[]}}\n'
                'data: {"type":"response.completed","response":{"output":[{"type":"function_call","name":"read_file","arguments":"{\\"path\\":\\"README.md\\",\\"start\\":1,\\"end\\":20}","call_id":"call_1"}],"usage":{"input_tokens":12,"output_tokens":4,"total_tokens":16}}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42, tool_definitions=[{"type": "function"}])

    # §2.1 原生 tool calling：客户端直接返回 dict
    assert result == {"name": "read_file", "args": {"path": "README.md", "start": 1, "end": 20}}
    assert client.last_completion_metadata["native_tool_call"] is True


def test_openai_compatible_client_treats_native_plain_text_as_final_answer():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"type":"response.output_text.delta","delta":"项目"}\n'
                'data: {"type":"response.output_text.delta","delta":"总结"}\n'
                'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"项目总结"}]}],"usage":{"input_tokens":12,"output_tokens":4,"total_tokens":16}}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompatibleModelClient(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42, tool_definitions=[{"type": "function"}])

    assert result == "<final>项目总结</final>"
    assert client.last_completion_metadata["native_text_response"] is True


def test_openai_compatible_client_extracts_text_from_event_stream_deltas():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"<final>"}\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"OK"}\n'
                'event: response.output_text.done\n'
                'data: {"type":"response.output_text.done","text":"<final>OK</final>"}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    deltas = []
    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42, on_text_delta=deltas.append)

    assert result == "<final>OK</final>"
    assert "".join(deltas) == result


def test_anthropic_compatible_client_posts_expected_messages_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "<final>ok</final>",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://www.right.codes/claude-aws/v1/messages"
    assert captured["timeout"] == 30
    assert captured["headers"]["X-api-key"] == "sk-test"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "claude-sonnet-4-5-20250929",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_tokens": 42,
        "stream": True,
        "temperature": 0.2,
    }


def test_anthropic_compatible_client_extracts_first_text_block():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "<final>ok</final>"},
                    ]
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"


def test_build_agent_uses_openai_provider_and_model_override(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "openai",
            "model": "override-model",
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_BASE": "https://www.right.codes/codex/v1",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_MODEL": "env-model",
        },
        clear=False,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = pico_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["model"] == "override-model"
    assert mock_openai.call_args.kwargs["base_url"] == "https://www.right.codes/codex/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "sk-test"
    assert agent.model_client is fake_client


def test_build_agent_uses_right_codes_shared_key_for_openai_provider(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "openai",
            "model": None,
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(os.environ, {"PICO_RIGHT_CODES_API_KEY": "sk-right-codes"}, clear=True):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = pico_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["api_key"] == "sk-right-codes"
    assert agent.model_client is fake_client


def test_build_arg_parser_defaults_provider_to_deepseek(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    assert args.provider == "deepseek"


def test_build_model_client_applies_router_model_and_temperature_overrides(monkeypatch, tmp_path):
    captured = []

    def fake_client(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(cli_module, "OllamaModelClient", fake_client)
    monkeypatch.setattr(cli_module, "OpenAICompatibleModelClient", fake_client)
    monkeypatch.setattr(cli_module, "AnthropicCompatibleModelClient", fake_client)
    monkeypatch.setattr(cli_module, "OpenAICompletionsModelClient", fake_client)

    for provider in ("ollama", "openai", "anthropic", "deepseek", "chat_completions"):
        args = pico_pkg.build_arg_parser().parse_args(
            ["--cwd", str(tmp_path), "--provider", provider, "--temperature", "0.7"]
        )
        cli_module._build_model_client(
            args,
            model_override="router-model",
            temperature_override=0.0,
        )

    assert [item["model"] for item in captured] == ["router-model"] * 5
    assert [item["temperature"] for item in captured] == [0.0] * 5


def test_build_arg_parser_accepts_anthropic_provider(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "anthropic"])

    assert args.provider == "anthropic"


def test_build_arg_parser_accepts_deepseek_provider(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])

    assert args.provider == "deepseek"


def test_build_agent_uses_anthropic_provider_and_openai_key_fallback(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "anthropic",
            "model": "claude-sonnet-4-5-20250929",
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "sk-openai-fallback",
        },
        clear=True,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "claude-sonnet-4-5-20250929"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://www.right.codes/claude/v1"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-openai-fallback"
    assert agent.model_client is fake_client


def test_build_agent_uses_anthropic_default_model_when_env_is_missing(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "anthropic"])

    with patch.dict(
        os.environ,
        {},
        clear=False,
    ):
        os.environ.pop("ANTHROPIC_MODEL", None)
        with patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            pico_pkg.build_agent(args)

    assert mock_anthropic.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_build_agent_uses_deepseek_provider_and_env_configuration(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic",
                "PICO_DEEPSEEK_API_KEY=sk-project-deepseek",
                "PICO_DEEPSEEK_MODEL=deepseek-v4-pro",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "deepseek",
            "model": None,
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_BASE": "https://legacy.deepseek.example/anthropic",
            "DEEPSEEK_API_KEY": "sk-legacy-deepseek",
            "DEEPSEEK_MODEL": "legacy-deepseek-model",
            "ANTHROPIC_API_KEY": "sk-anthropic",
            "OPENAI_API_KEY": "sk-openai",
        },
        clear=True,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-project-deepseek"
    assert agent.model_client is fake_client


def test_build_agent_uses_anthropic_key_fallback_for_deepseek_provider(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])

    with patch.dict(
        os.environ,
        {"PICO_ANTHROPIC_API_KEY": "sk-anthropic-shared", "OPENAI_API_KEY": "sk-openai"},
        clear=True,
    ):
        with patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            pico_pkg.build_agent(args)

    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-anthropic-shared"


def test_build_agent_uses_deepseek_default_model_when_env_is_missing(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-deepseek"}, clear=True):
        with patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            pico_pkg.build_agent(args)

    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"


def test_build_arg_parser_accepts_chat_completions_provider(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "chat_completions"])

    assert args.provider == "chat_completions"


def test_build_agent_uses_chat_completions_provider_and_env_configuration(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_CHAT_COMPLETIONS_API_BASE=https://api.siliconflow.cn/v1",
                "PICO_CHAT_COMPLETIONS_API_KEY=sk-project-siliconflow",
                "PICO_CHAT_COMPLETIONS_MODEL=deepseek-ai/DeepSeek-V3.2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "chat_completions",
            "model": None,
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "CHAT_COMPLETIONS_API_BASE": "https://legacy.chat.example/v1",
            "SILICONFLOW_API_KEY": "sk-legacy-siliconflow",
            "DEEPSEEK_API_KEY": "sk-deepseek",
            "ANTHROPIC_API_KEY": "sk-anthropic",
            "OPENAI_API_KEY": "sk-openai",
        },
        clear=True,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch(
            "pico.cli.AnthropicCompatibleModelClient",
            side_effect=AssertionError("anthropic client should not be used"),
        ), patch("pico.cli.OpenAICompletionsModelClient") as mock_completions:
            fake_client = mock_completions.return_value
            agent = pico_pkg.build_agent(args)

    mock_completions.assert_called_once()
    assert mock_completions.call_args.kwargs["model"] == "deepseek-ai/DeepSeek-V3.2"
    assert mock_completions.call_args.kwargs["base_url"] == "https://api.siliconflow.cn/v1"
    assert mock_completions.call_args.kwargs["api_key"] == "sk-project-siliconflow"
    assert agent.model_client is fake_client


def test_build_agent_uses_siliconflow_shared_key_for_chat_completions_provider(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "chat_completions",
            "model": None,
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-siliconflow"}, clear=True):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch(
            "pico.cli.AnthropicCompatibleModelClient",
            side_effect=AssertionError("anthropic client should not be used"),
        ), patch("pico.cli.OpenAICompletionsModelClient") as mock_completions:
            fake_client = mock_completions.return_value
            agent = pico_pkg.build_agent(args)

    mock_completions.assert_called_once()
    assert mock_completions.call_args.kwargs["api_key"] == "sk-siliconflow"
    assert agent.model_client is fake_client


def test_build_agent_uses_chat_completions_default_model_when_env_is_missing(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "chat_completions"])

    with patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-siliconflow"}, clear=True):
        with patch("pico.cli.OpenAICompletionsModelClient") as mock_completions:
            pico_pkg.build_agent(args)

    assert mock_completions.call_args.kwargs["model"] == "deepseek-ai/DeepSeek-V3.2"
    assert mock_completions.call_args.kwargs["base_url"] == "https://api.siliconflow.cn/v1"


def test_build_agent_uses_chat_completions_explicit_model_override(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--provider", "chat_completions", "--model", "custom-model"]
    )

    with patch.dict(
        os.environ,
        {"SILICONFLOW_API_KEY": "sk-siliconflow", "PICO_CHAT_COMPLETIONS_MODEL": "env-model"},
        clear=True,
    ):
        with patch("pico.cli.OpenAICompletionsModelClient") as mock_completions:
            pico_pkg.build_agent(args)

    assert mock_completions.call_args.kwargs["model"] == "custom-model"


def test_build_agent_uses_deepseek_provider_by_default(tmp_path):
    args = pico_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_BASE": "https://api.deepseek.com/anthropic",
            "DEEPSEEK_API_KEY": "sk-test",
        },
        clear=False,
    ):
        with patch(
            "pico.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "pico.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("pico.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = pico_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-test"
    assert agent.model_client is fake_client


def test_successful_run_persists_run_artifacts_and_stop_reason(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Finished.</final>",
        ],
    )

    assert agent.ask("Do the thing") == "Finished."

    runs_root = tmp_path / ".pico" / "runs"
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1

    run_dir = run_dirs[0]
    task_state = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()

    assert task_state["task_id"] != task_state["run_id"]
    assert run_dir.name == task_state["run_id"]
    assert (run_dir / "task_state.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "report.json").exists()
    assert task_state["stop_reason"] == "final_answer_returned"
    assert task_state["final_answer"] == "Finished."
    assert report["stop_reason"] == "final_answer_returned"
    assert report["task_state"]["stop_reason"] == "final_answer_returned"
    assert report["run_id"] == task_state["run_id"]
    trace_events = [json.loads(line)["event"] for line in trace_lines]
    assert trace_events[0] == "run_started"
    assert trace_events[-1] == "run_finished"
    assert trace_events.count("prompt_built") == 2
    assert "tool_executed" in trace_events


def test_trace_and_report_redact_secret_env_values(tmp_path, python_shell_command):
    secret = "sk-test-secret-123"
    command = python_shell_command(f"print({secret!r})")
    tool_call = json.dumps(
        {
            "name": "run_shell",
            "args": {"command": command, "timeout": 20},
        }
    )
    with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False):
        agent = build_agent(
            tmp_path,
            [
                f"<tool>{tool_call}</tool>",
                "<final>Masked.</final>",
            ],
        )

        assert agent.ask("Mask the secret") == "Masked."

    runs_root = tmp_path / ".pico" / "runs"
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1

    run_dir = run_dirs[0]
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")
    trace_events = [json.loads(line) for line in trace_text.splitlines()]

    assert secret not in trace_text
    assert secret not in report_text

    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events
    assert prompt_events[0]["prompt_metadata"]["secret_env_count"] >= 1
    assert "OPENAI_API_KEY" in prompt_events[0]["prompt_metadata"]["secret_env_names"]

    tool_events = [event for event in trace_events if event["event"] == "tool_executed"]
    assert tool_events
    assert "<redacted>" in tool_events[0]["args"]["command"]
    assert "<redacted>" in tool_events[0]["result"]


def test_prompt_budget_metadata_records_budget_decisions(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    agent.memory.append_note("alpha episodic note " + ("A" * 120), tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("beta episodic recall note " + ("B" * 120), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("gamma episodic note " + ("C" * 120), tags=("recall",), created_at="2026-04-07T10:02:00+00:00")

    for index in range(4):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("A" * 240),
                "created_at": f"2026-04-07T10:0{index}:00+00:00",
            }
        )

    agent.context_manager.total_budget = 1000
    agent.context_manager.section_budgets = {
        "prefix": 80,
        "memory": 80,
        "relevant_memory": 80,
        "history": 80,
    }

    assert agent.ask("recall") == "Done."

    trace_events = [
        json.loads(line)
        for line in (agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines())
    ]
    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events
    metadata = prompt_events[0]["prompt_metadata"]
    relevant_section = agent.model_client.prompts[0].split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ")]) == 3
    assert "alpha episodic" in relevant_section
    assert "beta episodic" in relevant_section
    assert "gamma episodic" in relevant_section
    assert metadata["current_request"]["text"] == "recall"
    assert metadata["current_request"]["rendered_chars"] == len("recall")


def test_prompt_metadata_refreshes_prefix_when_workspace_changes(tmp_path):
    agent = build_agent(tmp_path, [])

    first = agent.prompt_metadata("first", "")
    second = agent.prompt_metadata("second", "")

    assert first["prefix_hash"] == second["prefix_hash"]
    assert second["prefix_changed"] is False
    assert second["workspace_changed"] is False

    (tmp_path / "README.md").write_text("demo changed\n", encoding="utf-8")

    third = agent.prompt_metadata("third", "")

    assert third["prefix_hash"] != second["prefix_hash"]
    assert third["prefix_changed"] is True
    assert third["workspace_changed"] is True
    assert "demo changed" in agent.prefix


def test_agent_creates_checkpoint_when_context_reduction_happens_and_artifacts_only_reference_it(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done after checkpoint.</final>"])
    for index in range(10):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("A" * 260),
                "created_at": f"2026-04-07T10:{index:02d}:00+00:00",
            }
        )
    agent.memory.append_note("checkpoint note " + ("B" * 220), tags=("checkpoint",), created_at="2026-04-07T11:00:00+00:00")
    agent.context_manager.total_budget = 900
    agent.context_manager.section_budgets = {
        "prefix": 120,
        "memory": 120,
        "relevant_memory": 120,
        "history": 160,
    }

    assert agent.ask("Resume the long task") == "Done after checkpoint."

    checkpoint_state = agent.session["checkpoints"]
    checkpoint = checkpoint_state["items"][checkpoint_state["current_id"]]
    assert checkpoint["checkpoint_id"] == checkpoint_state["current_id"]
    assert checkpoint["schema_version"] == "phase1-v1"
    assert checkpoint["current_goal"] == "Resume the long task"
    assert checkpoint["key_files"] == []
    assert checkpoint["current_blocker"] == ""
    assert checkpoint["next_step"]

    task_state = json.loads(agent.run_store.task_state_path(agent.current_task_state).read_text(encoding="utf-8"))
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]

    assert task_state["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert report["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert report["task_state"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert "current_goal" not in task_state
    assert "current_goal" not in report
    checkpoint_events = [event for event in trace_events if event["event"] == "checkpoint_created"]
    assert checkpoint_events
    assert checkpoint_events[-1]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert "current_goal" not in checkpoint_events[-1]


def test_resume_prompt_uses_checkpoint_state_not_just_history(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_manual",
        "items": {
            "ckpt_manual": {
                "checkpoint_id": "ckpt_manual",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix failing resume flow",
                "completed": ["Read runtime.py"],
                "excluded": ["Do not add branch summary"],
                "current_blocker": "Need to re-anchor stale file facts",
                "next_step": "Re-read runtime.py and refresh the checkpoint",
                "key_files": [{"path": "runtime.py", "freshness": "abc"}],
                "freshness": {"runtime.py": "abc"},
                "summary": "Resume from the latest checkpoint",
                "runtime_identity": {"workspace_fingerprint": "old-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."

    prompt = resumed.model_client.prompts[-1]
    assert "Task checkpoint:" in prompt
    assert "Current goal: Fix failing resume flow" in prompt
    assert "Current blocker: Need to re-anchor stale file facts" in prompt
    assert "Next step: Re-read runtime.py and refresh the checkpoint" in prompt


def test_resume_invalidates_stale_file_summaries_and_marks_partial_stale(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.memory.set_file_summary("runtime.py", "runtime.py: alpha")
    freshness = agent.memory.to_dict()["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_stale",
        "items": {
            "ckpt_stale": {
                "checkpoint_id": "ckpt_stale",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix stale summary handling",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py is important",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."

    assert "runtime.py" not in resumed.memory.to_dict()["file_summaries"]
    assert resumed.last_prompt_metadata["resume_status"] == "partial-stale"
    assert resumed.last_prompt_metadata["stale_summary_invalidations"] == 1


def test_run_shell_nonzero_with_workspace_change_is_recorded_as_partial_success(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    assert "exit_code: 1" in result
    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert agent._last_tool_result_metadata["affected_paths"] == ["README.md"]
    assert agent._last_tool_result_metadata["workspace_changed"] is True


def test_resume_marks_workspace_mismatch_when_checkpoint_runtime_identity_is_stale(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_workspace",
        "items": {
            "ckpt_workspace": {
                "checkpoint_id": "ckpt_workspace",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after drift",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime state",
                "key_files": [],
                "freshness": {},
                "summary": "workspace changed",
                "runtime_identity": {"workspace_fingerprint": "outdated-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "workspace-mismatch"


def test_write_file_trace_records_minimum_tool_contract_fields(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"notes.txt","content":"hello\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )

    assert agent.ask("Create notes.txt") == "Done."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    tool_event = [event for event in trace_events if event["event"] == "tool_executed"][-1]

    assert tool_event["name"] == "write_file"
    assert tool_event["risk_level"] == "high"
    assert tool_event["read_only"] is False
    assert tool_event["tool_status"] == "ok"
    assert tool_event["affected_paths"] == ["notes.txt"]
    assert tool_event["workspace_changed"] is True
    assert tool_event["diff_summary"] == ["created:notes.txt"]


def test_resume_marks_schema_mismatch_when_checkpoint_version_is_incompatible(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_schema",
        "items": {
            "ckpt_schema": {
                "checkpoint_id": "ckpt_schema",
                "parent_checkpoint_id": "",
                "schema_version": "legacy-v0",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after schema change",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Migrate checkpoint",
                "key_files": [],
                "freshness": {},
                "summary": "schema changed",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "schema-mismatch"


def test_resume_marks_no_checkpoint_when_session_has_no_checkpoint_state(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session.pop("checkpoints", None)
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "no-checkpoint"
    assert "Task checkpoint:" not in resumed.model_client.prompts[-1]


def test_freshness_mismatch_creates_checkpoint_before_model_completion(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>Resumed.</final>"])
    agent.memory.set_file_summary("runtime.py", "runtime.py: alpha")
    freshness = agent.memory.to_dict()["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_freshness",
        "items": {
            "ckpt_freshness": {
                "checkpoint_id": "ckpt_freshness",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Handle freshness mismatch",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py changed",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    assert agent.ask("Continue the task") == "Resumed."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    checkpoint_events = [event for event in trace_events if event["event"] == "checkpoint_created"]

    assert checkpoint_events
    assert checkpoint_events[0]["trigger"] == "freshness_mismatch"


def test_runtime_identity_persists_key_execution_metadata(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient(["<final>Done.</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="never",
        max_steps=9,
        max_new_tokens=1024,
        feature_flags={"memory": True, "relevant_memory": False},
    )

    runtime_identity = agent.session["runtime_identity"]

    assert runtime_identity["session_id"] == agent.session["id"]
    assert runtime_identity["cwd"] == str(tmp_path)
    assert runtime_identity["approval_policy"] == "never"
    assert runtime_identity["read_only"] is False
    assert runtime_identity["max_steps"] == 9
    assert runtime_identity["max_new_tokens"] == 1024
    assert runtime_identity["feature_flags"]["memory"] is True
    assert runtime_identity["feature_flags"]["relevant_memory"] is False
    assert runtime_identity["shell_env_allowlist"] == list(agent.shell_env_allowlist)


def test_resume_records_runtime_identity_mismatch_fields_in_metadata_and_trace(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_identity",
        "items": {
            "ckpt_identity": {
                "checkpoint_id": "ckpt_identity",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Resume with a different runtime identity",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime identity",
                "key_files": [],
                "freshness": {},
                "summary": "identity changed",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint(),
                    "approval_policy": "auto",
                    "read_only": False,
                    "max_steps": 6,
                    "max_new_tokens": 512,
                    "model": "old-model",
                    "model_client": "FakeModelClient",
                    "feature_flags": {"memory": True, "relevant_memory": True},
                    "shell_env_allowlist": ["PATH"],
                    "session_id": agent.session["id"],
                    "cwd": str(tmp_path),
                },
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="never",
        max_steps=9,
        max_new_tokens=1024,
        feature_flags={"memory": True, "relevant_memory": False},
    )

    resumed.ask("Continue the task")

    assert resumed.last_prompt_metadata["resume_status"] == "workspace-mismatch"
    assert resumed.last_prompt_metadata["runtime_identity_mismatch_fields"] == [
        "approval_policy",
        "feature_flags",
        "max_new_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]

    trace_events = [
        json.loads(line)
        for line in resumed.run_store.trace_path(resumed.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    mismatch_events = [event for event in trace_events if event["event"] == "runtime_identity_mismatch"]
    assert mismatch_events
    assert mismatch_events[0]["fields"] == [
        "approval_policy",
        "feature_flags",
        "max_new_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]


def test_partial_success_creates_process_note_for_exploration_history(tmp_path):
    agent = build_agent(tmp_path, [])

    agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    process_notes = [
        note
        for note in agent.memory.to_dict()["episodic_notes"]
        if note.get("kind") == "process"
    ]

    assert process_notes
    assert process_notes[-1]["text"] == "run_shell partial_success on README.md; inspect diff before retry"
    assert "partial_success" in process_notes[-1]["tags"]
    assert "README.md" in process_notes[-1]["tags"]


def test_explicit_memory_promotion_persists_durable_memory_topics(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project convention: Use constrained tools instead of guessing.\n"
            "Project convention: Preserve local agent state under .pico/.\n"
            "Decision: Keep durable memory topic-based and lightweight.</final>",
        ],
    )

    answer = agent.ask(
        "Capture the stable facts you already discovered as durable memory. "
        "Respond with exactly the long-term facts."
    )

    assert "Project convention:" in answer

    index_path = tmp_path / ".pico" / "memory" / "MEMORY.md"
    conventions_path = tmp_path / ".pico" / "memory" / "topics" / "project-conventions.md"
    decisions_path = tmp_path / ".pico" / "memory" / "topics" / "key-decisions.md"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))

    assert index_path.exists()
    assert conventions_path.exists()
    assert decisions_path.exists()
    assert "project-conventions" in index_path.read_text(encoding="utf-8")
    assert "Use constrained tools instead of guessing." in conventions_path.read_text(encoding="utf-8")
    assert "Keep durable memory topic-based and lightweight." in decisions_path.read_text(encoding="utf-8")
    assert report["durable_promotions"] == [
        "project-conventions: Use constrained tools instead of guessing.",
        "project-conventions: Preserve local agent state under .pico/.",
        "key-decisions: Keep durable memory topic-based and lightweight.",
    ]


def test_explicit_memory_promotion_supports_chinese_intent_and_labels(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>项目约定：优先使用受约束工具，不要靠猜。\n"
            "决策：持久记忆保持轻量、按 topic 管理。</final>",
        ],
    )

    answer = agent.ask("请把下面这些稳定事实记住，作为长期记忆保存下来。")

    assert "项目约定：" in answer

    conventions_path = tmp_path / ".pico" / "memory" / "topics" / "project-conventions.md"
    decisions_path = tmp_path / ".pico" / "memory" / "topics" / "key-decisions.md"

    assert "优先使用受约束工具，不要靠猜。" in conventions_path.read_text(encoding="utf-8")
    assert "持久记忆保持轻量、按 topic 管理。" in decisions_path.read_text(encoding="utf-8")


def test_explicit_memory_promotion_rejects_secret_shaped_and_transient_lines(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project convention: Use constrained tools instead of guessing.\n"
            "Dependency: API key is sk-live-secret-abc.\n"
            "Decision: Current goal is fix flaky tests.\n"
            "Dependency: stdout: FAIL test_one FAIL test_two FAIL test_three.</final>",
        ],
    )

    agent.ask("Capture these stable facts into durable memory.")

    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    conventions_path = tmp_path / ".pico" / "memory" / "topics" / "project-conventions.md"
    dependency_path = tmp_path / ".pico" / "memory" / "topics" / "dependency-facts.md"

    assert report["durable_promotions"] == [
        "project-conventions: Use constrained tools instead of guessing.",
    ]
    assert report["durable_rejections"] == [
        "dependency-facts:secret_shaped",
        "key-decisions:transient_task_state",
        "dependency-facts:noisy_output",
    ]
    assert "Use constrained tools instead of guessing." in conventions_path.read_text(encoding="utf-8")
    assert not dependency_path.exists()


def test_explicit_memory_promotion_supersedes_matching_durable_fact(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Dependency: Python runtime is 3.11.</final>",
            "<final>Dependency: Python runtime is 3.12.</final>",
        ],
    )

    assert agent.ask("Capture this stable dependency fact into durable memory.") == "Dependency: Python runtime is 3.11."
    assert agent.ask("Save the updated dependency fact into durable memory.") == "Dependency: Python runtime is 3.12."

    dependency_path = tmp_path / ".pico" / "memory" / "topics" / "dependency-facts.md"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    text = dependency_path.read_text(encoding="utf-8")

    assert "Python runtime is 3.12." in text
    assert "Python runtime is 3.11." not in text
    assert report["durable_superseded"] == [
        "dependency-facts: Python runtime is 3.11. -> Python runtime is 3.12.",
    ]


def test_explicit_memory_promotion_dedupes_duplicate_durable_note(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project convention: Use constrained tools instead of guessing.</final>",
            "<final>Project convention: Use constrained tools instead of guessing.</final>",
        ],
    )

    agent.ask("Capture the stable fact into durable memory.")
    agent.ask("Capture the stable fact into durable memory again.")

    conventions_path = tmp_path / ".pico" / "memory" / "topics" / "project-conventions.md"
    text = conventions_path.read_text(encoding="utf-8")

    assert text.count("Use constrained tools instead of guessing.") == 1


def test_agent_records_model_cache_metadata_in_last_prompt_metadata(tmp_path):
    class CacheAwareFakeModelClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.last_completion_metadata = {
                "prompt_cache_supported": True,
                "cached_tokens": 512,
                "cache_hit": True,
                "input_tokens": 1024,
            }
            return super().complete(prompt, max_new_tokens, **kwargs)

    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=CacheAwareFakeModelClient(["<final>Done.</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    assert agent.ask("Cache aware run") == "Done."

    assert agent.last_prompt_metadata["prompt_cache_supported"] is True
    assert agent.last_prompt_metadata["cached_tokens"] == 512
    assert agent.last_prompt_metadata["cache_hit"] is True
    assert agent.last_prompt_metadata["prefix_hash"]
    assert agent.last_prompt_metadata["prompt_cache_key"] == agent.last_prompt_metadata["prefix_hash"]


def test_recent_transcript_entries_stay_richer_than_older_ones(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    old_text = "OLD-" + ("A" * 320)
    recent_text = "RECENT-" + ("B" * 320)

    agent.record({"role": "user", "content": old_text, "created_at": "2026-04-07T09:00:00+00:00"})
    agent.record({"role": "assistant", "content": old_text, "created_at": "2026-04-07T09:01:00+00:00"})
    agent.record({"role": "user", "content": recent_text, "created_at": "2026-04-07T09:02:00+00:00"})
    agent.record({"role": "assistant", "content": recent_text, "created_at": "2026-04-07T09:03:00+00:00"})
    agent.record({"role": "user", "content": recent_text, "created_at": "2026-04-07T09:04:00+00:00"})
    agent.record({"role": "assistant", "content": recent_text, "created_at": "2026-04-07T09:05:00+00:00"})
    agent.record({"role": "user", "content": recent_text, "created_at": "2026-04-07T09:06:00+00:00"})
    agent.record({"role": "assistant", "content": recent_text, "created_at": "2026-04-07T09:07:00+00:00"})

    assert agent.ask("Check the transcript") == "Done."

    prompt = agent.model_client.prompts[-1]

    assert recent_text in prompt
    assert old_text not in prompt


def test_public_api_exports_resolve_through_package_path():
    assert callable(build_welcome)
    assert FakeModelClient is not None
    assert Pico is not None
    assert OllamaModelClient is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert Path(pico_pkg.__file__).as_posix().endswith("/pico/__init__.py")


def test_reviewer_skeleton_docs_exist():
    review_pack = Path("docs/review-pack/README.md")
    architecture = Path("docs/architecture/agent-harness-v1-overview.md")

    assert review_pack.exists()
    assert architecture.exists()

    review_text = review_pack.read_text(encoding="utf-8")
    assert "Project pitch" in review_text
    assert "Architecture map" in review_text
    assert "Benchmark evidence" in review_text
    assert "Sample run artifact list" in review_text

    architecture_text = architecture.read_text(encoding="utf-8")
    assert "Agent Harness v1" in architecture_text
    assert "task state" in architecture_text.lower()


def test_package_import_surface_includes_cli_entrypoints():
    assert callable(pico_pkg.main)
    assert callable(pico_pkg.build_agent)
    assert callable(pico_pkg.build_arg_parser)


def test_module_execution_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "pico", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


# ---------------------------------------------------------------------------
# AnthropicCompatibleModelClient（完整版：流式 + 工具调用 + usage）
# ---------------------------------------------------------------------------


def test_anthropic_compatible_client_streams_and_emits_text_deltas():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":0}}}\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n'
                'data: {"type":"content_block_stop","index":0}\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n'
                'data: {"type":"message_stop"}\n'
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    deltas = []

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42, on_text_delta=deltas.append)

    assert result == "hello"
    assert "".join(deltas) == "hello"
    assert client.last_completion_metadata["input_tokens"] == 12
    assert client.last_completion_metadata["output_tokens"] == 4


def test_anthropic_compatible_client_accumulates_tool_use_across_chunks():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            # input_json_delta 分 3 片，参数 JSON 在 `{` 中间被切断。
            body = (
                'data: {"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":0}}}\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file","input":{}}}\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"pa"}}\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"th\\":\\"README.md\\"}"}}\n'
                'data: {"type":"content_block_stop","index":0}\n'
                'data: {"type":"message_stop"}\n'
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete(
            "hello", 42, tool_definitions=[{"type": "function", "name": "read_file"}]
        )

    # §2.1 原生 tool calling：客户端直接返回 dict
    assert result == {"name": "read_file", "args": {"path": "README.md"}}
    assert client.last_completion_metadata["native_tool_call"] is True


def test_anthropic_compatible_client_maps_openai_tools_to_input_schema():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    tool_definitions = [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {}, "strict": True},
        }
    ]

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete("hello", 42, tool_definitions=tool_definitions)

    assert captured["body"]["stream"] is True
    assert captured["body"]["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}, "strict": True},
        }
    ]
    assert "parallel_tool_calls" not in captured["body"]


def test_anthropic_compatible_client_tool_precedence_over_text():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"type":"message_start","message":{"usage":{"input_tokens":3,"output_tokens":0}}}\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"let me check"}}\n'
                'data: {"type":"content_block_stop","index":0}\n'
                'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"list_dir","input":{}}}\n'
                'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{}"}}\n'
                'data: {"type":"content_block_stop","index":1}\n'
                'data: {"type":"message_stop"}\n'
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    # §2.1 原生 tool calling：客户端直接返回 dict
    assert result == {"name": "list_dir", "args": {}}
    assert client.last_completion_metadata["native_tool_call"] is True


def test_anthropic_compatible_client_retries_rate_limit_and_respects_deadline():
    from pico.providers.clients import ModelProviderError

    calls = 0

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "too many",
                {"Retry-After": "0"},
                io.BytesIO(b"private provider response"),
            )
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        max_attempts=3,
    )
    retries = []

    with patch("urllib.request.urlopen", fake_urlopen), patch("time.sleep"):
        result = client.complete("hello", 42, on_retry=retries.append)

    assert result == "ok"
    assert calls == 2
    assert retries == [
        {
            "attempt": 1,
            "max_attempts": 3,
            "error_code": "model_rate_limited",
            "retry_delay_seconds": 0.5,
        }
    ]

    # deadline 临近时不再重试：429 的延迟超过 deadline -> 抛映射后的错误。
    calls = 0

    def raise_429(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "too many",
            {"Retry-After": "2"},
            io.BytesIO(b"private provider response"),
        )

    with patch("urllib.request.urlopen", raise_429), pytest.raises(ModelProviderError) as exc_info:
        client.complete("hello", 42, deadline_monotonic=time.monotonic() + 0.01)

    assert calls == 1
    assert exc_info.value.code == "model_rate_limited"
    assert exc_info.value.attempts == 1
    assert client.last_completion_metadata["provider_request_attempts"] == 1


def test_anthropic_compatible_client_raises_provider_error_on_sse_error_event():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'event: error\n'
                'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}\n'
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    from pico.providers.clients import ModelProviderError

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(ModelProviderError) as exc_info:
            client.complete("hello", 42)

    assert exc_info.value.code == "model_provider_error"
    assert client.last_completion_metadata["provider_error_code"] == "model_provider_error"


def test_anthropic_compatible_client_uses_instructions_as_system():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        instructions="You are a file assistant",
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete("hello", 42)

    assert captured["body"]["system"] == [{"type": "text", "text": "You are a file assistant"}]


def test_anthropic_compatible_client_normalizes_plain_text_to_final():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"type":"message_start","message":{"usage":{"input_tokens":3,"output_tokens":0}}}\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"done"}}\n'
                'data: {"type":"content_block_stop","index":0}\n'
                'data: {"type":"message_stop"}\n'
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = AnthropicCompatibleModelClient(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete(
            "hello", 42, tool_definitions=[{"type": "function", "name": "read_file"}]
        )

    assert result == "<final>done</final>"
    assert client.last_completion_metadata["native_text_response"] is True


# ---------------------------------------------------------------------------
# OpenAICompletionsModelClient（chat/completions：流式 + 工具调用 + usage）
# ---------------------------------------------------------------------------


def test_chat_completions_client_posts_expected_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "<final>ok</final>"}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://api.siliconflow.cn/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["body"] == {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 42,
        "stream": True,
        "temperature": 0.2,
    }
    assert "stream_options" not in captured["body"]

    # base_url 已带完整 /chat/completions 时不重复追加。
    client2 = OpenAICompletionsModelClient(
        model="m",
        base_url="https://api.siliconflow.cn/v1/chat/completions",
        api_key="",
        temperature=None,
        timeout=30,
    )
    assert client2.base_url.endswith("/v1/chat/completions")
    assert client2.base_url.count("/chat/completions") == 1


def test_chat_completions_client_accumulates_tool_call_arguments_by_index():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read_file","arguments":""}}]}}]}\n'
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\"README.md\\""}}]}}]}\n'
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}\n'
                'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"name":"list_dir","arguments":"{}"}}]}}]}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete(
            "hello", 42, tool_definitions=[{"type": "function", "name": "read_file"}]
        )

    # §2.1 原生 tool calling：客户端直接返回 dict
    assert result == {"name": "read_file", "args": {"path": "README.md"}}
    assert client.last_completion_metadata["native_tool_call"] is True


def test_chat_completions_client_uses_usage_from_last_chunk():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
                'data: {"choices":[{"delta":{"content":""}}],"usage":{"prompt_tokens":9,"completion_tokens":3,"total_tokens":12}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "hi"
    assert client.last_completion_metadata["input_tokens"] == 9
    assert client.last_completion_metadata["output_tokens"] == 3


def test_chat_completions_client_streams_text_and_final_wraps():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            body = (
                'data: {"choices":[{"delta":{"content":"fin"}}]}\n'
                'data: {"choices":[{"delta":{"content":"al"}}]}\n'
                "data: [DONE]\n"
            ).encode("utf-8")
            return iter(body.splitlines(keepends=True))

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    deltas = []

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete(
            "hello", 42, on_text_delta=deltas.append,
            tool_definitions=[{"type": "function", "name": "read_file"}],
        )

    assert result == "<final>final</final>"
    assert "".join(deltas) == "final"
    assert client.last_completion_metadata["native_text_response"] is True


def test_chat_completions_client_json_fallback_and_retry():
    from pico.providers.clients import ModelProviderError

    captured = {"attempts": 0}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"README.md"}',
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["attempts"] += 1
        return FakeResponse()

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete(
            "hello", 42, tool_definitions=[{"type": "function", "name": "read_file"}]
        )

    # §2.1 原生 tool calling：客户端直接返回 dict
    assert result == {"name": "read_file", "args": {"path": "README.md"}}
    assert client.last_completion_metadata["input_tokens"] == 5

    # 非 JSON body -> model_response_invalid（retryable，重试耗尽后抛）。
    class FakeBadResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"not json at all"

    client.max_attempts = 1
    with patch("urllib.request.urlopen", return_value=FakeBadResponse()):
        with pytest.raises(ModelProviderError) as exc_info:
            client.complete("hello", 42)

    assert exc_info.value.code == "model_response_invalid"
    assert client.last_completion_metadata["provider_error_code"] == "model_response_invalid"


def test_chat_completions_finalization_only_downgrades_max_effort():
    """收尾轮（finalization_only）把 DeepSeek 思考档位 max 压到 high，
    防 reasoning 吃光输出预算导致正文空响应。"""
    from pico.providers.clients import ModelProviderError

    captured = {"payloads": []}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "done"}}], "usage": {}}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payloads"].append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        reasoning_effort="max",
        supported_reasoning_efforts=("none", "low", "high", "max"),
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete("hello", 2048)
        client.complete("hello", 2048, finalization_only=True)

    normal = captured["payloads"][0]
    final = captured["payloads"][1]
    assert normal["reasoning_effort"] == "max"
    assert normal["thinking"] == {"type": "enabled"}
    assert final["reasoning_effort"] == "high"
    assert final["thinking"] == {"type": "enabled"}


def test_chat_completions_thinking_budget_exhausted_retries_with_thinking_disabled():
    """只有思考（reasoning_content）无正文且 finish_reason=length 时，
    不判 model_response_invalid，而是关闭 thinking 重试一次。"""
    from pico.providers.clients import ModelProviderError

    captured = {"attempts": 0, "payloads": []}

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            captured["attempts"] += 1
            if captured["attempts"] == 1:
                # 思考吃光预算：只有 reasoning_content，无 content。
                lines = [
                    'data: {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}',
                    'data: {"choices": [{"delta": {"content": ""}, "finish_reason": "length"}]}',
                    "data: [DONE]",
                ]
            else:
                lines = [
                    'data: {"choices": [{"delta": {"content": "<final>done</final>"}}]}',
                    'data: {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}',
                    "data: [DONE]",
                ]
            return iter(lines)

    def fake_urlopen(request, timeout):
        captured["payloads"].append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
        reasoning_effort="max",
        supported_reasoning_efforts=("none", "low", "high", "max"),
    )
    client.max_attempts = 2

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 128)

    assert captured["attempts"] == 2
    # 第一次请求带 thinking；重试关闭 thinking。
    assert captured["payloads"][0]["thinking"] == {"type": "enabled"}
    assert captured["payloads"][1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured["payloads"][1]
    assert result == "<final>done</final>"
    assert client.last_completion_metadata.get("thinking_budget_recovered") is True


def test_chat_completions_plain_empty_response_still_invalid():
    """无思考、无工具、无正文的空响应仍按 model_response_invalid 处理。"""
    from pico.providers.clients import ModelProviderError

    captured = {"attempts": 0}

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            captured["attempts"] += 1
            return iter(['data: {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}', "data: [DONE]"])

    def fake_urlopen(request, timeout):
        return FakeResponse()

    client = OpenAICompletionsModelClient(
        model="deepseek-ai/DeepSeek-V3.2",
        base_url="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    client.max_attempts = 1

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(ModelProviderError) as exc_info:
            client.complete("hello", 128)

    assert exc_info.value.code == "model_response_invalid"

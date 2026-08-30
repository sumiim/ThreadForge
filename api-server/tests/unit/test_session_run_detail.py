"""§7.8.9 决策（2026-08-19）：对话历史回放——run_index 还原工具/thinking/审查。"""

from threadforge_api.application.session_service import _attach_run_detail


def _messages():
    # 标准一轮对话：user → assistant（assistant 消息对应一次运行）。
    return [
        {"role": "user", "content": "第一问", "created_at": "2026-08-19T00:00:00Z"},
        {"role": "assistant", "content": "第一答", "created_at": "2026-08-19T00:00:02Z"},
    ]


def _task(run_index, *, status="completed", stop_reason="final_answer_returned"):
    return {
        "task_id": "task_1",
        "created_at": "2026-08-19T00:00:01Z",
        "run_index": run_index,
        "status": status,
        "stop_reason": stop_reason,
    }


def test_attach_run_detail_rebuilds_tools_thinking_and_review():
    run = [
        {"type": "assistant.thinking", "text": "先看结构"},
        {
            "type": "tool.requested",
            "tool_call_id": "call_1",
            "tool_name": "list_files",
            "args_preview": {"path": "."},
        },
        {
            "type": "tool.completed",
            "tool_call_id": "call_1",
            "tool_name": "list_files",
            "result_preview": "README.md\nclient",
        },
        {
            "type": "review.started",
            "trigger": "final_before",
        },
        {
            "type": "review.completed",
            "verdict": "finalize",
            "feedback": "verified reasonable",
            "reason": "done",
            "obstacles": [],
        },
        {"type": "main_loop_rebuttal", "against_verdict": "finalize", "action": "tool:read_file", "feedback": "再确认"},
        {"type": "review.completed", "verdict": "finalize", "feedback": "confirmed", "reason": "done", "obstacles": []},
    ]
    messages = _attach_run_detail(_messages(), [_task(run)])

    assert messages[1]["thinking"] == "先看结构"
    assert messages[1]["tool_calls"] == [
        {
            "id": "call_1",
            "tool_name": "list_files",
            "args": {"path": "."},
            "status": "completed",
            "result": "README.md\nclient",
        }
    ]
    entries = messages[1]["review_entries"]
    assert [entry["side"] for entry in entries] == ["review", "review", "main_loop", "review"]
    assert entries[0]["action"] == "final_before"  # review.started
    assert entries[1]["verdict"] == "finalize"
    assert entries[2]["action"] == "tool:read_file"  # 主循环反驳
    assert entries[3]["verdict"] == "finalize"
    # blocks 重建：thinking+工具归入 behavior 块,review 独立块。
    blocks = messages[1]["blocks"]
    assert blocks[0]["kind"] == "behavior"
    assert blocks[0]["thinking"] == "先看结构"
    assert blocks[0]["toolCalls"][0]["tool_name"] == "list_files"
    assert blocks[1]["kind"] == "review"
    assert len(blocks[1]["entries"]) == 4
    # 第二条 assistant 消息不存在于单轮夹具；此处确认无越界。
    assert len(messages) == 2


def test_attach_run_detail_groups_blocks_by_turn_and_keeps_commentary():
    run = [
        {"type": "assistant.commentary", "text": "我现在去查查配置"},
        {"type": "model.started"},
        {"type": "assistant.thinking", "text": "先看 A"},
        {"type": "tool.requested", "tool_call_id": "c1", "tool_name": "list_files", "args_preview": {"path": "."}},
        {"type": "tool.completed", "tool_call_id": "c1", "tool_name": "list_files", "result_preview": "a.txt"},
        {"type": "model.started"},
        {"type": "assistant.thinking", "text": "再看 B"},
    ]
    messages = _attach_run_detail(_messages(), [_task(run)])

    message = messages[1]
    assert message["commentary"] == "我现在去查查配置"
    blocks = message["blocks"]
    assert [block["kind"] for block in blocks] == ["commentary", "behavior", "behavior"]
    assert blocks[0]["text"] == "我现在去查查配置"
    # turn：commentary 在 model.started 前(turn 0),两个 behavior 块分别归属 turn 1 / turn 2
    assert blocks[1]["turn"] == 1
    assert blocks[2]["turn"] == 2
    assert blocks[1]["thinking"] == "先看 A"
    assert blocks[2]["thinking"] == "再看 B"
    assert blocks[1]["toolCalls"][0]["tool_name"] == "list_files"


def test_attach_run_detail_keeps_model_completed_text_out_of_chat_history():
    run = [
        {"type": "model.started"},
        {"type": "assistant.thinking", "text": "先定位配置"},
        {"type": "model.completed", "text": "我先检查项目配置。"},
        {"type": "tool.requested", "tool_call_id": "c1", "tool_name": "list_files", "args_preview": {"path": "."}},
        {"type": "model.completed", "text": "<tool>{\"name\":\"list_files\"}</tool>"},
    ]

    message = _attach_run_detail(_messages(), [_task(run)])[1]

    assert [block["kind"] for block in message["blocks"]] == ["behavior"]
    assert "commentary" not in message
    assert message["blocks"][0]["thinking"] == "先定位配置"
    assert message["blocks"][0]["toolCalls"][0]["tool_name"] == "list_files"


def test_attach_run_detail_marks_truncated_result():
    run = [
        {"type": "tool.requested", "tool_call_id": "c1", "tool_name": "run_shell", "args_preview": {"command": "pytest"}},
        {"type": "tool.completed", "tool_call_id": "c1", "tool_name": "run_shell", "result_preview": "big", "result_truncated": True},
    ]
    messages = _attach_run_detail(_messages(), [_task(run)])

    assert messages[1]["tool_calls"][0]["result"] == "big\n\n[预览已截断]"


def test_attach_run_detail_replaces_blocked_convergence_summary_with_stable_message():
    messages = _messages()
    messages[1]["content"] = "运行未能完整完成——过时且很长的候选总结"

    result = _attach_run_detail(
        messages,
        [_task([], status="blocked", stop_reason="convergence_guard_triggered")],
    )

    assert result[1]["content"] == "模型未能通过审查或持续产生有效进展，本次运行已停止空转。"


def test_attach_run_detail_keeps_readable_runtime_summary_for_failed_run():
    # 失败运行带引擎产出的可读托底总结：刷新后应保留，而不是被固定文案吞掉。
    def task_failed(final_answer, stop_reason="runtime_error"):
        return {
            "task_id": "task_1",
            "created_at": "2026-08-19T00:00:01Z",
            "run_index": [],
            "status": "failed",
            "stop_reason": stop_reason,
            "final_answer": final_answer,
        }

    summary = "⚠️ 运行中断：运行时出现未预期错误（runtime_error）。\n\n已收集到部分证据：\n- read_file：README.md"
    messages = _attach_run_detail(_messages(), [task_failed(summary)])
    assert messages[1]["content"] == summary


def test_attach_run_detail_keeps_stable_message_when_final_answer_is_internal():
    # 失败运行的 final_answer 仍是内部诊断（如 "status: needs_fix"）→ 落到固定文案。
    def task_failed(final_answer, stop_reason="runtime_error"):
        return {
            "task_id": "task_1",
            "created_at": "2026-08-19T00:00:01Z",
            "run_index": [],
            "status": "failed",
            "stop_reason": stop_reason,
            "final_answer": final_answer,
        }

    messages = _attach_run_detail(
        _messages(),
        [task_failed("status: needs_fix\nretry")],
    )
    assert messages[1]["content"] == "运行未能正常完成，请在审计中查看具体原因后重试。"


def test_attach_run_detail_read_only_skips_review():
    run = [
        {"type": "tool.requested", "tool_call_id": "c1", "tool_name": "read_file", "args_preview": {"path": "a.txt", "start": 1, "end": 8}},
        {"type": "tool.completed", "tool_call_id": "c1", "tool_name": "read_file", "result_preview": "content"},
        {"type": "review.skipped", "reason": "read_only_task"},
    ]
    messages = _attach_run_detail(_messages(), [_task(run)])

    entries = messages[1]["review_entries"]
    assert len(entries) == 1
    assert entries[0]["verdict"] == "skipped"
    assert "只读任务" in entries[0]["feedback"]


def test_attach_run_detail_pairs_from_tail_when_head_clipped():
    run_second = [
        {"type": "tool.requested", "tool_call_id": "c2", "tool_name": "search", "args_preview": {"pattern": "x"}},
        {"type": "tool.completed", "tool_call_id": "c2", "tool_name": "search", "result_preview": "hit"},
    ]
    # 头部被 message_limit 裁掉：只剩第二条 user/assistant，任务从尾部配对到第二条消息。
    messages = _attach_run_detail(
        [
            {"role": "user", "content": "第二问", "created_at": "2026-08-19T00:01:00Z"},
            {"role": "assistant", "content": "第二答", "created_at": "2026-08-19T00:01:30Z"},
        ],
        [
            {"task_id": "task_0", "created_at": "2026-08-19T00:00:01Z", "run_index": []},
            {"task_id": "task_1", "created_at": "2026-08-19T00:01:00Z", "run_index": run_second},
        ],
    )

    assert messages[1]["tool_calls"][0]["tool_name"] == "search"


def test_attach_run_detail_routes_planning_thinking_separately():
    # planning-stage thinking 进 planning_thinking;execute thinking + 工具进 turn 块。
    run = [
        {"type": "assistant.thinking", "text": "先规划一下", "stage": "planning"},
        {"type": "assistant.thinking", "text": "再执行", "stage": "execute"},
        {"type": "tool.requested", "tool_call_id": "c1", "tool_name": "list_files", "args_preview": {"path": "."}},
        {"type": "tool.completed", "tool_call_id": "c1", "tool_name": "list_files", "result_preview": "a.txt"},
    ]
    messages = _attach_run_detail(_messages(), [_task(run)])

    message = messages[1]
    assert message["planning_thinking"] == "先规划一下"
    blocks = message["blocks"]
    assert blocks[0]["kind"] == "behavior"
    assert blocks[0]["thinking"] == "再执行"
    assert blocks[0]["toolCalls"][0]["tool_name"] == "list_files"


def test_attach_run_detail_backfills_local_worker_task_input_from_user_message():
    # 本地 Worker 任务中央不落明文 input；应从同一轮对话的用户消息回填给
    # session.tasks / 审计导出使用。
    task = _task([], status="interrupted", stop_reason="worker_disconnected")
    task["input"] = ""

    messages = _attach_run_detail(_messages(), [task])

    assert task["input"] == "第一问"

"""§7.8.9 决策（2026-08-19）：对话历史回放——run_index 还原工具/thinking/审查。"""

from threadforge_api.application.session_service import _attach_run_detail


def _messages():
    # 标准一轮对话：user → assistant（assistant 消息对应一次运行）。
    return [
        {"role": "user", "content": "第一问", "created_at": "2026-08-19T00:00:00Z"},
        {"role": "assistant", "content": "第一答", "created_at": "2026-08-19T00:00:02Z"},
    ]


def _task(run_index):
    return {"task_id": "task_1", "created_at": "2026-08-19T00:00:01Z", "run_index": run_index}


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


def test_attach_run_detail_marks_truncated_result():
    run = [
        {"type": "tool.requested", "tool_call_id": "c1", "tool_name": "run_shell", "args_preview": {"command": "pytest"}},
        {"type": "tool.completed", "tool_call_id": "c1", "tool_name": "run_shell", "result_preview": "big", "result_truncated": True},
    ]
    messages = _attach_run_detail(_messages(), [_task(run)])

    assert messages[1]["tool_calls"][0]["result"] == "big\n\n[预览已截断]"


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

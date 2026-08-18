from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def test_agent_loop_runs_same_control_flow_as_pico_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.report_path(agent.current_task_state.run_id).exists()


def test_agent_loop_allows_final_only_round_after_tool_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>The workspace contains README.md.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Inspect the workspace")

    assert answer == "The workspace contains README.md."
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.attempts == 2
    assert "The tool-call budget is exhausted" in agent.model_client.prompts[-1]


def test_agent_loop_rejects_tool_during_final_only_round(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>must not be consumed</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Inspect the workspace")

    assert "步数预算已用尽" in answer
    assert "list_files" in answer
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.read_files == 0
    assert len(agent.model_client.outputs) == 1
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert '"event": "finalization_protocol_rejected"' in trace


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, ["<final>Facade works.</final>"])

    assert agent.ask("Use facade") == "Facade works."


def test_agent_loop_passes_current_allowed_tools_to_model(tmp_path):
    class CapturingModel(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.completion_kwargs = kwargs
            return super().complete(prompt, max_new_tokens, **kwargs)

    agent = build_agent(tmp_path, [])
    model = CapturingModel(["<final>Done.</final>"])
    agent.model_client = model

    assert agent.ask("Inspect the workspace") == "Done."

    definitions = model.completion_kwargs["tool_definitions"]
    assert {item["name"] for item in definitions} == set(agent.tools)
    assert all(item["type"] == "function" for item in definitions)


def test_tool_result_enters_post_tool_reasoning_and_persists_agent_state(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Evidence reviewed.</final>",
        ],
    )

    assert agent.ask("Inspect hello.txt") == "Evidence reviewed."
    state = agent.current_task_state
    assert state.phase == "FINAL"
    assert state.read_files == 1
    assert state.checklist
    assert "Gather the minimum workspace context" in state.completed_items
    assert "Analyze evidence and choose the next action" in state.completed_items
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "agent_state_changed"' in trace
    assert '"event": "post_tool_reasoning"' in trace
    assert '"reason": "post_tool_reasoning"' in trace


def test_agent_loop_consumes_native_dict_tool_calls(tmp_path):
    # §2.1 原生 tool calling：客户端直接返回 dict {"name", "args"}，
    # AgentLoop 不再依赖 <tool> 文本协议解析。
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}},
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.read_files == 1


def test_agent_loop_rejects_native_dict_with_non_object_args(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": "not-an-object"},
            "<final>ok</final>",  # retry 后模型给 final
        ],
    )

    answer = AgentLoop(agent).run("Inspect")

    # args 非对象 → parse 返回 retry → 模型给 final → completed，且未执行工具
    assert answer == "ok"
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.read_files == 0


def test_agent_loop_stagnation_converges_without_evidence_growth(tmp_path):
    """§7.8.9：连续坏轮（工具重复/失败，talk 豁免）→ 证据截停，审计链完整。

    模型反复调用相同工具（同名同参 read_file）：第 1 次执行成功，之后被 P4
    重复拦截（tool_repeated_or_failed），连续 3 个坏轮 → 截停。验证：
    ① stagnation_audit 逐轮记录（含坏轮原因）；② stagnation_detected trace
    带完整审计链；③ 后续工具调用被 finalization 拒掉。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>converged</final>",
        ],
        max_steps=6,
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    state = agent.current_task_state
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "stagnation_detected"' in trace
    # 停滞触发后不允许新工具：重复读被 finalization 拒掉，执行数远小于供给数
    assert state.tool_steps < 5
    # 审计链：stagnation_audit 逐轮记录坏轮，且坏轮原因可解释
    assert state.stagnation_audit, "stagnation_audit must record per-turn bad decisions"
    bad_reasons = {reason for entry in state.stagnation_audit for reason in entry.get("reasons", [])}
    assert "tool_repeated_or_failed" in bad_reasons
    # trace 里截停事件带审计明细
    assert '"audit"' in trace


def test_agent_loop_talk_rounds_are_not_bad(tmp_path):
    """§7.8.9：talk 轮是思考信号（trace + 前端可见），不判坏。

    模型 talk → 工具 → talk → 工具 的交替轮次，坏轮判定不把 talk 计为坏，
    停滞窗口不会因思考轮误触发。连续 talk 由 MAX_CONSECUTIVE_TALKS 兜底。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            "<talk>let me look at this</talk>",
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<talk>i see the content</talk>",
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    state = agent.current_task_state
    assert state.status == "completed"
    # talk 轮不产生坏轮记录（reasons 为空且 bad=False）
    for entry in state.stagnation_audit:
        if not entry.get("bad"):
            continue
        assert "silent_turn_no_output" not in entry.get("reasons", []), (
            "talk rounds must not be counted as silent bad rounds"
        )


def test_agent_loop_executes_tool_batch_serially(tmp_path):
    """§7.8.9 阶段 2：声明并行（tools 列表）→ 串行队列逐个执行。

    模型一次返回多个工具（read a + list .），AgentLoop 逐个串行执行，
    每个动作独立产 evidence、独立入窗口；trace 记录 batch 起止。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {
                "tools": [
                    {"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}},
                    {"name": "list_files", "args": {"path": "."}},
                ]
            },
            "<final>Batch done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect the workspace")

    assert answer == "Batch done."
    state = agent.current_task_state
    assert state.status == "completed"
    assert state.tool_steps == 2
    assert state.read_files == 1
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "tool_batch_started"' in trace
    assert '"event": "tool_batch_completed"' in trace
    # 批内两个动作都产 evidence
    assert len(state.evidence) >= 2


def test_agent_loop_tool_batch_all_rejected_counts_bad(tmp_path):
    """§7.8.9 阶段 2：批内所有动作都重复/失败 → 本轮计坏（turn_tool_repeated）。

    连续多个坏批（每批都是重复动作被 P4 拦截）→ 证据截停触发。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"tools": [{"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}}]},
            {"tools": [{"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}}]},
            {"tools": [{"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}}]},
            {"tools": [{"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}}]},
            {"tools": [{"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}}]},
            "<final>converged</final>",
        ],
        max_steps=6,
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    state = agent.current_task_state
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "stagnation_detected"' in trace
    # 第 1 批执行成功（evidence+1），后续批重复被 P4 拦截 → 连续坏轮 → 截停
    assert state.tool_steps < 5


def test_agent_loop_tool_batch_dedupes_and_truncates(tmp_path):
    """§7.8.9 阶段 2：P1 并行数截断 + P2 批内去重。

    批内重复动作合并（只留首个）；超过 MAX_PARALLEL_TOOLS 的动作被截断。
    """
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    tools = []
    # 12 个动作：hello.txt 读两次（重复），其余不同文件读 10 次（超 8 截断）
    for _ in range(2):
        tools.append({"name": "read_file", "args": {"path": "hello.txt", "start": 1, "end": 1}})
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text(f"content {i}\n", encoding="utf-8")
        tools.append({"name": "read_file", "args": {"path": f"f{i}.txt", "start": 1, "end": 1}})
    agent = build_agent(
        tmp_path,
        [
            {"tools": tools},
            "<final>Done.</final>",
        ],
        max_steps=10,
    )

    answer = AgentLoop(agent).run("Inspect files")

    assert answer == "Done."
    state = agent.current_task_state
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    # 12 个声明 → 去重 1 个（hello.txt 重复）→ 剩 11 → P1 截断到 8
    assert '"deduped": 1' in trace
    assert '"truncated": true' in trace
    assert state.tool_steps <= 8


def test_agent_loop_batch_merges_and_chunks_reads(tmp_path):
    """§7.8.9 阶段 2：P3 读归并 + P4 读切片。

    同文件重叠区间合并（50-100 + 70-150 → 50-150）；
    合并后超长区间切成 ≤200 行多片（原子批）。
    """
    from pico.agent_loop import (
        MAX_READ_CHUNK_LINES,
        _merge_read_intervals,
        _normalize_batch_reads,
    )

    # 归并：重叠区间取并集
    merged = _merge_read_intervals([(50, 100), (70, 150)])
    assert merged[0] == (50, 150)

    # 切片：500 行切 3 片（200+200+100）
    chunks = []
    for start, end in [(1, 500)]:
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + MAX_READ_CHUNK_LINES - 1)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + 1
    assert chunks == [(1, 200), (201, 400), (401, 500)]

    # 批归一化：重叠读合并 + 超长切片
    (tmp_path / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 501)), encoding="utf-8")
    tools = [
        {"name": "read_file", "args": {"path": "big.py", "start": 50, "end": 100}},
        {"name": "read_file", "args": {"path": "big.py", "start": 70, "end": 300}},
        {"name": "read_file", "args": {"path": "other.py", "start": 1, "end": 10}},
    ]
    normalized, merged_count = _normalize_batch_reads(tools)
    assert merged_count == 1
    read_big = [t for t in normalized if t["args"].get("path") == "big.py"]
    assert read_big, "big.py reads must be normalized"
    assert len(read_big) == 2  # 50-300 → 切 50-249 + 250-300（每片 ≤200 行）
    assert read_big[0]["args"] == {"path": "big.py", "start": 50, "end": 249}
    assert read_big[1]["args"] == {"path": "big.py", "start": 250, "end": 300}


def test_agent_loop_repeated_actions_do_not_pollute_repeat_window(tmp_path):
    """§7.8.9 边界：重复动作（P4 拦截）不入重复窗口。

    重复 read 被拦截后不 append 指纹；真正的新动作（不同文件）不被重复污染，
    后续仍可基于最早的原始动作正确判定。
    """
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"b.txt","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"b.txt","start":1,"end":1}}</tool>',
            "<final>done</final>",
        ],
        max_steps=6,
    )

    answer = AgentLoop(agent).run("Inspect files")

    state = agent.current_task_state
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    # 4 次重复 a.txt（第 1 次成功，后 2 次被拦）+ 2 次 b.txt（第 1 次成功，第 2 次被拦）
    # 若重复动作污染窗口，b.txt 第 1 次可能被误判（窗口被 a 挤满）——修复后不会。
    # 验证：b.txt 成功执行了（read_files 至少 2：a 和 b 各一次成功）
    assert state.read_files == 2
    assert answer == "done"


def test_repeated_final_rejected_converges_with_rejected_finals(tmp_path):
    """连续 2 次无工具 final 被 review redirect → 程序强制收敛，输出 rejected_finals 内容。

    §7.8.9 修正（2026-08-18）：纯语言空转（模型反复 final 但 review 拒）不能烧到
    步数上限，连续 2 轮即收敛；被拒的 final 存 rejected_finals，收敛时拼进 best-effort。
    """
    agent = build_agent(
        tmp_path,
        [
            "<final>候选回答一：我根据记忆回答。</final>",   # 主循环第 1 次 final
            '{"verdict": "redirect", "feedback": "需要先读工作区", "reason": "ungrounded"}',  # review 1
            "<final>候选回答二：我再回答一次。</final>",   # 主循环第 2 次 final
            '{"verdict": "redirect", "feedback": "仍无证据", "reason": "ungrounded"}',      # review 2
            # 收敛触发，不再调模型
        ],
        feature_flags={"review_subagent": True},
    )

    answer = AgentLoop(agent).run("你是谁")

    state = agent.current_task_state
    # 收敛：stopped（非 blocked），且输出包含被拒候选内容
    assert state.status == "stopped"
    assert state.stop_reason == "step_limit_reached"
    assert "候选回答一" in answer
    assert "候选回答二" in answer
    # rejected_finals 独立于 evidence 存储（不污染 evidence 计数）
    assert len(state.rejected_finals) == 2
    assert all(item["status"] == "final_rejected" for item in state.rejected_finals)
    # evidence 未被污染（纯对话无工具）
    assert len(state.evidence) == 0


def test_tooled_final_rejected_does_not_trigger_quick_convergence(tmp_path):
    """有工具 final 被 review 拒 2 次 → 不进「纯空转」计数，走通用坏轮窗口继续。

    §7.8.9 修正（2026-08-18）：有工具被拒 ≠ 空转（可能差最后一步验证，有产出
    不该急停）——连续 2 次有工具被拒不触发 rejected_finals 收敛，模型继续按
    review 反馈补，最终通过 review。
    """
    (tmp_path / "a.txt").write_text("v1\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            # 第 1 轮：读文件（有工具）
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            "<final>回答一（有工具）</final>",
            '{"verdict": "redirect", "feedback": "验证未过", "reason": "verification"}',  # review 1
            # 第 2 轮：再读（有工具）
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            "<final>回答二（有工具）</final>",
            '{"verdict": "redirect", "feedback": "仍需验证", "reason": "verification"}',   # review 2
            # 第 3 轮：继续读 + 最终通过
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            "<final>最终通过</final>",
            '{"verdict": "finalize", "feedback": "verified by test, done", "reason": "done"}',  # review 3
        ],
        feature_flags={"review_subagent": True},
    )

    answer = AgentLoop(agent).run("改 a.txt")

    state = agent.current_task_state
    # 有工具被拒 2 次不触发快速收敛 → 正常继续到第 3 轮通过
    assert state.status == "completed"
    assert answer == "最终通过"
    # 被拒的 final 仍存 rejected_finals（记录候选，但没触发快速收敛）
    assert len(state.rejected_finals) == 2
    # 首次 read 记 evidence；后两次 read 是重复（被拦）不新增
    assert len(state.evidence) == 1


def test_result_fingerprint_distinguishes_changed_file_reload(tmp_path):
    """§7.8.9 修正（2026-08-18）：结果参与重复判定——同动作但结果变了不算重复。

    直接测 _tool_call_repeats：read_file 同参数，结果指纹不同（文件被改）→
    不算重复；结果指纹相同 → 算重复。
    """
    from pico.agent_loop import _tool_call_repeats

    # 窗口：一次 read_file(a.txt) 动作 + 结果指纹 "hash_v1"
    window = [(("read_file", "a.txt", (1, 1)), "hash_v1")]

    # 同动作、结果相同 → 重复
    assert _tool_call_repeats("read_file", {"path": "a.txt", "start": 1, "end": 1}, "hash_v1", window) is True
    # 同动作、结果不同（文件被改）→ 不重复
    assert _tool_call_repeats("read_file", {"path": "a.txt", "start": 1, "end": 1}, "hash_v2", window) is False
    # 无结果指纹（shell/写）→ 只看动作
    assert _tool_call_repeats("run_shell", {"command": "ls"}, None, window) is False  # 工具不同
    assert _tool_call_repeats("read_file", {"path": "a.txt", "start": 1, "end": 1}, None, window) is True


def test_partition_batch_tools(tmp_path):
    """§7.8.9 P5：批内分区纯函数——并发组 vs 串行组判定。

    只读且批内早于它没有写冲突 → 并发；**不同文件的写可并行**（同路径被
    触碰过才串行）；run_shell 与「读到批内已写路径」的只读工具 → 串行；
    list_files / search 在批内已有写时保守串行。
    """
    from pico.agent_loop import _partition_batch_tools

    # 全部只读 → 全部并发，保持声明顺序
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
            {"name": "list_files", "args": {"path": "."}},
            {"name": "search", "args": {"pattern": "x"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0, 1, 2]
    assert serial == []

    # read(A) → write(A) → read(A)：首个读并发；写串行（A 已被读过）；
    # 写后同路径读串行
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
            {"name": "write_file", "args": {"path": "a.txt", "content": "v2"}},
            {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1, 2]

    # write(A) → read(A)：写 A 并发（A 未被触碰），写后同路径读串行
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "write_file", "args": {"path": "a.txt", "content": "v2"}},
            {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1]

    # 不同文件的写 → 全部并发（写 A、写 B 互不依赖）
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "write_file", "args": {"path": "a.txt", "content": "va"}},
            {"name": "write_file", "args": {"path": "b.txt", "content": "vb"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0, 1]
    assert serial == []

    # 同文件两次写 → 首个并发、第二个串行（同路径写必须按声明顺序）
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "patch_file", "args": {"path": "a.txt", "old_text": "x", "new_text": "y"}},
            {"name": "patch_file", "args": {"path": "a.txt", "old_text": "y", "new_text": "z"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1]

    # write A + write A（同文件不同内容）：覆盖写并行会竞态（非原子），
    # 最终内容取决于调度 → 第二个必须串行
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "write_file", "args": {"path": "a.txt", "content": "v1"}},
            {"name": "write_file", "args": {"path": "a.txt", "content": "v2"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1]

    # patch A + write A（同路径混合）：局部替换 + 全量覆盖顺序敏感 →
    # 第二个必须串行（两个方向都验证）
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "patch_file", "args": {"path": "a.txt", "old_text": "x", "new_text": "y"}},
            {"name": "write_file", "args": {"path": "a.txt", "content": "z"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1]
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "write_file", "args": {"path": "a.txt", "content": "z"}},
            {"name": "patch_file", "args": {"path": "a.txt", "old_text": "x", "new_text": "y"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1]

    # write(A) → read(B)：B 未被批内写污染 → read(B) 仍并发（读 A 前状态无妨）
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "write_file", "args": {"path": "a.txt", "content": "v2"}},
            {"name": "read_file", "args": {"path": "b.txt", "start": 1, "end": 1}},
        ]
    )
    assert [i for i, _ in concurrent] == [0, 1]
    assert serial == []

    # read(A) → write(B) → list_files：批内已有写 → list_files 保守串行
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
            {"name": "write_file", "args": {"path": "b.txt", "content": "v2"}},
            {"name": "list_files", "args": {"path": "."}},
        ]
    )
    assert [i for i, _ in concurrent] == [0, 1]
    assert [i for i, _ in serial] == [2]

    # list_files 之后写 A：list 并发（记 "*"），写 A 只能串行（list 看到写前状态）
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "list_files", "args": {"path": "."}},
            {"name": "write_file", "args": {"path": "a.txt", "content": "v2"}},
        ]
    )
    assert [i for i, _ in concurrent] == [0]
    assert [i for i, _ in serial] == [1]

    # run_shell 影响面未知 → 其后所有只读只能串行
    concurrent, serial = _partition_batch_tools(
        [
            {"name": "run_shell", "args": {"command": "make"}},
            {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
            {"name": "search", "args": {"pattern": "x"}},
        ]
    )
    assert concurrent == []
    assert [i for i, _ in serial] == [0, 1, 2]


def test_agent_loop_concurrent_batch_commits_in_declaration_order(tmp_path):
    """§7.8.9 P5：只读批走并发组——ThreadPoolExecutor 并行执行、按声明顺序提交。

    3 个不同文件 read 全部进并发组；提交后 tool_steps / read_files / evidence
    与串行链路一致；trace 带 concurrent/serial 分区统计。
    """
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bravo\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("charlie\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {
                "tools": [
                    {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
                    {"name": "read_file", "args": {"path": "b.txt", "start": 1, "end": 1}},
                    {"name": "read_file", "args": {"path": "c.txt", "start": 1, "end": 1}},
                ]
            },
            "<final>Concurrent batch done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Read the three files")

    assert answer == "Concurrent batch done."
    state = agent.current_task_state
    assert state.status == "completed"
    assert state.tool_steps == 3
    assert state.read_files == 3
    assert len(state.evidence) == 3
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"event": "tool_batch_started"' in trace
    assert '"concurrent": 3' in trace
    assert '"serial": 0' in trace


def test_agent_loop_batch_same_path_write_then_read_serial(tmp_path):
    """§7.8.9 P5：同路径写→读强制串行——读必须看到写后状态。

    批内 write_file(a.txt) 后紧跟 read_file(a.txt)：read 落串行组（写后
    读取），若并发先读会读到旧内容 v1。验证读结果含写后内容 v2。
    """
    (tmp_path / "a.txt").write_text("v1\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {
                "tools": [
                    {"name": "write_file", "args": {"path": "a.txt", "content": "v2\n"}},
                    {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 1}},
                ]
            },
            "<final>Verified write.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Rewrite a.txt and read it back")

    assert answer == "Verified write."
    state = agent.current_task_state
    assert state.status == "completed"
    assert state.tool_steps == 2
    # 写后读落串行组 → 读结果包含写后内容 v2
    tool_records = [
        r for r in agent.session["history"]
        if r.get("role") == "tool" and r.get("name") == "read_file"
    ]
    assert tool_records, "read_file must execute after the write"
    assert "v2" in tool_records[-1]["content"]
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"concurrent": 1' in trace
    assert '"serial": 1' in trace


def test_agent_loop_batch_patch_then_write_same_path_serial(tmp_path):
    """§7.8.9 P5：patch + write 同文件强制串行——按声明顺序执行。

    若并行，write 全量覆盖会让 patch 的 old_text 匹配失败（或 patch 结果
    被覆盖）——串行保证 patch 先替换、write 再覆盖，最终为 write 内容。
    """
    (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {
                "tools": [
                    {"name": "patch_file", "args": {"path": "a.txt", "old_text": "alpha", "new_text": "ALPHA"}},
                    {"name": "write_file", "args": {"path": "a.txt", "content": "omega\n"}},
                ]
            },
            "<final>Patched then rewrote.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Patch then rewrite a.txt")

    assert answer == "Patched then rewrote."
    state = agent.current_task_state
    assert state.status == "completed"
    assert state.tool_steps == 2
    # 串行：patch 先成功（old_text 匹配），write 再覆盖 → 最终 omega
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "omega\n"
    patch_records = [
        r for r in agent.session["history"]
        if r.get("role") == "tool" and r.get("name") == "patch_file"
    ]
    assert patch_records and "patched" in patch_records[0]["content"]
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"concurrent": 1' in trace
    assert '"serial": 1' in trace


def test_agent_loop_batch_commits_window_in_declaration_order(tmp_path):
    """§7.8.9 R7：并发组 + 串行组交错时,提交(窗口)顺序 = 声明顺序。

    batch [write A, list_files, write B]:write A/write B 进并发组、
    list_files 落串行组（批内已有写,保守串行）——提交必须按声明序
    （write A, list_files, write B）,不能按组顺序（write A, write B,
    list_files）,否则 list_files 挤到窗口末尾,「匹配后有无写工具」的
    重复豁免判定与声明语义不一致。
    """
    (tmp_path / "a.txt").write_text("va\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {
                "tools": [
                    {"name": "write_file", "args": {"path": "a.txt", "content": "va2\n"}},
                    {"name": "list_files", "args": {"path": "."}},
                    {"name": "write_file", "args": {"path": "b.txt", "content": "vb2\n"}},
                ]
            },
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Rewrite a.txt, list, then write b.txt")

    assert answer == "Done."
    state = agent.current_task_state
    assert state.status == "completed"
    assert state.tool_steps == 3
    # 提交（窗口）顺序 = 声明顺序：write A → list_files → write B
    tool_names = [r.get("name") for r in agent.session["history"] if r.get("role") == "tool"]
    assert tool_names == ["write_file", "list_files", "write_file"]


def test_agent_loop_batch_parallel_writes_different_files(tmp_path):
    """§7.8.9 P5：不同文件的写可并行——互不依赖，affected_paths 不被污染。

    batch [write a.txt, write b.txt] 都进并发组并行执行；各 evidence 的
    affected_paths 只含自己的文件（路径级 snapshot diff 保证不把并行兄弟
    写的文件算进来）。
    """
    agent = build_agent(
        tmp_path,
        [
            {
                "tools": [
                    {"name": "write_file", "args": {"path": "a.txt", "content": "va\n"}},
                    {"name": "write_file", "args": {"path": "b.txt", "content": "vb\n"}},
                ]
            },
            "<final>Wrote both.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Write a.txt and b.txt")

    assert answer == "Wrote both."
    state = agent.current_task_state
    assert state.status == "completed"
    assert state.tool_steps == 2
    # 两个文件都写好了
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "va\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "vb\n"
    # 各写工具的 affected_paths 只含自己的文件（无并行污染）
    write_evidence = [e for e in state.evidence if e.get("tool_name") == "write_file"]
    assert len(write_evidence) == 2
    for ev in write_evidence:
        assert len(ev.get("affected_paths", [])) == 1
    paths_by_call = {tuple(sorted(ev["affected_paths"])) for ev in write_evidence}
    assert paths_by_call == {("a.txt",), ("b.txt",)}
    trace = agent.run_store.trace_path(state).read_text(encoding="utf-8")
    assert '"concurrent": 2' in trace
    assert '"serial": 0' in trace

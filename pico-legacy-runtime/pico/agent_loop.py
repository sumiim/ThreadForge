"""Agent control loop extracted from the runtime facade."""

import concurrent.futures
import hashlib
import json
import threading
import time
import uuid
from collections import deque

from .checkpoint import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from .evaluation.review_subagent import REVIEW_POLL_ACTIONS
from .execution_hooks import ProcessCleanupFailed, RunCancelled
from .task_state import (
    PHASE_ACT_OR_ANSWER,
    PHASE_FINAL,
    PHASE_GATHER_CONTEXT,
    PHASE_UNDERSTAND_REQUEST,
    PHASE_VERIFY,
    STATUS_FAILED,
    STATUS_STOPPED,
    STOP_REASON_BUDGET_EXHAUSTED,
    STOP_REASON_PROCESS_CLEANUP_FAILED,
    STOP_REASON_STEP_LIMIT_REACHED,
    TaskState,
)
from .tool_executor import ToolExecutionResult
from .tools import provider_tool_definitions
from .workspace import clip, now


def _new_tool_call_id():
    return "call_" + uuid.uuid4().hex


def _best_effort_step_limit(task_state, reason):
    """预算/重试耗尽时的可读收尾：列出已收集证据 + 被 review 拒的候选答案。

    §7.8.9 修正（2026-08-18）：纯语言空转收敛（连续 2 次 final 被拒）时，
    rejected_finals 里有模型产出过的候选答案——拼进收尾，避免裸 blocked。
    """
    evidence = list(task_state.evidence or [])
    rejected = list(task_state.rejected_finals or [])
    header = f"⚠️ 运行中断：{reason}，未能在预算内产出最终结论。"
    if not evidence and not rejected:
        return header + " 本轮未成功读取任何工作区证据，建议缩小请求范围后重试。"
    lines = []
    for item in evidence[-12:]:
        tool = str(item.get("tool_name", "tool"))
        paths = item.get("relative_paths") or []
        path_text = "、".join(str(path) for path in paths[:4])
        lines.append(f"- {tool}：{path_text or '（无路径）'}")
    text = header + " 本轮已收集的部分证据：\n" + "\n".join(lines)
    if rejected:
        text += "\n\n模型曾给出的候选回答（未经 review 确认）：\n"
        for item in rejected[-3:]:
            content = str(item.get("content", "") or "").strip()
            if content:
                text += f"- {content[:400]}\n"
    text += "\n\n请缩小范围、换一个更具体的请求，或提高预算后重试。"
    return text


def _convergence_summary(agent, task_state, reason):
    """§7.8.9 决策（2026-08-19）：截断/收敛兜底——有候选或证据时,让模型做一次
    总结输出,而不是 raw 拼候选列表。

    被拒候选（rejected_finals）和证据有内容 → LLM 总结成连贯、诚实的最终回答
    （说明运行未完成 + 整合已确认信息）。模型调用失败或无可总结内容 → 回退
    _best_effort_step_limit。
    """
    rejected = [
        item
        for item in (task_state.rejected_finals or [])
        if str(item.get("content", "") or "").strip()
    ]
    evidence = list(task_state.evidence or [])
    if not rejected and not evidence:
        return _best_effort_step_limit(task_state, reason)
    parts = []
    for index, item in enumerate(rejected[-3:]):
        parts.append(f"[被拒候选 {index + 1}]\n{str(item.get('content', ''))[:800]}")
    for item in evidence[-5:]:
        summary = str(item.get("summary", "") or "")[:300]
        if summary:
            parts.append(f"[证据:{item.get('tool_name', 'tool')}] {summary}")
    prompt = (
        "你是 ThreadForge 的收尾总结器。运行被中断（预算或收敛兜底），以下是模型被审查"
        "驳回的候选回答和已收集证据。请总结成一个连贯、诚实、可直接展示给用户的最终回答：\n"
        "- 开头说明运行未能完整完成\n"
        "- 整合已确认的信息（证据）与候选回答中的可用内容\n"
        "- 不要编造新证据，不要逐条罗列原始候选列表\n"
        "输出纯文本。\n\n"
        + "\n\n".join(parts)
    )
    try:
        raw = agent.model_client.complete(prompt, 2048)
        text = str(raw or "").strip()
        if text:
            return f"⚠️ 运行中断：{reason}。\n\n{text}"
    except Exception:
        pass
    return _best_effort_step_limit(task_state, reason)


MAX_CONSECUTIVE_TALKS = 2
MAX_PROTOCOL_REPAIRS = 1
# §7.8.9 决策（2026-08-18）：无 turn 硬顶——max_attempts 仅作「模型反复
# 无效输出」的最后保险丝（防完全失控），正常停止全由证据截停/rejected_finals
# 收敛/墙钟/token 硬顶接管。500 轮 × 每轮数秒 ≫ 墙钟 3600s，实际先撞墙钟。
MAX_ATTEMPTS_SAFETY = 500

# §7.8.7-③ / §7.8.9 停滞检测（滑动窗口）：
# - 一个 turn = 一次模型决策 + 其后的工具执行（或无工具）。
# - 每 turn 结束算三个坏信号：checklist 无新增、evidence 无新增、工具重复/无工具。
# - 滑动窗口内最近 n 个 turn 三信号全坏 → 强制收敛。
# n 固定 3（§7.8.9 定）：不随剩余预算动态——动态化的初衷「剩余多→宽容」
# 由 review 轮询承担（模型慢思考时 review 给 continue 确认方向），证据截停
# 只管「evidence 不涨」。深思熟虑的模型（前 2 轮 talk，第 3 轮行动）不会被
# 杀——第 3 轮 evidence 涨了窗口重置。
STAGNATION_TOLERANCE = 3          # 连续坏 turn 阈值（固定）
STAGNATION_WINDOW = 6             # 滑动窗口长度（≥ tolerance 即可，留余量）
TOOL_REPEAT_WINDOW = 6            # 工具重复判定窗口（复用 repeated_tool_call 的窗口宽度）
READ_OVERLAP_RATIO = 0.6          # read_file 区间重叠比例 ≥ 60% 才算重复读
MAX_PARALLEL_TOOLS = 8            # §7.8.9 P1：声明并行数截断，一次 >8 个砍到 8

# 只读工具：写工具会改变工作区，之后重新 list/read 是合理的，不算停滞。
_READ_ONLY_TOOL_NAMES = frozenset({"list_files", "read_file", "search"})


def _stagnation_threshold(remaining_turns):
    """连续坏 turn 阈值 n：固定 3（§7.8.9 定）。

    remaining_turns 参数保留仅作 trace 记录；n 不随剩余预算变化。
    """
    return STAGNATION_TOLERANCE


def _normalize_read_interval(start, end):
    """read_file 区间归一化：(start, end) 数字元组；缺省视为全文件。"""
    try:
        s = int(start) if start is not None else None
        e = int(end) if end is not None else None
    except (TypeError, ValueError):
        return (None, None)
    return (s, e)


def _interval_overlap_ratio(interval_a, interval_b):
    """当前读取区间相对较早区间的重叠比例（0~1）。

    语义：读同一文件时，当前区间的大部分内容（≥ READ_OVERLAP_RATIO）若在
    历史区间内读过，则视为重复读（防模型靠微调行号绕过重复检测）。缺省端
    视为全文件。分母取「当前区间长度」，保证「读全文 vs 读其中一段」这种
    大范围重叠也被判为重复。
    """
    a_start, a_end = interval_a
    b_start, b_end = interval_b
    if a_start is None:
        a_start = b_start if b_start is not None else 1
    if a_end is None:
        a_end = b_end if b_end is not None else a_start
    if b_start is None:
        b_start = a_start
    if b_end is None:
        b_end = a_end
    # 零长度区间（start == end，如 read 单行 {start:1, end:1}）：视为单行，
    # 同 start 且同 end 完全重叠；不同位置则无重叠。
    if a_end <= a_start:
        if b_end <= b_start:
            return 1.0 if (a_start == b_start and a_end == b_end) else 0.0
        # 当前是单行，历史是区间：单行落在历史区间内算重叠
        return 1.0 if b_start <= a_start <= b_end else 0.0
    if a_end < b_start or b_end < a_start:
        return 0.0
    overlap = min(a_end, b_end) - max(a_start, b_start)
    if overlap <= 0:
        return 0.0
    len_current = max(1, a_end - a_start)
    return overlap / len_current


def _tool_fingerprint(name, args):
    """工具调用指纹：语义等价归一化（复用 runtime._normalize_tool_args 思路）。

    - read_file → (name, path, 行区间)：行区间保留用于重叠判定（读不同区段不算重复）。
    - run_shell → (name, command)：命令内容相同才算重复。
    - search → (name, path, pattern)。
    - 其余 → (name, 归一化 args)。
    """
    from .runtime import _normalize_tool_args

    args = args if isinstance(args, dict) else {}
    if name == "read_file":
        normalized = _normalize_tool_args(name, args)
        path = str(normalized.get("path", "") or ".")
        interval = _normalize_read_interval(args.get("start"), args.get("end"))
        return ("read_file", path, interval)
    if name == "run_shell":
        command = str(args.get("command", "") or "").strip()
        return ("run_shell", command)
    if name == "search":
        normalized = _normalize_tool_args(name, args)
        path = str(normalized.get("path", "") or ".")
        pattern = str(args.get("pattern", "") or "").strip()
        return ("search", path, pattern)
    return (name, json.dumps(_normalize_tool_args(name, args), sort_keys=True, ensure_ascii=False))


def _tool_fingerprints_match(fp_a, fp_b):
    """两个指纹是否构成「重复调用」：
    - read_file 同路径且行区间重叠比例 ≥ 阈值（读不同区段不算重复）；
    - 其余完全相等。
    """
    if fp_a[0] != fp_b[0]:
        return False
    if fp_a[0] == "read_file":
        path_a, interval_a = fp_a[1], fp_a[2]
        path_b, interval_b = fp_b[1], fp_b[2]
        if path_a != path_b:
            return False
        return _interval_overlap_ratio(interval_a, interval_b) >= READ_OVERLAP_RATIO
    return fp_a == fp_b


def _result_fingerprint(name, args, tool_result):
    """观察结果指纹：只读工具的成功内容摘要（同动作同结果才算空转）。

    §7.8.9 修正（2026-08-18）：read_file(a) 两次、中间文件被改 → 内容变了，
    重读是合理的（确认写结果），不应判重复。指纹加结果后：参数相同且结果
    相同才算重复；结果变了 → 不算重复。

    只对只读工具（read_file/search/list_files）且**成功结果**做比较——
    shell 输出多变（时间戳/路径/环境）不参与；失败/被拦结果（如 runtime
    重复拦截的 error）不参与，返回 None（否则 error 会让「原本重复的调用」
    看起来像结果变化，污染判定）。
    """
    if name not in {"read_file", "search", "list_files"}:
        return None
    if not hasattr(tool_result, "content"):
        return None
    status = str((getattr(tool_result, "metadata", {}) or {}).get("tool_status", ""))
    if status not in {"ok", "partial_success"}:
        return None
    content = str(tool_result.content or "")
    if not content.strip():
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _tool_call_repeats(name, args, result_fp, recent_entries):
    """当前工具调用是否与滑动窗口内的历史调用重复。

    - 窗口内匹配到同动作指纹 → 看结果：只读工具要求结果相同（结果变了 =
      文件被改，重读合理 → 不算重复）；result_fp 为 None（shell/写/未执行）
      只看动作。
    - **read_file 部分重叠区间例外**（§7.8.9 修正 2026-08-19）：区间不同但
      重叠 ≥60%（如 1-250 → 20-200）→ 直接判重复——区间微调 = 同一区域打转，
      结果 hash 因区间不同必然不同，若比较会让「1-250 → 20-200 → 38-200」
      每次都是新读取、绕开重复判定。完全同区间才用结果指纹（结果变 = 文件
      被改，重读合理）。
    - 但若「匹配之后有写工具介入」，工作区已变，重读/重列是合理的 → 不重复
      （对齐 runtime.repeated_tool_call 的语义）。

    ``recent_entries``：deque of (action_fp, result_fp)。
    """
    action_fp = _tool_fingerprint(name, args)
    for index, (action_prev, result_prev) in enumerate(recent_entries):
        if not _tool_fingerprints_match(action_fp, action_prev):
            continue
        partial_overlap = (
            name == "read_file"
            and action_prev[0] == "read_file"
            and action_fp[2] != action_prev[2]
        )
        # §7.8.9 修正（2026-08-19）：read_file 部分重叠分「扩展读」与「子集微调」。
        # 扩展读（新区间含未读新行,如 1-200 → 1-260）→ 有增量信息,放行执行;
        # 纯子集/微调（30-130、40-80 ⊆ 已读并集）→ 空转,判重复（防空转）——
        # 但拒绝消息会给明确指引（内容已在上下文,勿重读;如需未读行请指定新区间）。
        if partial_overlap:
            merged_intervals = _merge_read_intervals(
                [
                    entry[0][2]
                    for entry in recent_entries
                    if entry[0][0] == "read_file" and entry[0][1] == action_fp[1]
                ]
            )
            new_start, new_end = action_fp[2]
            if _uncovered_read_lines(new_start, new_end, merged_intervals) > 0:
                continue  # 扩展读：本次读到未覆盖的新行,不算重复
            return True
        # 结果参与（只读工具）：完全同区间 + 结果变 = 文件被改 → 不重复；
        if not partial_overlap and (
            result_fp is not None
            and result_prev is not None
            and result_fp != result_prev
        ):
            continue  # 结果变了（文件被改）→ 不算重复，重读合理
        tail = [entry[0][0] for entry in list(recent_entries)[index + 1:]]
        if any(tool not in _READ_ONLY_TOOL_NAMES for tool in tail):
            continue
        return True
    return False


# §7.8.9 P3/P4 读工具声明预防：切片行数/字节上限。
MAX_READ_CHUNK_LINES = 200
MAX_READ_CHUNK_BYTES = 64 * 1024


def _merge_read_intervals(intervals):
    """同文件 read_file 区间归并：重叠/相邻区间取并集。

    输入 [(start, end), ...]，输出归并后的 [(start, end), ...]。
    None 端视为全文件（不参与归并，保持原样）。
    """
    definite = [iv for iv in intervals if iv[0] is not None and iv[1] is not None]
    open_ended = [iv for iv in intervals if iv[0] is None or iv[1] is None]
    if not definite:
        return intervals
    ordered = sorted(definite)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:  # 重叠或相邻（end+1 相邻算连续）
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged + open_ended


def _uncovered_read_lines(start, end, merged_intervals):
    """新区间 (start, end) 中未被已读区间并集覆盖的行数。

    >0 = 这次读有增量信息（扩展读）→ 放行；=0 = 纯子集/微调 → 空转判重复。
    """
    if start is None or end is None:
        return 0
    uncovered = 0
    cursor = start
    for iv_start, iv_end in sorted(merged_intervals):
        if iv_start is None or iv_end is None:
            continue
        if iv_end < cursor:
            continue
        if iv_start > end:
            break
        if iv_start > cursor:
            uncovered += iv_start - cursor
        cursor = max(cursor, iv_end + 1)
        if cursor > end:
            break
    if cursor <= end:
        uncovered += end - cursor + 1
    return uncovered


def _chunk_read_intervals(merged):
    """按 MAX_READ_CHUNK_LINES 把合并后的区间切片。

    每片 ≤ 200 行；返回 (chunks, original_count)。open-ended 区间不切。
    """
    chunks = []
    for start, end in merged:
        if start is None or end is None:
            chunks.append((start, end))
            continue
        length = end - start + 1
        if length <= MAX_READ_CHUNK_LINES:
            chunks.append((start, end))
            continue
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + MAX_READ_CHUNK_LINES - 1)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + 1
    return chunks


def _normalize_batch_reads(tools):
    """§7.8.9 P3 读归并 + P4 读切片（批内 read_file 声明预防）。

    - 同文件重叠/相邻区间 → 归并成并集区间（不同文件不合并）。
    - 归并后 >200 行 → 切成多片。
    返回 (normalized_tools, merged_count)。
    """
    # 先按文件分组 read_file 动作
    read_groups: dict[str, list] = {}
    others = []
    for item in tools:
        name = str(item.get("name", ""))
        if name == "read_file":
            args = item.get("args", {}) if isinstance(item.get("args"), dict) else {}
            path = str(args.get("path", "") or ".").strip().replace("\\", "/")
            read_groups.setdefault(path, []).append(item)
        else:
            others.append(item)
    normalized = list(others)
    merged_count = 0
    for path, group in read_groups.items():
        intervals = []
        for item in group:
            args = item.get("args", {})
            intervals.append(
                _normalize_read_interval(args.get("start"), args.get("end"))
            )
        merged = _merge_read_intervals(intervals)
        merged_count += len(group) - len(merged)
        for start, end in merged:
            # open-ended 区间：保留原动作（无行号限制）
            if start is None or end is None:
                original = group[0]
                normalized.append(
                    {"name": "read_file", "args": {"path": path}}
                )
                continue
            for chunk_start, chunk_end in _chunk_read_intervals([(start, end)]):
                normalized.append(
                    {
                        "name": "read_file",
                        "args": {"path": path, "start": chunk_start, "end": chunk_end},
                    }
                )
    return normalized, merged_count


def _extract_tool_path(name, args):
    """从工具参数里取规范化路径（写工具 / read_file 有 path 参数）。"""
    if not isinstance(args, dict):
        return None
    path = args.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip().replace("\\", "/")
    return None


def _verify_done_when(agent, done_when, cmd_cache):
    """§7.8.9 决策（2026-08-18）：checklist 打钩——程序化验收标准验证。

    支持三种前缀（planning schema 引导模型产出）：
    - ``file:<path>``：workspace 文件存在
    - ``grep:<path>:<pattern>``：文件内容包含 pattern（子串匹配）
    - ``cmd:<command>``：受管 shell 执行 exit 0（结果缓存,同一命令本 run 只跑一次）
    自由文本（无前缀）→ 返回 None（无法程序判定,交给 review 语义打钩）。
    """
    text = str(done_when or "").strip()
    try:
        if text.startswith("file:"):
            path = text[5:].strip()
            if not path:
                return False
            return (agent.root / path).is_file()
        if text.startswith("grep:"):
            path, _, pattern = text[5:].partition(":")
            if not path or not pattern:
                return False
            content = (agent.root / path).read_text(
                encoding="utf-8", errors="ignore"
            )
            return pattern in content
        if text.startswith("cmd:"):
            command = text[4:].strip()
            if not command:
                return False
            if command in cmd_cache:
                return cmd_cache[command]
            tool = agent.tools.get("run_shell")
            content = str(tool["run"]({"command": command, "timeout": 30})) if tool else ""
            ok = "exit_code: 0" in content
            cmd_cache[command] = ok
            return ok
    except Exception:
        return False
    return None  # 自由文本 → review 语义打钩


def _partition_batch_tools(tools):
    """§7.8.9 P5 批内分区：并发组（无数据依赖） vs 串行组。

    判定规则（按工具单位截断/拦阻，与 P1/P2/P4 一致）：
    - 只读工具（list_files / read_file / search）：
      - 批内早于它没有写工具 → 并发组（可并行执行；提交仍按声明顺序，
        重复判定窗口与 evidence 有序）。
      - 批内已有写工具：
        - list_files / search 枚举全工作区，无法预知写的影响面 → 保守串行；
        - read_file 只读指定路径，仅当该路径已被批内更早的写改过才串行
          （否则并发读到旧状态无妨，写不影响它）。
    - 写工具（write_file / patch_file）：
      - **不同文件的写可以并行**（2026-08-18 用户指出，原实现过度保守）：
        有 path、批内早于它没人读过/写过该 path、且批内没有 run_shell
        → 并发组。路径级 snapshot diff（`capture_path_snapshot`）保证
        affected_paths 不被并行兄弟污染。
      - 同路径已被触碰（读过或写过）、或批内已有 run_shell → 串行组。
    - run_shell → 永远串行组，影响面未知，污染所有路径（其后只读只能串行）。
    - P5 写切片（plan B）：patch_file 单条 old_text→new_text 语义不变，
      同文件多条 patch_file 天然构成写切片，串行组按声明顺序执行即可。

    返回 (concurrent_items, serial_items)，每项为 (index_in_tools, item)，
    index 用于按声明顺序合并执行结果与提交。
    """
    dirty_paths = set()   # 批内已被写过的具体路径
    touched = set()       # 批内已被触碰（读过/写过）的路径 —— 写工具进并发组的判定
    saw_write = False     # 批内是否已出现写/shell
    saw_shell = False     # 批内是否已出现 run_shell（影响面未知）
    concurrent = []
    serial = []
    for index, item in enumerate(tools):
        name = str(item.get("name", ""))
        args = item.get("args", {}) if isinstance(item.get("args"), dict) else {}
        if name in _READ_ONLY_TOOL_NAMES:
            if name in {"list_files", "search"}:
                # 枚举全工作区：批内已有写 → 保守串行；进入并发组则记录
                # "*"（其后任何写只能串行，保证 list 看到写前状态）。
                if not saw_write and not saw_shell:
                    concurrent.append((index, item))
                    touched.add("*")
                else:
                    serial.append((index, item))
                continue
            # read_file
            path = _extract_tool_path(name, args)
            if not saw_shell and path not in dirty_paths:
                concurrent.append((index, item))
                touched.add(path)
            else:
                serial.append((index, item))
        elif name == "run_shell":
            saw_write = True
            saw_shell = True
            dirty_paths.add("*")
            touched.add("*")
            serial.append((index, item))
        else:
            # write_file / patch_file
            path = _extract_tool_path(name, args)
            if not saw_shell and path and path not in touched and "*" not in touched:
                # 不同文件的写可并行：该路径批内未被触碰（没人读过/写过它）、
                # 前面无 shell → 并发组。
                concurrent.append((index, item))
            else:
                serial.append((index, item))
            saw_write = True
            if path:
                dirty_paths.add(path)
                touched.add(path)
            else:
                dirty_paths.add("*")
                touched.add("*")
    return concurrent, serial


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def _maybe_run_review(
        self,
        task_state,
        user_message,
        *,
        trigger,
        has_write_or_shell,
        verification_passed,
        prior_feedback,
    ):
        """§7.8.9 阶段 3：程序强制触发 review subagent。

        由 AgentLoop 在「每 REVIEW_POLL_ACTIONS 个动作」或「final 前」调用，
        不进 tool_definitions，模型无法绕过。返回 ReviewDecision（dict）。

        feature flag `review_subagent` 关闭时跳过（阶段推进中；测试用
        FakeModelClient 不开启，避免 review 消费其顺序输出）。
        """
        # 只读任务不需要 Review 门禁。重复动作和停滞窗口仍然负责收敛，
        # 但不会再额外消耗模型回合或因方向审查误驳回正常答案。
        if not has_write_or_shell:
            self.agent.emit_trace(
                task_state,
                "review_skipped",
                {"trigger": trigger, "reason": "read_only_task"},
            )
            return {
                "verdict": "finalize",
                "feedback": "read-only task does not require review",
                "reason": "review_skipped_read_only",
            }
        if not self.agent.feature_enabled("review_subagent"):
            return {
                "verdict": "continue",
                "feedback": "",
                "reason": "review_subagent_disabled",
            }
        from .evaluation.review_subagent import run_review

        return run_review(
            self.agent,
            task_state,
            request=user_message,
            trigger=trigger,
            has_write_or_shell=has_write_or_shell,
            verification_passed=verification_passed,
            prior_feedback=prior_feedback,
        )

    def _initial_plan(self, task_state, user_message):
        """§7.8.9 阶段 3.5：run 开始时一次 planning，生成真实 checklist。

        feature flag `planning` 关闭或规划失败时降级（保留默认阶段模板）。
        返回是否生成真实 checklist。
        """
        if not self.agent.feature_enabled("planning"):
            return False
        from .planning import is_plain_conversation, run_planning

        # §7.8.9 修正（2026-08-19）：纯对话（你好/谢谢/ok）不生成 explicit checklist——
        # 否则 review 的 checklist 障碍恒真（打钩引擎无程序化验收标准、review 语义
        # 打钩无工具可验证）→ 纯对话被反复拒死收敛 blocked。
        if is_plain_conversation(user_message):
            return False
        steps = run_planning(
            self.agent,
            task_state,
            task=user_message,
            context=str(getattr(self.agent, "memory_text", lambda: "")())[:2000],
            replan=False,
        )
        if not steps:
            return False
        task_state.checklist = [str(step["goal"]) for step in steps]
        task_state.done_when = [
            str(dw) for step in steps for dw in step.get("done_when", [])
        ]
        # §7.8.9 决策（2026-08-18）：checklist 打钩——每步验收标准映射,
        # 程序化（file:/grep:/cmd:）由打钩引擎验证、自由文本由 review 语义打钩。
        task_state.step_done_when = {
            str(step["goal"]): [str(dw) for dw in step.get("done_when", [])]
            for step in steps
        }
        self.agent.run_store.write_task_state(task_state)
        self.agent.emit_trace(
            task_state,
            "plan_checklist_created",
            {"step_count": len(steps), "steps": task_state.checklist},
        )
        return True

    def _replan(self, task_state, user_message, feedback):
        """§7.8.9 阶段 3.5：review redirect 时 replan，更新 checklist。

        保留已完成项 + 已探索摘要（explored_summary），防重复规划。
        规划失败 → 保留原 checklist。
        """
        if not self.agent.feature_enabled("planning"):
            return False
        from .planning import run_planning

        completed = list(getattr(task_state, "completed_items", []) or [])
        explored = "已完成: " + "；".join(completed) if completed else ""
        steps = run_planning(
            self.agent,
            task_state,
            task=user_message,
            context=str(getattr(self.agent, "memory_text", lambda: "")())[:2000],
            explored_summary=explored,
            replan=True,
        )
        if not steps:
            return False
        task_state.checklist = [str(step["goal"]) for step in steps]
        task_state.done_when = [
            str(dw) for step in steps for dw in step.get("done_when", [])
        ]
        task_state.step_done_when = {
            str(step["goal"]): [str(dw) for dw in step.get("done_when", [])]
            for step in steps
        }
        task_state.replan_reasons.append(str(feedback or "")[:200])
        self.agent.run_store.write_task_state(task_state)
        self.agent.emit_trace(
            task_state,
            "plan_replanned",
            {
                "step_count": len(steps),
                "steps": steps,
                "feedback": str(feedback or "")[:300],
                "preserved_completed": completed,
            },
        )
        return True

    def _execute_tool_action(
        self,
        task_state,
        attempts,
        name,
        args,
        tool_call_id,
        recent_tool_fps,
        recent_tool_names,
        started_at,
        *,
        defer_commit=False,
    ):
        """执行单个工具动作（§7.8.9 阶段 2：串行队列的最小单元）。

        批内每个动作独立走完整链路：P4 重复拦截 → 预算拒绝 → 执行 →
        工具信号（窗口）→ evidence 记录。返回 ToolExecutionResult。

        ``defer_commit=True``（并发批）：只执行，跳过窗口/evidence 提交——
        由调用方按声明顺序统一提交（保持重复判定窗口有序）。
        """
        agent = self.agent
        agent.emit_progress(f"step {attempts}: running tool {name}")
        # P4 重复动作执行前拦截：read_file 部分重叠且无未读新行（纯子集/微调）
        # 是同一区域空转,直接拦截;扩展读（含未读新行）放行执行;完全相同区间
        # 仍在执行后比较结果,避免文件在两次读取之间变化时误伤。
        if name == "read_file":
            current_fp = _tool_fingerprint(name, args)
            pre_repeat = any(
                previous_fp[0] == "read_file"
                and _tool_fingerprints_match(current_fp, previous_fp)
                and current_fp[2] != previous_fp[2]
                for previous_fp, _ in tuple(recent_tool_fps)
            )
            if pre_repeat:
                merged_intervals = _merge_read_intervals(
                    [
                        fp[2]
                        for fp, _ in tuple(recent_tool_fps)
                        if fp[0] == "read_file" and fp[1] == current_fp[1]
                    ]
                )
                start, end = current_fp[2]
                if _uncovered_read_lines(start, end, merged_intervals) > 0:
                    pre_repeat = False  # 扩展读：有增量信息,放行
        else:
            pre_repeat = _tool_call_repeats(
                name,
                args,
                None,
                tuple(recent_tool_fps),
            )
        if pre_repeat:
            hint = (
                "该文件区间(或其子集)已在本轮读取,内容在上下文中;"
                "如需未读行,请指定未读的行区间"
                if name == "read_file"
                else "use the existing evidence or try a different action"
            )
            tool_result = ToolExecutionResult(
                content=(
                    f"error: repeated identical call ({name}); {hint}"
                ),
                metadata={
                    "tool_status": "rejected",
                    "tool_error_code": "repeated_identical_call",
                    "read_only": bool(name in _READ_ONLY_TOOL_NAMES),
                    "affected_paths": [],
                },
            )
            task_state.record_malformed_output_recovered()
            agent.emit_trace(
                task_state,
                "tool_rejected_repeat",
                {
                    "name": name,
                    "args": args,
                    "error_code": "repeated_identical_call",
                },
            )
        # §7.8.9 修正（2026-08-19）：移除 read_file 预算硬顶——空转已由重复
        # 动作拦截（含部分重叠区间直接判重复）+ 墙钟/token 硬顶兜底，只读任务
        # 不再需要 4 次读取上限；read_files 仍计数供显示/审计。
        else:
            tool_result = agent.execute_tool(name, args, tool_call_id=tool_call_id)
        if defer_commit:
            # 并发批：执行完成，提交由调用方按声明顺序统一做（保持窗口有序）。
            return tool_result
        return self._commit_tool_action(
            task_state, attempts, name, args, tool_call_id,
            recent_tool_fps, recent_tool_names, started_at, tool_result,
        )

    def _commit_tool_action(
        self,
        task_state,
        attempts,
        name,
        args,
        tool_call_id,
        recent_tool_fps,
        recent_tool_names,
        started_at,
        tool_result,
    ):
        """工具执行的「提交」阶段：重复判定 + 窗口 + read_files + evidence。

        与执行分离：并发批先并发执行（defer_commit），再按声明顺序逐个提交，
        保证重复判定窗口有序、共享状态无竞争。
        """
        agent = self.agent
        task_state.record_affected_paths(tool_result.metadata.get("affected_paths", []))
        tool_status = str(tool_result.metadata.get("tool_status", "unknown"))
        # 工具信号：本轮是否调了工具、是否与窗口内重复（写工具介入豁免）。
        # §7.8.9 修正（2026-08-18）：执行后判定带结果指纹——只读工具
        # 同动作同结果才算重复；内容变了（文件被改）→ 不算。
        result_fp = _result_fingerprint(name, args, tool_result)
        repeated = _tool_call_repeats(
            name,
            args,
            result_fp,
            tuple(recent_tool_fps),
        )
        # §7.8.9 修正（2026-08-18）：read_file 计数移到重复判定后——
        # 内容没变的重复读不计 read_files（不消耗预算）；内容变了的重读
        # 是新读，计入。
        if name == "read_file" and not repeated:
            task_state.record_read_file()
        # §7.8.9 边界：重复动作（P4 拦截）不入重复窗口——入队会污染「有效历史」，
        # 未来相同调用仍应基于最早的原始动作判定重复；窗口只存真实执行过的动作。
        if not repeated:
            recent_tool_fps.append((_tool_fingerprint(name, args), result_fp))
            recent_tool_names.append(name)
        # §7.8.9 修正（2026-08-18）：evidence 只在非重复时记录——重复读（同动作
        # 同结果）不产生新事实，不重复记 evidence（与 read_files 计数一致）。
        if tool_status in {"ok", "partial_success"} and not repeated:
            affected_paths = list(tool_result.metadata.get("affected_paths", []))
            relative_paths = list(affected_paths)
            requested_path = args.get("path") if isinstance(args, dict) else None
            if isinstance(requested_path, str) and requested_path.strip():
                relative_paths.append(requested_path.strip().replace("\\", "/"))
            task_state.record_evidence(
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "status": tool_status,
                    "read_only": bool(tool_result.metadata.get("read_only", False)),
                    "affected_paths": affected_paths,
                    "relative_paths": sorted(set(relative_paths)),
                    "freshness": "current_run",
                    "sensitivity": "workspace",
                    "summary": agent.summarize_tool_result(name, args, tool_result),
                }
            )
        agent.emit_progress(
            f"step {attempts}: tool {name} finished "
            f"({tool_result.metadata.get('tool_status', 'unknown')})"
        )
        result = tool_result.content
        summary = agent.summarize_tool_result(name, args, tool_result)
        task_state.begin_post_tool_reasoning(name)
        agent.record(
            {
                "role": "tool",
                "name": name,
                "args": args,
                "content": result,
                "created_at": now(),
            }
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "tool_executed",
            {
                "name": name,
                "args": args,
                "result": clip(result, 500),
                "summary": summary,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                **dict(tool_result.metadata or {}),
            },
        )
        if agent.allow_checkpoint:
            checkpoint = agent.create_checkpoint(task_state, task_state.user_request, trigger="tool_executed")
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "tool_executed",
                },
            )
        agent.emit_trace(
            task_state,
            "post_tool_reasoning",
            {
                "tool": name,
                "summary": summary,
                "decision": "continue_or_final",
            },
        )
        task_state.finish_post_tool_reasoning("continue")
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "post_tool_reasoning")
        return tool_result, repeated

    def _check_checklist_progress(self, task_state, cmd_cache):
        """§7.8.9 决策（2026-08-18）：checklist 打钩——程序化 done_when 验证。

        每轮工具提交后运行：对未完成 checklist 项的 step_done_when 做
        file:/grep:/cmd: 验证；通过 → complete_item + trace（证据可审计,
        非 LLM 自报）。自由文本 done_when 返回 None → 跳过,由 review 语义打钩。
        """
        agent = self.agent
        completed = set(task_state.completed_items)
        checked = False
        for goal in list(task_state.checklist):
            if goal in completed:
                continue
            for dw in task_state.step_done_when.get(goal, []) or []:
                result = _verify_done_when(agent, dw, cmd_cache)
                if result is True:
                    task_state.complete_item(goal)
                    agent.emit_trace(
                        task_state,
                        "checklist_item_checked",
                        {
                            "goal": str(goal)[:300],
                            "done_when": str(dw)[:300],
                            "method": "programmatic",
                        },
                    )
                    checked = True
                    break
                if result is None:
                    break  # 自由文本 → review 语义打钩,程序不再试
        if checked:
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "checklist_progress")

    def _apply_review_completed_steps(self, task_state, review_decision):
        """§7.8.9 决策（2026-08-18）：review 语义打钩。

        自由文本验收标准经 review 工具验证后,确认的 step（completed_steps）
        写入 completed_items——review_audit 里带 feedback（凭据）,trace 记
        checklist_item_checked(method=review_semantic)。
        """
        steps = review_decision.get("completed_steps") or []
        if not steps:
            return
        checked = False
        for goal in steps:
            goal = str(goal).strip()
            if goal in task_state.checklist and goal not in task_state.completed_items:
                task_state.complete_item(goal)
                checked = True
                self.agent.emit_trace(
                    task_state,
                    "checklist_item_checked",
                    {
                        "goal": goal[:300],
                        "method": "review_semantic",
                        "review_feedback": str(review_decision.get("feedback", ""))[:300],
                    },
                )
        if checked:
            self.agent.run_store.write_task_state(task_state)
            self.agent.emit_agent_state(task_state, "checklist_progress")

    def run(self, user_message, *, task_id=None, run_id=None):
        agent = self.agent
        token = agent.cancellation_token
        hooks = agent.execution_hooks
        agent.child_task_states = []
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(
            run_id=run_id or agent.new_run_id(),
            task_id=task_id or agent.new_task_id(),
            user_request=user_message,
            max_tool_steps=agent.max_steps,
            max_read_files=agent.max_read_files,
            max_total_steps=agent.max_total_steps,
        )
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.emit_progress(f"run {task_state.run_id} started")
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )
        task_state.set_phase(
            PHASE_UNDERSTAND_REQUEST,
            next_step="Gather the minimum workspace context",
            completed_item="Understand the request and acceptance criteria",
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "run_started")
        task_state.set_phase(PHASE_GATHER_CONTEXT, next_step="Inspect the workspace only when evidence is needed")
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "context_requested")

        try:
            # §7.8.9 阶段 3.5：初始 planning（run 开始一次）——生成真实 checklist，
            # 修复「无 planning = checklist 退化」。失败/未开启 → 默认阶段模板。
            self._initial_plan(task_state, user_message)
            return self._run_loop(
                task_state,
                user_message,
                run_started_at=run_started_at,
                token=token,
                hooks=hooks,
            )
        except ProcessCleanupFailed:
            task_state.stop(
                STOP_REASON_PROCESS_CLEANUP_FAILED,
                status=STATUS_FAILED,
                final_answer="agent run failed: shell process cleanup could not be confirmed",
            )
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": task_state.final_answer,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            return task_state.final_answer
        except RunCancelled:
            # 取消收敛：不向调用方抛出，最终回答一律以 TaskState.final_answer 为准。
            task_state.stop_user_cancelled()
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": "",
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            agent.emit_progress(f"run {task_state.run_id} cancelled")
            return ""

    def _run_loop(self, task_state, user_message, *, run_started_at, token, hooks):
        agent = self.agent
        tool_steps = 0
        attempts = 0
        consecutive_talks = 0
        protocol_repairs = 0
        protocol_feedback = ""
        protocol_failed = False
        finalization_attempted = False
        tool_definitions = provider_tool_definitions(agent.tools)
        # §7.8.9 决策（2026-08-18）：去掉 max_steps turn 硬顶——停止由
        # 证据截停（坏轮窗口）/ rejected_finals 收敛 / 墙钟/token 硬顶接管。
        # max_attempts 仅作「模型反复无效输出」的最后保险丝（远大于任务正常
        # 轮数；墙钟 3600s / token 500k 通常更早兜底）。
        max_attempts = MAX_ATTEMPTS_SAFETY
        # §7.8.7-③ 停滞检测（滑动窗口）：每 turn 结束记录三信号
        # （checklist 无新增 / evidence 无新增 / 工具重复或零工具），
        # 窗口内最近 n 个 turn 全坏（n 随剩余预算自适应）→ 强制收敛。
        #
        # 数据流：循环顶部 = 「上一轮执行完后的状态」。
        #   - prev_end = 上上轮结束状态（首次 None，跳过第一轮判定，给开局机会）
        #   - 顶部比较 current(上轮结束) vs prev_end(上上轮结束) → 上轮是否有增量
        #   - 工具分支记录本轮调用 → 下轮顶部判定工具信号
        # 工具重复判定窗口（只读工具指纹 + 工具名序列，供写工具介入豁免）。
        recent_tool_fps: deque = deque(maxlen=TOOL_REPEAT_WINDOW)
        recent_tool_names: deque = deque(maxlen=TOOL_REPEAT_WINDOW)
        prev_end: tuple[int, int] | None = None
        # 上一轮的工具信号（本轮结束时更新，下轮顶部读取）。
        turn_tool_called = False
        turn_tool_repeated = False
        turn_talked = False
        turn_final_rejected = False
        turn_actions = 0
        # §7.8.9 修正（2026-08-18）：连续「无工具 final 被 review 拒」计数，
        # 达 2 触发收敛（纯语言空转最严重，比通用坏轮更激进）。
        consecutive_final_rejected = 0
        # §7.8.9 Review subagent 状态（阶段 3）：程序强制触发。
        # 每 REVIEW_POLL_ACTIONS 个动作触发一次；final 前必确认。
        actions_since_review = 0
        has_write_or_shell = False
        verification_passed = False
        prior_review_feedback = ""
        review_triggered = False
        # §7.8.9 决策（2026-08-18）：双向对抗协议。
        # ① awaiting_review_confirmation：review 同意（finalize/continue）不直接放行，
        #    主循环需显式确认（重提 final）或反驳（继续调工具）——结束必须双方同意。
        # ② review_interval：redirect 跟踪——review 判 redirect 后间隔 6→3 提前复查
        #    （防「说了不听」），复查通过恢复 6；连续 2 次 redirect 未收敛 → 程序强制收敛。
        awaiting_review_confirmation = False
        review_interval = REVIEW_POLL_ACTIONS
        tracking_rejects = 0
        # 双向对抗：review 的同意决策暂存（awaiting 期间），主循环反驳时
        # 用于 main_loop_rebuttal 事件的「against_verdict/feedback」。
        pending_review_decision = None
        # §7.8.9 决策（2026-08-18）：checklist 打钩——cmd: 验收标准的结果缓存
        #（同一命令本 run 只跑一次,防每轮重跑测试烧时间）。
        cmd_cache = {}

        # 边界 1：进入控制循环前。
        token.raise_if_cancelled()

        while attempts < max_attempts and not finalization_attempted:
            # 边界 2：每轮开始、构建 prompt 前。
            token.raise_if_cancelled()
            # §7.8.9 阶段 4：墙钟/token 硬顶（唯一保留的外部尺子，防烧钱）。
            # 超限 → 托底收尾（best-effort 用已收集证据给结论）。
            elapsed_s = int(time.monotonic() - run_started_at)
            tokens_used = task_state.input_tokens + task_state.output_tokens
            cap_hit = elapsed_s >= agent.max_elapsed_seconds or tokens_used >= agent.max_total_tokens
            if cap_hit:
                reason = (
                    "wall_clock_cap"
                    if elapsed_s >= agent.max_elapsed_seconds
                    else "token_budget_cap"
                )
                final = _convergence_summary(self.agent, 
                    task_state, f"运行超时或 token 预算耗尽（{reason}）"
                )
                task_state.stop(
                    STOP_REASON_BUDGET_EXHAUSTED,
                    status=STATUS_STOPPED,
                    final_answer=final,
                )
                task_state.record_error(
                    stage="budget_cap",
                    code=reason,
                    retryable=False,
                    attempts=attempts,
                )
                agent.emit_trace(
                    task_state,
                    "budget_cap_hit",
                    {
                        "reason": reason,
                        "elapsed_seconds": elapsed_s,
                        "max_elapsed_seconds": agent.max_elapsed_seconds,
                        "tokens_used": tokens_used,
                        "max_total_tokens": agent.max_total_tokens,
                    },
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_agent_state(task_state, "run_stopped")
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                return final
            # §7.8.9 坏轮判定（上一轮）：current = 上轮结束状态，prev_end = 上上轮结束。
            # 坏轮 = 完全失联（无 talk 无工具）或 工具动作全失败/全被重复拦截。
            # talk 轮不坏（思考信号，trace + 前端可见）；checklist 退出坏轮判定。
            # §7.8.9 修正（2026-08-18）：删除「工具成功但 evidence 未涨」条件——
            # evidence 只在 ok/partial_success 时记录（record_evidence），成功工具
            # 必涨，该条件实际只兜「失败/被拒」剩余情形，与 tool_repeated_or_failed
            # 语义重叠（冗余）。工具级停滞判定只保留工具信号（层1）；任务级推进
            # 由 checklist 增量（层2）另行判定。
            current = (len(task_state.evidence), len(task_state.completed_items))
            # §7.8.9 决策（2026-08-18）：checklist 打钩——每轮工具提交后跑程序化
            # 验收标准验证（file:/grep:/cmd:）,通过的 step 进 completed_items。
            # 放在坏轮判定前,让本轮打钩反映到 completed_items（停滞审计/进度展示）。
            self._check_checklist_progress(task_state, cmd_cache)
            if prev_end is not None:
                reasons = []
                # §7.8.9 修正（2026-08-18）：final 被 review 拒的轮次不算 silent——
                # 模型有产出（final 内容），只是方向/验证没过，走 review 反馈循环
                # 而非坏轮窗口（否则有工具被拒不进快速收敛，却会被通用坏轮误杀）。
                if not turn_final_rejected and not turn_tool_called and not turn_talked:
                    reasons.append("silent_turn_no_output")
                elif turn_tool_called and turn_tool_repeated:
                    reasons.append("tool_repeated_or_failed")
                is_bad = bool(reasons)
                if is_bad:
                    task_state.stagnation_audit.append(
                        {
                            "turn": attempts,
                            "bad": True,
                            "reasons": reasons,
                            "evidence_count": current[0],
                            "tool_name": task_state.last_tool,
                            "actions": turn_actions,
                        }
                    )
                else:
                    task_state.stagnation_audit.append(
                        {
                            "turn": attempts,
                            "bad": False,
                            "reasons": [],
                            "evidence_count": current[0],
                            "tool_name": task_state.last_tool,
                            "actions": turn_actions,
                        }
                    )
                bad_window = [
                    item.get("bad", False) for item in task_state.stagnation_audit
                ][-STAGNATION_WINDOW:]
                remaining_turns = max(0, agent.max_steps - tool_steps)
                threshold = _stagnation_threshold(remaining_turns)
                if len(bad_window) >= threshold and all(bad_window[-threshold:]):
                    # 证据截停触发：完整审计链（坏轮明细 + 阈值 + 窗口）。
                    task_state.record_malformed_output_recovered()
                    task_state.set_phase(
                        PHASE_ACT_OR_ANSWER,
                        next_step="Converge on a grounded final answer using the collected evidence",
                    )
                    audit_tail = list(task_state.stagnation_audit)[-threshold:]
                    agent.emit_trace(
                        task_state,
                        "stagnation_detected",
                        {
                            "error_code": "stagnation",
                            "threshold": threshold,
                            "window": bad_window,
                            "remaining_turns": remaining_turns,
                            "audit": audit_tail,
                            "reasons_all": [item.get("reasons", []) for item in audit_tail],
                        },
                    )
                    finalization_only = True
                    finalization_attempted = True
                else:
                    # §7.8.9 决策（2026-08-18）：无 max_steps 硬顶——正常路径
                    # 不再因步数进入 finalization；只有证据截停触发它。
                    finalization_only = False
            else:
                # 第一轮：只记录起点，不判定（给模型开局机会）。
                finalization_only = False
            # 本轮结束时的状态 = 下轮顶部的 prev_end 基准。
            prev_end = current
            # §7.8.9 修正（2026-08-18）：「重复无工具 final 被 review 拒」独立计数，
            # 连续 2 轮即触发收敛（比通用坏轮阈值 3 更激进——纯语言空转最严重）。
            # 只对「无工具 final 被拒」计数：有工具被拒 ≠ 空转（可能差最后一步验证，
            # 有产出不该急停），走通用坏轮窗口（阶段 1）+ review 继续给反馈。
            if turn_final_rejected and not turn_tool_called:
                consecutive_final_rejected += 1
            else:
                consecutive_final_rejected = 0
            if consecutive_final_rejected >= 2:
                # §7.8.9 修正（2026-08-18）：连续 2 次无工具 final 被 review 拒
                # → 纯语言空转，直接收敛产出 best-effort（含 rejected_finals 候选），
                # 不再进 finalization 等模型——review 的 redirect 已证明方向无法收敛。
                task_state.record_malformed_output_recovered()
                task_state.set_phase(
                    PHASE_ACT_OR_ANSWER,
                    next_step="Converge on a best-effort answer using collected evidence and rejected candidates",
                )
                audit_tail = list(task_state.stagnation_audit)[-STAGNATION_TOLERANCE:]
                agent.emit_trace(
                    task_state,
                    "stagnation_detected",
                    {
                        "error_code": "final_rejected_stagnation",
                        "threshold": 2,
                        "window": [item.get("bad", False) for item in audit_tail],
                        "remaining_turns": remaining_turns,
                        "audit": audit_tail,
                        "reasons_all": [item.get("reasons", []) for item in audit_tail],
                    },
                )
                final = _convergence_summary(self.agent, 
                    task_state,
                    "模型连续两次提交 final 均被审查驳回（无工具动作），停止空转",
                )
                task_state.stop(
                    STOP_REASON_STEP_LIMIT_REACHED,
                    status=STATUS_STOPPED,
                    final_answer=final,
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_agent_state(task_state, "run_stopped")
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                return final
            # 本轮信号重置，供本轮执行分支设置。
            turn_tool_called = False
            turn_tool_repeated = False
            turn_talked = False
            turn_final_rejected = False
            turn_actions = 0
            attempts += 1
            task_state.record_attempt()
            task_state.set_phase(PHASE_ACT_OR_ANSWER, next_step="Choose a tool or prepare a final answer")
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "model_decision")
            agent.emit_progress(f"step {attempts}: building prompt")
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            if protocol_feedback:
                prompt += (
                    "\n\nRuntime control feedback:\n"
                    f"{protocol_feedback}\n"
                    "This feedback is control-plane input, not an assistant message."
                )
                prompt_metadata["runtime_protocol_feedback"] = True
                prompt_metadata["prompt_chars"] = len(prompt)
            if finalization_only:
                prompt += (
                    "\n\nRuntime control feedback:\n"
                    "Evidence stagnation detected: recent rounds produced no new "
                    "progress. Do not call another tool. Use the evidence already "
                    "collected and return a grounded final answer now."
                )
                prompt_metadata["runtime_finalization_only"] = True
                prompt_metadata["prompt_chars"] = len(prompt)
            agent.emit_progress(f"step {attempts}: prompt ready ({prompt_metadata.get('prompt_chars', len(prompt))} chars)")
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if agent.allow_checkpoint and prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif agent.allow_checkpoint and prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if agent.allow_checkpoint and prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "finalization_only": finalization_only,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            # 边界 3：模型调用前（hook 在 RunGate 内检查取消并发布 model.started）。
            token.raise_if_cancelled()
            hooks.before_model(task_state)
            agent.emit_progress(f"step {attempts}: waiting for model response")
            raw = agent.model_client.complete(
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                on_retry=lambda details: getattr(
                    hooks, "model_retrying", lambda *_args: None
                )(task_state, "execute", details),
                on_text_delta=lambda delta: getattr(
                    hooks, "model_text_delta", lambda *_args: None
                )(task_state, "execute", delta),
                on_thinking_delta=lambda delta: getattr(
                    hooks, "model_thinking_delta", lambda *_args: None
                )(task_state, "execute", delta),
                tool_definitions=() if finalization_only else tool_definitions,
                # §7.8.9 阶段 4 收尾：收尾轮只出最终答案，把 DeepSeek 思考档位
                # 压到 high，防 reasoning 吃光预算导致正文空响应。
                finalization_only=finalization_only,
            )
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            task_state.record_model_usage(completion_metadata)
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            # 边界 4：模型返回后、解析/执行工具前；取消时丢弃迟到响应。
            token.raise_if_cancelled()
            hooks.after_model(task_state, completion_metadata)
            kind, payload = agent.parse(raw)
            if kind == "final" and agent.is_deferred_action_answer(payload):
                kind = "retry"
                payload = agent.retry_notice(
                    "model announced future work instead of performing it"
                )
            response_diagnostics = agent.diagnose_response_shape(raw) if kind == "retry" else {}
            if kind != "retry":
                protocol_feedback = ""
            agent.emit_progress(f"step {attempts}: model returned {kind}")
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                    **response_diagnostics,
                },
            )

            if finalization_only and kind != "final":
                # 收尾轮已经禁用了工具。只读任务的模型有时会返回普通文本，
                # 而不是再次包裹 <final>；此时文本本身就是可展示的终答，不能
                # 因协议外壳缺失把已经收集到的证据丢成 blocked。
                raw_text = str(raw or "").strip()
                action_markers = ("<tool", "<retry", "<talk", '"name"', '"tools"')
                if not has_write_or_shell and raw_text and not any(
                    marker in raw_text for marker in action_markers
                ):
                    kind = "final"
                    payload = raw_text
                    agent.emit_trace(
                        task_state,
                        "finalization_plain_text_accepted",
                        {"tool_steps": tool_steps},
                    )
                else:
                    task_state.record_malformed_output_recovered()
                    agent.emit_trace(
                        task_state,
                        "finalization_protocol_rejected",
                        {
                            "kind": kind,
                            "error_code": "stagnation_finalization",
                            "tool_steps": tool_steps,
                        },
                    )
                    break

            if kind == "talk":
                # §7.8.9：talk 是思考信号（trace + 前端 commentary 可见），
                # 不判坏；连续 talk 由 MAX_CONSECUTIVE_TALKS 兜底。
                turn_talked = True
                if consecutive_talks >= MAX_CONSECUTIVE_TALKS:
                    task_state.record_malformed_output_recovered()
                    task_state.set_phase(
                        PHASE_ACT_OR_ANSWER,
                        next_step="Choose a tool or submit a grounded final answer",
                    )
                    agent.emit_trace(
                        task_state,
                        "talk_rejected",
                        {"error_code": "consecutive_talk_limit", "limit": MAX_CONSECUTIVE_TALKS},
                    )
                else:
                    consecutive_talks += 1
                    task_state.record_talk()
                    hooks.commentary(task_state, str(payload))
                    agent.emit_trace(
                        task_state,
                        "assistant_commentary",
                        {"text": clip(str(payload), 1000), "consecutive": consecutive_talks},
                    )
                    task_state.set_phase(
                        PHASE_ACT_OR_ANSWER,
                        next_step="Continue with a tool or submit a grounded final answer",
                    )
                agent.run_store.write_task_state(task_state)
                agent.emit_agent_state(task_state, "assistant_commentary")
                continue

            if kind == "tool":
                # §7.8.9 决策（2026-08-18）：双向对抗——主循环继续调工具 =
                # 用行动反驳 review 的「可以结束」,结束确认流程（行动即理由）。
                if awaiting_review_confirmation and pending_review_decision is not None:
                    agent.emit_trace(
                        task_state,
                        "main_loop_rebuttal",
                        {
                            "against_verdict": str(pending_review_decision.get("verdict", "")),
                            "action": "tool:" + str(payload.get("name", "")),
                            "feedback": str(pending_review_decision.get("feedback", ""))[:500],
                        },
                    )
                awaiting_review_confirmation = False
                pending_review_decision = None
                consecutive_talks = 0
                tool_steps += 1
                turn_actions += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)
                tool_started_at = time.monotonic()
                tool_call_id = _new_tool_call_id()
                tool_result, repeated = self._execute_tool_action(
                    task_state,
                    attempts,
                    name,
                    args,
                    tool_call_id,
                    recent_tool_fps,
                    recent_tool_names,
                    tool_started_at,
                )
                turn_tool_called = True
                turn_tool_repeated = bool(repeated)
                # §7.8.9 阶段 3：动作统计 + 周期 review 触发。
                actions_since_review += 1
                if name in {"write_file", "patch_file", "run_shell"}:
                    has_write_or_shell = True
                if actions_since_review >= review_interval:
                    review_decision = self._maybe_run_review(
                        task_state,
                        user_message,
                        trigger="action_poll",
                        has_write_or_shell=has_write_or_shell,
                        verification_passed=verification_passed,
                        prior_feedback=prior_review_feedback,
                    )
                    actions_since_review = 0
                    review_triggered = True
                    self._apply_review_completed_steps(task_state, review_decision)
                    if review_decision.get("verdict") == "redirect":
                        prior_review_feedback = str(review_decision.get("feedback", "") or "")
                        protocol_feedback = str(review_decision.get("feedback", "") or "")
                        # §7.8.9 阶段 3.5：review redirect → replan。
                        self._replan(task_state, user_message, str(review_decision.get("feedback", "") or ""))
                        # §7.8.9 决策（2026-08-18）：redirect 跟踪——间隔减半提前复查
                        # （防「说了不听」）；连续 2 次 redirect 未收敛 → 程序强制收敛。
                        review_interval = max(2, review_interval // 2)
                        tracking_rejects += 1
                        if tracking_rejects >= 2:
                            agent.emit_trace(
                                task_state,
                                "review_tracking_converged",
                                {"tracking_rejects": tracking_rejects, "reason": "redirect_not_followed"},
                            )
                            finalization_attempted = True
                            break
                    else:
                        # 方向纠正/有产出 → 恢复常规频率。
                        review_interval = REVIEW_POLL_ACTIONS
                        tracking_rejects = 0
                continue

            if kind == "tool_batch":
                # §7.8.9 阶段 2：声明并行 → 串行队列逐个执行。
                # 批内每个动作独立走完整链路；批内动作按声明顺序入窗口。
                # §7.8.9 决策（2026-08-18）：双向对抗——主循环继续调工具 = 反驳。
                if awaiting_review_confirmation and pending_review_decision is not None:
                    names = [str(item.get("name", "")) for item in (tools or [])]
                    agent.emit_trace(
                        task_state,
                        "main_loop_rebuttal",
                        {
                            "against_verdict": str(pending_review_decision.get("verdict", "")),
                            "action": "tool_batch:" + ",".join(names[:5]),
                            "feedback": str(pending_review_decision.get("feedback", ""))[:500],
                        },
                    )
                awaiting_review_confirmation = False
                pending_review_decision = None
                consecutive_talks = 0
                tools = list(payload.get("tools", []))
                if not tools:
                    task_state.record_malformed_output_recovered()
                    agent.emit_trace(
                        task_state,
                        "tool_batch_rejected",
                        {"error_code": "empty_tool_batch"},
                    )
                    continue
                # §7.8.9 P1 并行数截断：一次 > MAX_PARALLEL_TOOLS 个 → 砍到前 8。
                truncated = False
                if len(tools) > MAX_PARALLEL_TOOLS:
                    tools = tools[:MAX_PARALLEL_TOOLS]
                    truncated = True
                # §7.8.9 P2 批内去重：同批 (name, 归一化args) 重复 → 合并只留首个。
                from .runtime import _normalize_tool_args

                seen = set()
                deduped = []
                deduped_count = 0
                for item in tools:
                    name = str(item.get("name", ""))
                    args = item.get("args", {})
                    key = (
                        name,
                        json.dumps(_normalize_tool_args(name, args), sort_keys=True, ensure_ascii=False),
                    )
                    if key in seen:
                        deduped_count += 1
                        continue
                    seen.add(key)
                    deduped.append(item)
                if deduped_count:
                    tools = deduped
                # §7.8.9 P3 读归并 + P4 读切片（同文件重叠合并、超长切片）。
                tools, merged_reads = _normalize_batch_reads(tools)
                # §7.8.9 P5 分区：并发组（只读、无同路径写冲突）并行执行，
                # 提交按声明顺序统一做；串行组保持原有链路。
                concurrent_items, serial_items = _partition_batch_tools(tools)
                batch_started_at = time.monotonic()
                agent.emit_trace(
                    task_state,
                    "tool_batch_started",
                    {
                        "count": len(tools),
                        "names": [str(item.get("name", "")) for item in tools],
                        "truncated": truncated,
                        "limit": MAX_PARALLEL_TOOLS,
                        "deduped": deduped_count,
                        "merged_reads": merged_reads,
                        "concurrent": len(concurrent_items),
                        "serial": len(serial_items),
                    },
                )
                batch_results = []
                batch_all_bad = True  # 批内所有动作都重复/失败才计本轮坏
                batch_acc = {"all_bad": batch_all_bad, "results": batch_results}

                def _collect_batch_result(acc, name, args, tool_result, repeated):
                    status = str(tool_result.metadata.get("tool_status", "unknown"))
                    if status in {"ok", "partial_success"} and not repeated:
                        acc["all_bad"] = False
                    acc["results"].append(
                        {
                            "name": name,
                            "args": args,
                            "status": status,
                            "summary": agent.summarize_tool_result(name, args, tool_result),
                        }
                    )

                # §7.8.9 P5 执行：并发组 + 串行组都「只执行不提交」（defer_commit），
                # 最后统一按全局声明顺序提交——R7：窗口顺序 = 声明顺序，不能按
                # 组顺序（并发组先行 + 串行组尾巴会让交错的读挤到窗口末尾，
                # 破坏「匹配后有无写工具」豁免判定）。
                executed = {}
                if concurrent_items:
                    def _run_concurrent(index_item):
                        index, item = index_item
                        name = str(item.get("name", ""))
                        args = item.get("args", {})
                        tool_call_id = _new_tool_call_id()
                        tool_started_at = time.monotonic()
                        tool_result = self._execute_tool_action(
                            task_state,
                            attempts,
                            name,
                            args,
                            tool_call_id,
                            recent_tool_fps,
                            recent_tool_names,
                            tool_started_at,
                            defer_commit=True,
                        )
                        return index, name, args, tool_call_id, tool_started_at, tool_result

                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(MAX_PARALLEL_TOOLS, len(concurrent_items))
                    ) as pool:
                        futures = [
                            pool.submit(_run_concurrent, item) for item in concurrent_items
                        ]
                        for future in futures:
                            # 任一并发工具抛异常（RunCancelled/清理失败）→ 上抛，
                            # with 退出时等待其余 future 收尾；整批提交跳过（运行中止）。
                            index, name, args, tool_call_id, tool_started_at, tool_result = future.result()
                            executed[index] = (name, args, tool_call_id, tool_started_at, tool_result)

                # 串行组：按声明顺序逐个执行（同路径依赖保持），但 defer_commit——
                # 提交仍留给下面的统一循环（保证窗口 = 全局声明顺序）。
                for index, item in serial_items:
                    name = str(item.get("name", ""))
                    args = item.get("args", {})
                    tool_call_id = _new_tool_call_id()
                    tool_started_at = time.monotonic()
                    tool_result = self._execute_tool_action(
                        task_state,
                        attempts,
                        name,
                        args,
                        tool_call_id,
                        recent_tool_fps,
                        recent_tool_names,
                        tool_started_at,
                        defer_commit=True,
                    )
                    executed[index] = (name, args, tool_call_id, tool_started_at, tool_result)

                # 统一提交：按全局声明顺序（index 升序），窗口/read_files/evidence
                # 与声明顺序严格一致。
                for index in sorted(executed):
                    name, args, tool_call_id, tool_started_at, tool_result = executed[index]
                    tool_steps += 1
                    turn_actions += 1
                    task_state.record_tool(name)
                    tool_result, repeated = self._commit_tool_action(
                        task_state,
                        attempts,
                        name,
                        args,
                        tool_call_id,
                        recent_tool_fps,
                        recent_tool_names,
                        tool_started_at,
                        tool_result,
                    )
                    _collect_batch_result(batch_acc, name, args, tool_result, repeated)
                batch_all_bad = batch_acc["all_bad"]
                agent.emit_trace(
                    task_state,
                    "tool_batch_completed",
                    {
                        "count": len(batch_results),
                        "duration_ms": int((time.monotonic() - batch_started_at) * 1000),
                        "results": batch_results,
                    },
                )
                turn_tool_called = True
                turn_tool_repeated = batch_all_bad
                # §7.8.9 阶段 3：动作统计 + 周期 review 触发（批内每个动作计一次）。
                for item in tools:
                    actions_since_review += 1
                    iname = str(item.get("name", ""))
                    if iname in {"write_file", "patch_file", "run_shell"}:
                        has_write_or_shell = True
                if actions_since_review >= review_interval:
                    review_decision = self._maybe_run_review(
                        task_state,
                        user_message,
                        trigger="action_poll",
                        has_write_or_shell=has_write_or_shell,
                        verification_passed=verification_passed,
                        prior_feedback=prior_review_feedback,
                    )
                    actions_since_review = 0
                    review_triggered = True
                    self._apply_review_completed_steps(task_state, review_decision)
                    if review_decision.get("verdict") == "redirect":
                        prior_review_feedback = str(review_decision.get("feedback", "") or "")
                        protocol_feedback = str(review_decision.get("feedback", "") or "")
                        # §7.8.9 阶段 3.5：review redirect → replan。
                        self._replan(task_state, user_message, str(review_decision.get("feedback", "") or ""))
                        # §7.8.9 决策（2026-08-18）：redirect 跟踪——间隔减半提前复查
                        # （防「说了不听」）；连续 2 次 redirect 未收敛 → 程序强制收敛。
                        review_interval = max(2, review_interval // 2)
                        tracking_rejects += 1
                        if tracking_rejects >= 2:
                            agent.emit_trace(
                                task_state,
                                "review_tracking_converged",
                                {"tracking_rejects": tracking_rejects, "reason": "redirect_not_followed"},
                            )
                            finalization_attempted = True
                            break
                    else:
                        # 方向纠正/有产出 → 恢复常规频率。
                        review_interval = REVIEW_POLL_ACTIONS
                        tracking_rejects = 0
                continue

            if kind == "retry":
                consecutive_talks = 0
                task_state.record_malformed_output_recovered()
                task_state.set_phase(PHASE_ACT_OR_ANSWER, next_step="Retry with a valid tool call or final answer")
                if protocol_repairs < MAX_PROTOCOL_REPAIRS:
                    protocol_repairs += 1
                    protocol_feedback = str(payload)
                    getattr(agent.execution_hooks, "model_protocol_retrying", lambda *_args: None)(
                        task_state,
                        "execute",
                        {
                            "attempt": protocol_repairs,
                            "max_attempts": MAX_PROTOCOL_REPAIRS + 1,
                            **response_diagnostics,
                        },
                    )
                    agent.run_store.write_task_state(task_state)
                    agent.emit_agent_state(task_state, "malformed_output_recovered")
                    continue
                protocol_failed = True
                agent.emit_trace(
                    task_state,
                    "model_protocol_failed",
                    {
                        "repairs": protocol_repairs,
                        **response_diagnostics,
                    },
                )
                break

            # 边界 8：写最终回答和 durable memory 前。
            token.raise_if_cancelled()
            # §7.8.9 阶段 3：final 前确认——模型想 final 时先过 review。
            # verdict=finalize（且对抗性验证通过）→ 放行；redirect → 注入
            # feedback 继续；continue → 继续。review 由程序强制调用。
            review_decision = self._maybe_run_review(
                task_state,
                user_message,
                trigger="final_before",
                has_write_or_shell=has_write_or_shell,
                verification_passed=verification_passed,
                prior_feedback=prior_review_feedback,
            )
            self._apply_review_completed_steps(task_state, review_decision)
            if review_decision.get("verdict") == "redirect":
                # §7.8.9 修正（2026-08-19）：**无写/shell 动作（只读/纯对话任务）直接
                # 放行**（用户定）——读相关任务的空转由坏轮窗口 + 重叠读取拦截管,
                # review 不需要拦 final（拦了只会反复 reject 死循环收敛 blocked）。
                # 只有写相关任务（write/patch/shell,方向/验证/瞎验收风险高）才走
                # review 拦截。
                if not has_write_or_shell:
                    # 视为「review 同意且主循环已确认」→ 落到 awaiting 检查直接完成。
                    awaiting_review_confirmation = True
                    pending_review_decision = None
                else:
                    # §7.8.9 修正（2026-08-18）：final 被 review 拒 → 内容不作为最终
                    # 输出，存入 rejected_finals（独立于 evidence，供收敛拼 best-effort），
                    # 并置 turn_final_rejected 信号（连续 2 次触发收敛，防纯语言空转）。
                    rejected = str(payload or raw or "").strip()
                    if rejected:
                        task_state.rejected_finals.append(
                            {
                                "status": "final_rejected",
                                "content": rejected[:4000],
                                "feedback": str(review_decision.get("feedback", ""))[:500],
                                "reason": str(review_decision.get("reason", ""))[:200],
                                "created_at": now(),
                            }
                        )
                    turn_final_rejected = True
                    # §7.8.9 决策（2026-08-18）：双向对抗——主循环确认被 review #2 拒，
                    # 结束确认流程（本次 final 被拒,理由入 rejected_finals）。
                    awaiting_review_confirmation = False
                    pending_review_decision = None
                    # 方向/验证有问题：注入 feedback，继续循环（不 final）。
                    prior_review_feedback = str(review_decision.get("feedback", "") or "")
                    task_state.set_phase(
                        PHASE_ACT_OR_ANSWER,
                        next_step="Apply review feedback and converge on a verified final answer",
                    )
                    agent.emit_trace(
                        task_state,
                        "final_rejected_by_review",
                        {
                            "feedback": str(review_decision.get("feedback", ""))[:500],
                            "reason": str(review_decision.get("reason", ""))[:200],
                        },
                    )
                    agent.run_store.write_task_state(task_state)
                    # §7.8.9 阶段 3.5：review redirect → replan（保留已完成项）。
                    self._replan(task_state, user_message, str(review_decision.get("feedback", "") or ""))
                    continue
            # §7.8.9 决策（2026-08-18）：双向对抗协议——review 同意（finalize/continue）
            # 不直接放行。主循环需显式确认（重提 final）或反驳（继续调工具,行动即理由）。
            # review #2 就是下一次 final 前的 review（自然触发）。
            if awaiting_review_confirmation:
                # 主循环已确认（重提 final）→ review #2 判了。redirect 已在上面处理；
                # finalize/continue → review 同意或无异议 → 双方同意,放行。
                awaiting_review_confirmation = False
                pending_review_decision = None
            elif review_decision.get("reason") in {
                "review_subagent_disabled",
                "review_skipped_read_only",
            } or review_decision.get("review_failed"):
                # review 未启用（测试/降级）或模型调用失败——按原语义直接放行,
                # 不强制双向确认（避免 review 故障把 run 卡死）。
                awaiting_review_confirmation = False
            else:
                # 第一次 review 同意 → 不完成,等主循环确认。
                awaiting_review_confirmation = True
                pending_review_decision = dict(review_decision)
                protocol_feedback = (
                    "The review approves completion. Re-submit your final answer to "
                    "confirm, or keep working if you still have work (your next action "
                    "will be treated as a rebuttal and recorded)."
                )
                task_state.set_phase(
                    PHASE_ACT_OR_ANSWER,
                    next_step="Review approves completion; re-submit final to confirm or keep working",
                )
                agent.emit_trace(
                    task_state,
                    "review_awaiting_confirmation",
                    {
                        "verdict": str(review_decision.get("verdict", "")),
                        "feedback": str(review_decision.get("feedback", ""))[:500],
                        "reason": str(review_decision.get("reason", ""))[:200],
                    },
                )
                agent.run_store.write_task_state(task_state)
                continue
            # final 通过双方确认：清零连续被拒计数。
            consecutive_final_rejected = 0
            turn_final_rejected = False
            final = (payload or raw).strip()
            if task_state.requires_post_tool_reasoning:
                # Defensive fallback for custom runtimes that bypass the
                # normal post-tool transition.
                task_state.finish_post_tool_reasoning("final")
            task_state.set_phase(
                PHASE_VERIFY,
                next_step="Verify the collected evidence before returning the final answer",
                completed_item="Gather the minimum workspace context",
            )
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "verify_before_final")
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            agent.run_store.write_task_state(task_state)
            agent.emit_agent_state(task_state, "final")
            if agent.allow_durable_memory_write:
                agent.promote_durable_memory(user_message, final)
            checkpoint = None
            if agent.allow_checkpoint:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.run_store.write_task_state(task_state)
            if checkpoint is not None:
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "run_finished",
                    },
                )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            agent.emit_progress(f"run {task_state.run_id} finished")
            return final

        if protocol_failed or attempts >= max_attempts:
            final = _convergence_summary(self.agent, task_state, "模型反复返回无效输出或达到尝试保险丝（retry_limit_reached）")
            task_state.stop_retry_limit(final)
        else:
            # finalization 轮（证据截停触发）模型未给出有效 final → best-effort。
            final = _convergence_summary(self.agent, task_state, "停滞收敛：连续坏轮后未能产出最终结论")
            task_state.stop_step_limit(final)
        task_state.set_phase(PHASE_FINAL, next_step="Explain the budget or execution blocker")
        agent.run_store.write_task_state(task_state)
        agent.emit_agent_state(task_state, "run_stopped")
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        if agent.allow_durable_memory_write:
            agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        if agent.allow_checkpoint:
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": task_state.stop_reason or "run_stopped",
                },
            )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        agent.emit_progress(f"run {task_state.run_id} stopped: {task_state.stop_reason}")
        return final

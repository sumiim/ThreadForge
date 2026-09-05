"""§7.8.9 Review subagent：程序强制调用的质量收敛门。

与旧 review delegate 的区别：
- 程序强制触发（AgentLoop 每 REVIEW_POLL_ACTIONS 个动作 / evidence 里程碑 / final 前），
  不进 tool_definitions，模型无法绕过；
- 对抗性验证（有罪推定）：finalize 时程序注入「为什么不能结束」理由清单，
  review 必须逐条反驳（附证据）才允许 finalize；
- 收敛不设次数上限（次数上限 = 变相步数）；有证据空转由墙钟/token 终止。
"""

from __future__ import annotations

import json
import time

from ..execution_hooks import RunCancelled
from ..workspace import clip

REVIEW_POLL_ACTIONS = 6          # 每 N 个工具动作触发一次 review
REVIEW_MAX_NEW_TOKENS = 512
# §review 简单双向对抗（2026-09-03）：一轮 final 对抗的 review 次数上限。达到上限后
# review 只建议、主循环终决——主循环重提 final 有权 override review 结束，不再被拦。
REVIEW_BATTLE_MAX_ROUNDS = 2
# §7.8.9 决策（2026-08-18）：review 工具化——review 内部可调 read/search/bash
# 跑验证（如测试/编译），否则 finalize 前无法自证。review 的工具执行走轻量
# 路径（不 hooks/record/commit/审批），结果只回 feed review 上下文，不污染
# 主循环 history/evidence。REVIEW_MAX_STEPS = 内部工具轮 + 判决轮上限。
REVIEW_MAX_STEPS = 5
# §7.8.9 决策（2026-08-18）：方向监督——review 可调 list_files 验证 run trail 里
# 搜索/读取的路径是否存在、目录结构是否正确（纯推断不可靠,工具实证兜底）。
REVIEW_ALLOWED_TOOLS = frozenset({"list_files", "read_file", "search", "run_shell"})

# finalize 判决需要反驳的「不能结束理由」清单（程序从客观状态生成）。
OBSTACLE_VERIFICATION = "verification"      # R1：有写/shell 且验证未过
OBSTACLE_CHECKLIST = "checklist"            # 剩余未完成 checklist 项
OBSTACLE_FAILED = "failed"                  # 存在失败动作未处理
OBSTACLE_PRIOR_FEEDBACK = "prior_feedback"  # 上轮 review 意见未响应

_REVIEW_SYSTEM_TEMPLATE = (
    "You are a review subagent inside ThreadForge, called by the runtime. "
    "You may run read-only tools (read_file / search) and run_shell to verify "
    "the agent's work (e.g. run tests, compile, or inspect files). "
    "Do not modify files other than what verification commands produce.\n"
    "Decide whether the agent's work is complete.\n"
    "This is a control call: emit exactly one JSON object per turn and nothing else.\n"
    '- To run a tool first: {"name": "<tool>", "args": {...}} where <tool> is '
    "read_file, search, or run_shell. The tool result will be appended, then you "
    "can run another tool or emit the verdict.\n"
    '- Final verdict: {"verdict": "finalize" | "continue" | "redirect", '
    '"feedback": "...", "reason": "..."}\n'
    "- finalize: the task is complete; if obstacles are listed, you MUST rebut "
    "each one with concrete evidence from the execution context (e.g. a passed test).\n"
    "- continue: work in progress and on track; no direction change needed.\n"
    "- redirect: the direction/plan is wrong; give actionable feedback the agent "
    "can apply next turn.\n"
    # §7.8.9 决策（2026-08-18）：方向监督——检查 run trail 的探索是否有效
    # （搜索无命中 / 路径不存在 / evidence 不支撑任务）。可疑时用 list_files /
    # read_file / search 验证路径与内容再下结论,并在 feedback 里引用具体观察
    # （不是空口断言）。不做「请求是否涉及工作区」的判定（会误杀纯对话）。
    "- Direction check: examine the run trail for ineffective exploration: searches "
    "with no hits, reads of missing paths, or evidence that does not support the task. "
    "If a path looks wrong, verify it with list_files / read_file / search before "
    "redirecting, and cite the concrete result you observed.\n"
    # §7.8.9 修正（2026-08-18）：移除 grounded 判定——「请求是否涉及工作区」
    # 无法程序精确判定（正则盖不全，误杀纯对话/continuation），且引入
    # review 死循环（你是谁/继续 每轮 redirect）。瞎验收交给模型自判 +
    # 用户可见性（交互式），harness 只做确定性障碍（写证据/验证/收敛兜底）。
    "Never emit prose outside the JSON object.\n"
)

_REVIEW_USER_TEMPLATE = (
    "Request: {request}\n"
    "Acceptance criteria (done_when): {done_when}\n"
    "Run trail (what the agent did this run):\n{run_trail}\n"
    "Checklist remaining: {checklist_remaining}\n"
    "Evidence summary (per action):\n{evidence_summary}\n"
    "Prior review feedback: {prior_feedback}\n"
    "Obstacles that must be rebutted before finalize:\n{obstacles}\n"
    "Semantic acceptance criteria to verify (free-text, cannot be auto-checked; "
    "verify each with tools and include the goals you confirmed in "
    "completed_steps):\n{semantic_criteria}\n"
    # §review 只读软化（2026-09-03）：run 未做任何写/修改（has_write_or_shell=False）
    # 时注入明确指引——评审只判方向与答案质量，不要要求代码修改/补测试/验证通过。
    "{read_only_guidance}"
)

REVIEW_VERDICTS = frozenset({"finalize", "continue", "redirect"})


def build_review_obstacles(task_state, *, has_write_or_shell: bool, verification_passed: bool) -> list[str]:
    """程序生成「为什么不能结束」理由清单（对抗性验证的输入）。

    来源全部是客观状态，不是模型自报：
    - R1 验证：有写/shell 且验证未过 → verification
    - checklist：剩余未完成项（真实 planning checklist）→ checklist
    - 失败动作：坏轮审计里有工具失败/被拒（tool_repeated_or_failed）→ failed。
      §7.8.9 修正（2026-08-18）：不再从 evidence 读——evidence 只在
      ok/partial_success 时记录，永远不含 failed/error，原判定是死代码。
    """
    obstacles = []
    if has_write_or_shell and not verification_passed:
        obstacles.append(OBSTACLE_VERIFICATION)
    # §7.8.9 修正（2026-08-19）：checklist 障碍只对 planning 生成的真实清单生效
    #（explicit）——默认阶段模板（无 planning 的纯对话/直接回答）剩余项不算障碍,
    # 否则「你好」这类纯对话会被 checklist 障碍反复拒死（review 语义打钩无工具可
    # 验证 → checklist 恒剩 → 收敛 blocked）。
    from .review_gate import _checklist_is_explicit

    checklist = list(getattr(task_state, "checklist", []) or [])
    completed = set(getattr(task_state, "completed_items", []) or [])
    remaining = [item for item in checklist if item not in completed]
    if remaining and _checklist_is_explicit(task_state):
        obstacles.append(OBSTACLE_CHECKLIST)
    for item in getattr(task_state, "stagnation_audit", []) or []:
        reasons = item.get("reasons", []) or []
        if "tool_repeated_or_failed" in reasons:
            obstacles.append(OBSTACLE_FAILED)
            break
    return obstacles


def _evidence_summary(task_state, limit: int = 12) -> str:
    """逐动作 evidence 摘要（不走原始结果，控制 review 输入成本）。"""
    evidence = list(getattr(task_state, "evidence", []) or [])
    lines = []
    for item in evidence[-limit:]:
        tool = str(item.get("tool_name", "tool"))
        paths = "、".join(str(p) for p in (item.get("relative_paths") or [])[:3])
        summary = str(item.get("summary", "") or "")[:120]
        status = str(item.get("status", ""))
        lines.append(f"- [{status}] {tool}({paths or 'no path'}): {summary}")
    return "\n".join(lines) if lines else "- (no evidence yet)"


def _parse_review_output(raw: str) -> dict:
    """解析 review 子 agent 输出为结构化判决；格式非法 → redirect + 修复反馈。

    §7.8.9 决策（2026-08-18）：可选 `completed_steps: [goal]`——review 语义
    打钩（自由文本验收标准经工具验证后确认的 step）。程序写入 completed_items
    并记 review_audit（谁打的钩、凭据在 feedback）。
    """
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "verdict": "redirect",
            "feedback": "review output was not valid JSON; please follow the required format",
            "reason": "malformed_review_output",
        }
    if not isinstance(value, dict):
        return {
            "verdict": "redirect",
            "feedback": "review output must be a JSON object",
            "reason": "malformed_review_output",
        }
    verdict = str(value.get("verdict", "")).strip().lower()
    if verdict not in REVIEW_VERDICTS:
        return {
            "verdict": "redirect",
            "feedback": "review verdict must be finalize/continue/redirect",
            "reason": "malformed_review_verdict",
        }
    completed_steps = value.get("completed_steps", [])
    return {
        "verdict": verdict,
        "feedback": str(value.get("feedback", "") or "").strip(),
        "reason": str(value.get("reason", "") or "").strip(),
        "completed_steps": (
            [str(goal).strip() for goal in completed_steps if str(goal).strip()]
            if isinstance(completed_steps, list)
            else []
        ),
    }


def _parse_review_action(raw: str):
    """§7.8.9 决策（2026-08-18）：review 输出可以是判决 JSON 或工具调用。

    返回 ("verdict", decision) | ("tool", name, args) | ("invalid", message)。
    支持 <tool>…</tool> 文本协议与纯 JSON（{name, args} / {verdict, ...}）。
    """
    text = str(raw or "").strip()
    # <tool> 文本协议
    if "<tool" in text:
        import re as _re

        match = _re.search(r"<tool[^>]*>(.*?)</tool>", text, _re.S)
        if match:
            inner = match.group(1).strip()
            try:
                value = json.loads(inner)
                if isinstance(value, dict) and "name" in value:
                    return ("tool", str(value["name"]), value.get("args", {}))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ("invalid", "review output was not valid JSON; emit a tool call or a verdict JSON")
    if not isinstance(value, dict):
        return ("invalid", "review output must be a JSON object")
    if "verdict" in value:
        decision = _parse_review_output(text)
        if decision["verdict"] in REVIEW_VERDICTS:
            return ("verdict", decision)
        return ("invalid", decision.get("feedback", "review verdict must be finalize/continue/redirect"))
    if "name" in value:
        return ("tool", str(value["name"]), value.get("args", {}))
    return ("invalid", "review output must contain a verdict or a tool name")


def _execute_review_tool(agent, name: str, args) -> str:
    """§7.8.9 决策（2026-08-18）：review 内部工具执行（轻量路径）。

    只走校验 + tool run——不触发主循环 hooks / record / commit / 审批，
    结果只回 feed review 上下文（不污染主循环 history/evidence）。review
    是验证者，它的调查不应影响主循环判定。
    """
    if name not in REVIEW_ALLOWED_TOOLS:
        return (
            "error: tool not allowed in review; allowed: "
            + ", ".join(sorted(REVIEW_ALLOWED_TOOLS))
        )
    tool = agent.tools.get(name)
    if tool is None:
        return f"error: unknown tool '{name}'"
    try:
        agent.validate_tool(name, args)
        content = tool["run"](args)
        return clip(str(content), 4000)
    except RunCancelled:
        # §7.8.9 修正（2026-08-19）：取消信号传播,不当工具错误吞掉。
        raise
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def run_review(
    agent,
    task_state,
    *,
    request: str,
    trigger: str,
    has_write_or_shell: bool,
    verification_passed: bool,
    prior_feedback: str = "",
    model_client=None,
) -> dict:
    """程序强制调用 review 子 agent（复用主 model_client 做独立上下文的调用）。

    §7.8.9 决策（2026-08-18）：review 工具化——review 内部循环可调
    read/search/run_shell（跑验证：测试/编译/检查），再给 verdict。
    review 的工具结果只回 feed 自身上下文，不污染主循环 history/evidence；
    工具执行走轻量路径（不 hooks/record/commit/审批）。

    返回结构化判决 + 审计记录（写入 task_state.review_audit）。
    """
    obstacles = build_review_obstacles(
        task_state,
        has_write_or_shell=has_write_or_shell,
        verification_passed=verification_passed,
    )
    # §7.8.9 决策（2026-08-18）：review 内部验证持久化——某次 review 跑过成功的
    # shell（review_verified），后续 review（如双向确认的 review #2）也认可,
    # 否则确认轮没有验证证据会被 R1 gate 误拒。
    verification_passed = verification_passed or bool(
        getattr(task_state, "review_verified", False)
    )
    checklist = list(getattr(task_state, "checklist", []) or [])
    completed = set(getattr(task_state, "completed_items", []) or [])
    remaining_count = max(0, len([item for item in checklist if item not in completed]))
    # §7.8.9 review 判据补全（2026-08-18）：除 evidence 摘要外，注入
    # ① 验收标准（done_when）——review 才知道「做到什么算完成」；
    # ② run trail（session history 的动作印记）——review 才能看到
    # 「这 run 到底干了什么」（含零工具的瞎验收场景）。
    # run_trail 复用 agent.history_text()（已压缩 + 截断 + 有界），
    # 成本可控，不违背上下文隔离原则。
    done_when = list(getattr(task_state, "done_when", []) or [])
    # §7.8.9 决策（2026-08-18）：checklist 语义打钩——提取未完成的自由文本
    # 验收标准（无 file:/grep:/cmd: 前缀,程序无法自动验证）,注入 review 语义验证。
    semantic_criteria = []
    for goal, criteria in dict(getattr(task_state, "step_done_when", {}) or {}).items():
        if goal in set(getattr(task_state, "completed_items", []) or []):
            continue
        for criterion in criteria or []:
            if not str(criterion).startswith(("file:", "grep:", "cmd:")):
                semantic_criteria.append(f"- {goal}: {criterion}")
    try:
        run_trail = agent.history_text()
    except Exception:
        run_trail = ""
    # §review 只读软化（2026-09-03）：run 未做任何写/修改（has_write_or_shell=False，
    # 由 #178 判定：write/patch 恒写，run_shell 仅在真改了文件时才写）→ 明确告诉 review
    # 别要求改代码/补测试/验证，只判方向与答案质量（防「只读诊断却被 review 逼着写」）。
    read_only_guidance = (
        (
            "\n[Read-only run]: This run performed no write or file-modifying actions "
            "(read-only / diagnostic). Judge only direction and answer quality; do NOT "
            "require code changes, added tests, or verification to pass. Do not treat "
            "'not verified' as an obstacle, and do not push the agent toward a write.\n"
        )
        if not has_write_or_shell
        else ""
    )
    context = (
        _REVIEW_SYSTEM_TEMPLATE
        + "\n"
        + _REVIEW_USER_TEMPLATE.format(
            request=str(request)[:2000],
            done_when="; ".join(done_when) if done_when else "(none)",
            run_trail=str(run_trail or "")[:4000],
            checklist_remaining=str(remaining_count),
            evidence_summary=_evidence_summary(task_state),
            prior_feedback=str(prior_feedback or "")[:500],
            obstacles="; ".join(obstacles) if obstacles else "(none)",
            semantic_criteria="\n".join(semantic_criteria) if semantic_criteria else "(none)",
            read_only_guidance=read_only_guidance,
        )
    )
    started_at = time.monotonic()
    decision = None
    review_shell_ok = False
    tool_rounds = 0
    for step in range(REVIEW_MAX_STEPS):
        try:
            # §review 独立模型（2026-09-03）：review 用注入的 model_client（可独立
            # provider/model，用户经卡片选择），否则默认复用主循环 agent.model_client。
            # 独立模型使双向对抗化为不同模型真·互驳，不再同模型 double-check。
            client = model_client or agent.model_client
            raw = client.complete(
                context,
                REVIEW_MAX_NEW_TOKENS,
                on_thinking_delta=lambda delta: getattr(
                    agent.execution_hooks, "model_thinking_delta", lambda *_args: None
                )(task_state, "review", delta),
            )
        except RunCancelled:
            # §7.8.9 修正（2026-08-19）：取消信号必须传播——用户停止/连接断开时
            # review 的模型调用会被取消,若吞成 review_failed 降级 continue,
            # 取消语义被破坏（主循环还会再跑一轮才取消,白白浪费时间）。
            raise
        except Exception as exc:
            decision = {
                "verdict": "continue",
                "feedback": "",
                "reason": f"review model call failed: {type(exc).__name__}",
                # §7.8.9 决策（2026-08-18）：review 调用失败 → 降级放行
                # （AgentLoop 不强制双向确认,避免 run 卡死）。
                "review_failed": True,
            }
            duration_ms = int((time.monotonic() - started_at) * 1000)
            agent.emit_trace(
                task_state,
                "review_failed",
                {
                    "trigger": trigger,
                    "error_type": type(exc).__name__,
                    "duration_ms": duration_ms,
                },
            )
            record = {
                "seq": len(task_state.review_audit) + 1,
                "trigger": trigger,
                "verdict": decision["verdict"],
                "feedback": decision["feedback"],
                "reason": decision["reason"],
                "obstacles": obstacles,
                "duration_ms": duration_ms,
                "failed": True,
            }
            task_state.review_audit.append(record)
            agent.run_store.write_task_state(task_state)
            return decision

        action = _parse_review_action(raw)
        if action[0] == "verdict":
            decision = action[1]
            break
        if action[0] == "tool":
            name, args = action[1], action[2]
            result = _execute_review_tool(agent, name, args)
            tool_rounds += 1
            # review 内部 shell 成功（exit 0）→ 视为验证通过（review 用 bash
            # 就是为了跑验证；finalize 时程序 gate 认可它）。持久化到 task_state,
            # 供后续 review（双向确认轮）复用。
            if name == "run_shell" and "exit_code: 0" in result:
                review_shell_ok = True
                task_state.review_verified = True
            context += (
                f"\n\n[review tool round {tool_rounds}] {name}("
                + json.dumps(args, ensure_ascii=False)[:300]
                + ")\nResult:\n"
                + result
            )
            continue
        # invalid：提示格式，继续给 review 一次机会。
        context += "\n\n" + str(action[1])
    else:
        # 超步未给 verdict → redirect（review 无法收敛成判决）。
        decision = {
            "verdict": "redirect",
            "feedback": "review did not produce a verdict within its step limit",
            "reason": "review_step_limit",
        }
    duration_ms = int((time.monotonic() - started_at) * 1000)

    # 方向 B：程序侧 R1 验证门槛（不依赖模型，纯客观判定）。
    # review 判 finalize 时，若有写/shell 且验证未过 → 程序直接拒绝，
    # 除非 review 内部自己跑过成功的 shell（review 工具化的验证证据）。
    if (
        decision["verdict"] == "finalize"
        and has_write_or_shell
        and not verification_passed
        and not review_shell_ok
    ):
        decision = {
            "verdict": "redirect",
            "feedback": (
                "finalize rejected by runtime gate: this run performed write/shell "
                "actions but no verification (test/compile/syntax check) has passed. "
                "Run a verification action and include its result in evidence before finishing."
            ),
            "reason": "verification_gate_unpassed",
        }
    # 对抗性验证（有罪推定）：finalize 时障碍清单非空且无证据反驳 → 拒 finalize。
    # review 的 feedback 若未逐条引用障碍对应的证据，视为未反驳。
    elif decision["verdict"] == "finalize" and obstacles:
        rebutted = _check_rebuttals(decision.get("feedback", ""), obstacles)
        if not rebutted:
            decision = {
                "verdict": "redirect",
                "feedback": (
                    "finalize rejected: the runtime listed obstacles that must be "
                    "rebutted with evidence before finishing. Obstacles: "
                    + "; ".join(obstacles)
                ),
                "reason": "unrebutted_obstacles",
            }

    agent.emit_trace(
        task_state,
        "review_completed",
        {
            "trigger": trigger,
            "verdict": decision["verdict"],
            "feedback": str(decision.get("feedback", ""))[:500],
            "reason": str(decision.get("reason", ""))[:200],
            "obstacles": obstacles,
            "duration_ms": duration_ms,
            "tool_rounds": tool_rounds,
            "review_shell_ok": review_shell_ok,
        },
    )
    record = {
        "seq": len(task_state.review_audit) + 1,
        "trigger": trigger,
        "verdict": decision["verdict"],
        "feedback": str(decision.get("feedback", ""))[:1000],
        "reason": str(decision.get("reason", ""))[:500],
        "obstacles": obstacles,
        "duration_ms": duration_ms,
        "tool_rounds": tool_rounds,
        "review_shell_ok": review_shell_ok,
        "failed": False,
    }
    task_state.review_audit.append(record)
    agent.run_store.write_task_state(task_state)
    return decision


def _check_rebuttals(feedback: str, obstacles: list[str]) -> bool:
    """检查 review 是否逐条反驳了障碍（有罪推定的证据门槛）。

    简化规则：反馈文本里出现「证据性关键词」（如 test passed / verified /
    complete / evidence / 已通过）即视为尝试反驳。完整实现可要求逐条映射，
    这里先做保守的「提到证据」判定——未提到任何证据关键词 → 未反驳。
    """
    text = str(feedback or "").lower()
    evidence_keywords = ("pass", "verified", "complete", "evidence", "test", "通过", "已验")
    return any(keyword in text for keyword in evidence_keywords)

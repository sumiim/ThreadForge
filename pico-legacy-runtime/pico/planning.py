"""§7.8.9 阶段 3.5：轻量 planning（run 开始一次 + review redirect 时 replan）。

与大取消前 LangGraph 的 planning.py 区别：
- 不依赖 agent-orchestrator / intent（pico 层独立）；
- 轻量输出：只要求步骤列表（goal 字符串），解析成 TaskState.checklist；
- 目标是「checklist 恢复为真实计划」——review 完成判定、停滞窗口、
  预算扩展重新有真实依据，而不是阶段模板退化。
"""

from __future__ import annotations

import json
import re
import time

PLANNING_MAX_NEW_TOKENS = 800
MAX_PLAN_STEPS = 20
MIN_PLAN_STEPS = 1

# §7.8.9 修正（2026-08-19）：纯对话请求（你好/谢谢/ok 等社交短句）跳过 planning——
# 否则 LLM 也会生成 explicit checklist,review 的 checklist 障碍恒真（打钩引擎对
# 纯对话无程序化验收标准、review 语义打钩无工具可验证）→ 纯对话被反复拒死。
PLAIN_CONVERSATION_PATTERN = re.compile(
    r"^(?:"
    r"(?:hi|hello|hey|thanks|thank\s+you|ok|okay|yes|no)"
    r"|(?:你好|您好|嗨|哈喽|在吗|谢谢|谢了|好的|好|收到|再见|拜拜)"
    r")[!！。.?？\s]*$",
    re.IGNORECASE,
)


def is_plain_conversation(task) -> bool:
    """纯社交短句（无需工作区上下文/计划的对话）→ True。"""
    return bool(PLAIN_CONVERSATION_PATTERN.fullmatch(str(task or "").strip()))

# 普通字符串（不用 .format，避免花括号冲突），{payload} 由 replace 填充。
_PLANNING_PROMPT = (
    "You are planning an execution for a local coding agent. "
    "Return exactly one JSON object and nothing else.\n"
    'The JSON must be: {"steps": [{"goal": "step 1 goal", "done_when": ["acceptance"]}, ...]}\n'
    "Requirements:\n"
    f"- steps: a list of {MIN_PLAN_STEPS}-{MAX_PLAN_STEPS} step objects, in execution order.\n"
    "  - goal: a short imperative sentence describing what the step achieves.\n"
    "  - done_when: 1-3 verifiable acceptance criteria for THIS step. Prefer "
    "program-verifiable forms:\n"
    '    - "file:<path>" — the file exists in the workspace (e.g. "file:src/foo.py")\n'
    '    - "grep:<path>:<pattern>" — the file content contains the text/pattern '
    '(e.g. "grep:README.md:installation")\n'
    '    - "cmd:<shell command>" — the command exits 0 (e.g. "cmd:python -m pytest -q")\n'
    "    - otherwise a plain sentence (review will verify it semantically).\n"
    "  A step may have only plain-sentence criteria if its outcome cannot be "
    "expressed programmatically.\n"
    "- Cover only the current request; do not plan duplicate exploration over "
    "already-explored areas.\n"
    "- No markdown, no prose outside the JSON object.\n"
    "PAYLOAD={payload}"
)


def build_planning_prompt(task, context, explored_summary=""):
    payload = json.dumps(
        {
            "task": str(task),
            "recent_context": str(context),
            "explored_summary": str(explored_summary),
        },
        ensure_ascii=False,
    )
    return _PLANNING_PROMPT.replace("PAYLOAD={payload}", f"PAYLOAD={payload}")


def parse_plan_steps(raw):
    """从 planning 输出解析步骤列表；失败抛 ValueError（调用方决定是否降级无计划）。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise ValueError("plan output must be a JSON object")
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("plan output is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
        raise ValueError("plan output must contain a steps array")
    steps = []
    for item in value["steps"]:
        if isinstance(item, dict):
            goal = str(item.get("goal", "") or "").strip()
            raw_done = item.get("done_when", [])
            done_when = (
                [str(dw).strip() for dw in raw_done if str(dw).strip()]
                if isinstance(raw_done, list)
                else []
            )
            if not done_when:
                done_when = [goal]
        else:
            goal = str(item or "").strip()
            done_when = [goal] if goal else []
        if goal:
            steps.append({"goal": goal, "done_when": done_when})
    if not MIN_PLAN_STEPS <= len(steps) <= MAX_PLAN_STEPS:
        raise ValueError(f"plan steps must be {MIN_PLAN_STEPS}-{MAX_PLAN_STEPS}")
    return steps


def run_planning(
    agent,
    task_state,
    *,
    task: str,
    context: str = "",
    explored_summary: str = "",
    replan: bool = False,
) -> list[dict]:
    """调用一次模型生成步骤列表；失败时降级（返回空列表 → 无计划直接跑）。

    §7.8.9 决策（2026-08-18）：返回 list[dict] `{"goal", "done_when"}`——
    done_when 是每步的验收标准（程序化 file:/grep:/cmd: 前缀由打钩引擎验证,
    自由文本由 review 语义打钩）。纯字符串 step 向后兼容 → done_when=[goal]。

    replan=True 时是 review redirect 触发的重新规划。
    """
    prompt = build_planning_prompt(task, context, explored_summary)
    started_at = time.monotonic()
    try:
        raw = agent.model_client.complete(
            prompt,
            PLANNING_MAX_NEW_TOKENS,
            # §7.8.9 决策（2026-08-18）：planning 的思考流式回传（stage=planning），
            # 与每轮 turn 思考（stage=execute）分区展示。
            on_thinking_delta=lambda delta: getattr(
                agent.execution_hooks, "model_thinking_delta", lambda *_args: None
            )(task_state, "planning", delta),
        )
    except Exception:
        agent.emit_trace(
            task_state,
            "planning_failed",
            {"replan": replan, "error_type": "model_call_failed"},
        )
        return []
    try:
        steps = parse_plan_steps(raw)
    except ValueError:
        agent.emit_trace(
            task_state,
            "planning_failed",
            {"replan": replan, "error_type": "parse_failed"},
        )
        return []
    duration_ms = int((time.monotonic() - started_at) * 1000)
    agent.emit_trace(
        task_state,
        "planning_completed",
        {
            "replan": replan,
            "step_count": len(steps),
            "steps": steps,
            "duration_ms": duration_ms,
        },
    )
    return steps

"""§7.8.9 阶段 3.5：轻量 planning（run 开始一次 + review redirect 时 replan）。

与大取消前 LangGraph 的 planning.py 区别：
- 不依赖 agent-orchestrator / intent（pico 层独立）；
- 轻量输出：只要求步骤列表（goal 字符串），解析成 TaskState.checklist；
- 目标是「checklist 恢复为真实计划」——review 完成判定、停滞窗口、
  预算扩展重新有真实依据，而不是阶段模板退化。
"""

from __future__ import annotations

import json
import time

PLANNING_MAX_NEW_TOKENS = 800
MAX_PLAN_STEPS = 20
MIN_PLAN_STEPS = 1

# 普通字符串（不用 .format，避免花括号冲突），{payload} 由 replace 填充。
_PLANNING_PROMPT = (
    "You are planning an execution for a local coding agent. "
    "Return exactly one JSON object and nothing else.\n"
    'The JSON must be: {"steps": ["step 1 goal", "step 2 goal", ...]}\n'
    "Requirements:\n"
    f"- steps: a list of {MIN_PLAN_STEPS}-{MAX_PLAN_STEPS} concise step goals "
    "the agent should complete, in execution order. Each step is a short "
    "imperative sentence.\n"
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
        goal = str(item or "").strip()
        if goal:
            steps.append(goal)
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
) -> list[str]:
    """调用一次模型生成步骤列表；失败时降级（返回空列表 → 无计划直接跑）。

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

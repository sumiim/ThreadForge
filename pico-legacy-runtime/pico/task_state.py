"""一次 ask() 运行过程中的状态机快照。

它回答的是：这次用户请求当前进行到哪了、调了多少次工具、最后为什么停下。
这个对象会被不断写入 task_state.json，供运行中观察和运行后复盘。
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_STEP_LIMIT_REACHED = "step_limit_reached"
STOP_REASON_RETRY_LIMIT_REACHED = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_DELEGATE_FAILED = "delegate_failed"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"
STOP_REASON_REVIEW_RETRY_LIMIT_REACHED = "review_retry_limit_reached"
STOP_REASON_NO_CHANGES_TO_REVIEW = "no_changes_to_review"
STOP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_REASON_RUNTIME_ERROR = "runtime_error"
STOP_REASON_USER_CANCELLED = "user_cancelled"
STOP_REASON_PROCESS_CLEANUP_FAILED = "process_cleanup_failed"
STOP_REASON_SERVICE_RESTARTED = "service_restarted"
STOP_REASON_SERVICE_SHUTDOWN_TIMEOUT = "service_shutdown_timeout"

# Public task phases.  These are deliberately data, not hidden model state, so
# the control plane and UI can explain what the Agent is doing after reconnect.
PHASE_UNDERSTAND_REQUEST = "UNDERSTAND_REQUEST"
PHASE_GATHER_CONTEXT = "GATHER_CONTEXT"
PHASE_ANALYZE_CONTEXT = "ANALYZE_CONTEXT"
PHASE_ACT_OR_ANSWER = "ACT_OR_ANSWER"
PHASE_VERIFY = "VERIFY"
PHASE_FINAL = "FINAL"
TASK_PHASES = (
    PHASE_UNDERSTAND_REQUEST,
    PHASE_GATHER_CONTEXT,
    PHASE_ANALYZE_CONTEXT,
    PHASE_ACT_OR_ANSWER,
    PHASE_VERIFY,
    PHASE_FINAL,
)

DEFAULT_CHECKLIST = (
    "Understand the request and acceptance criteria",
    "Gather the minimum workspace context",
    "Analyze evidence and choose the next action",
    "Act or prepare a grounded answer",
    "Verify the result before finishing",
)


@dataclass
class TaskState:
    run_id: str
    task_id: str
    user_request: str
    status: str = STATUS_RUNNING
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""
    checkpoint_id: str = ""
    resume_status: str = ""
    sandbox_violations: int = 0
    malformed_output_recovered: int = 0
    affected_paths: list[str] = field(default_factory=list)
    phase: str = PHASE_UNDERSTAND_REQUEST
    checklist: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    completed_items: list[str] = field(default_factory=list)
    next_step: str = "Understand the request and acceptance criteria"
    requires_post_tool_reasoning: bool = False
    read_files: int = 0
    max_tool_steps: int = 6
    max_read_files: int = 4
    max_total_steps: int = 18
    plan_id: str = ""
    plan_revision: int = 0
    plan_history: list[dict] = field(default_factory=list)
    replan_reasons: list[str] = field(default_factory=list)
    intent: str = ""
    review_status: str = ""
    budget_converged: bool = False
    talk_steps: int = 0
    evidence: list[dict] = field(default_factory=list)
    # §7.8.9 阶段 3 修正（2026-08-18）：被 review redirect 拒绝的 final 候选答案。
    # 独立于 evidence 存储——塞进 evidence 会让 len(evidence) 计数污染
    # _round_signature（停滞签名误判「有进展」）与坏轮审计。收敛时拼 best-effort
    # 结论用（状态 final_rejected），不进完成门禁判定。
    rejected_finals: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error_stage: str = ""
    error_code: str = ""
    error_retryable: bool = False
    error_attempts: int = 0
    # §7.8.9 证据截停审计：逐轮坏轮判定记录 + 截停时的完整证据链。
    # 每条坏轮记录：{turn, reasons: [...], evidence_count, tool_name}。
    # 截停时：{triggered: true, threshold, window, reasons_all}。
    # 全部写入 task_state.json 与 trace，保证「为什么停」可解释、可复盘。
    stagnation_audit: list[dict] = field(default_factory=list)
    # §7.8.9 Review subagent 审计：每次 review 判决的完整记录。
    # 每条：{seq, trigger, verdict, feedback, reason, 理由清单, 反驳结果, duration_ms}。
    # 与停滞审计并列，共同构成「主循环怎么走、为什么停、review 怎么判」的完整审计链。
    review_audit: list[dict] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        task_id,
        user_request,
        run_id="",
        max_tool_steps=6,
        max_read_files=4,
        max_total_steps=None,
    ):
        if not run_id:
            run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        max_tool_steps = max(1, int(max_tool_steps))
        max_read_files = max(0, int(max_read_files))
        if max_total_steps is None:
            max_total_steps = max(max_tool_steps * 3, max_tool_steps + 4)
        return cls(
            run_id=run_id,
            task_id=task_id,
            user_request=user_request,
            checklist=list(DEFAULT_CHECKLIST),
            done_when=["A final answer is grounded in the collected evidence", "The requested work is verified or its blocker is explicit"],
            max_tool_steps=max_tool_steps,
            max_read_files=max_read_files,
            max_total_steps=max(1, int(max_total_steps)),
        )

    @classmethod
    def from_dict(cls, data):
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            user_request=str(data.get("user_request", "")),
            status=str(data.get("status", STATUS_RUNNING)),
            tool_steps=int(data.get("tool_steps", 0)),
            attempts=int(data.get("attempts", 0)),
            last_tool=str(data.get("last_tool", "")),
            stop_reason=str(data.get("stop_reason", "")),
            final_answer=str(data.get("final_answer", "")),
            checkpoint_id=str(data.get("checkpoint_id", "")),
            resume_status=str(data.get("resume_status", "")),
            sandbox_violations=int(data.get("sandbox_violations", 0)),
            malformed_output_recovered=int(data.get("malformed_output_recovered", 0)),
            affected_paths=[str(path) for path in data.get("affected_paths", [])],
            phase=str(data.get("phase", PHASE_UNDERSTAND_REQUEST)),
            checklist=[str(item) for item in data.get("checklist", DEFAULT_CHECKLIST)],
            done_when=[str(item) for item in data.get("done_when", [])],
            completed_items=[str(item) for item in data.get("completed_items", [])],
            next_step=str(data.get("next_step", "Understand the request and acceptance criteria")),
            requires_post_tool_reasoning=bool(data.get("requires_post_tool_reasoning", False)),
            read_files=int(data.get("read_files", 0)),
            max_tool_steps=max(1, int(data.get("max_tool_steps", data.get("max_steps", 6)))),
            max_read_files=max(0, int(data.get("max_read_files", 4))),
            max_total_steps=max(1, int(data.get("max_total_steps", 18))),
            plan_id=str(data.get("plan_id", "")),
            plan_revision=max(0, int(data.get("plan_revision", 0))),
            plan_history=[
                dict(item) for item in data.get("plan_history", []) if isinstance(item, dict)
            ],
            replan_reasons=[
                str(item) for item in data.get("replan_reasons", []) if str(item).strip()
            ],
            intent=str(data.get("intent", "")),
            review_status=str(data.get("review_status", "")),
            budget_converged=bool(data.get("budget_converged", False)),
            talk_steps=max(0, int(data.get("talk_steps", 0))),
            evidence=[dict(item) for item in data.get("evidence", []) if isinstance(item, dict)],
            rejected_finals=[
                dict(item) for item in data.get("rejected_finals", []) if isinstance(item, dict)
            ],
            input_tokens=max(0, int(data.get("input_tokens", 0) or 0)),
            output_tokens=max(0, int(data.get("output_tokens", 0) or 0)),
            error_stage=str(data.get("error_stage", "")),
            error_code=str(data.get("error_code", "")),
            error_retryable=bool(data.get("error_retryable", False)),
            error_attempts=max(0, int(data.get("error_attempts", 0) or 0)),
            stagnation_audit=[
                dict(item) for item in data.get("stagnation_audit", []) if isinstance(item, dict)
            ],
            review_audit=[
                dict(item) for item in data.get("review_audit", []) if isinstance(item, dict)
            ],
        )

    def set_phase(self, phase, *, next_step="", completed_item=""):
        phase = str(phase or "").strip().upper()
        if phase not in TASK_PHASES:
            raise ValueError(f"unknown task phase: {phase}")
        self.phase = phase
        if completed_item and completed_item not in self.completed_items:
            self.completed_items.append(str(completed_item))
        if next_step:
            self.next_step = str(next_step)
        return self

    def begin_post_tool_reasoning(self, tool_name):
        self.requires_post_tool_reasoning = True
        return self.set_phase(
            PHASE_ANALYZE_CONTEXT,
            next_step=f"Summarize the {tool_name} result and decide whether more evidence is needed",
        )

    def finish_post_tool_reasoning(self, decision="continue"):
        self.requires_post_tool_reasoning = False
        for item in (
            "Gather the minimum workspace context",
            "Analyze evidence and choose the next action",
        ):
            if item not in self.completed_items:
                self.completed_items.append(item)
        if decision == "final":
            return self.set_phase(PHASE_VERIFY, next_step="Verify the evidence before returning the final answer")
        return self.set_phase(PHASE_ACT_OR_ANSWER, next_step="Choose the next tool or prepare the final answer")

    def record_read_file(self):
        self.read_files += 1
        return self

    def record_attempt(self):
        # attempt 统计的是“模型被调用了几轮”，不等于 tool_steps。
        self.attempts += 1
        return self

    def record_tool(self, name):
        # tool_steps 只统计真正进入执行阶段的工具调用次数。
        self.tool_steps += 1
        self.last_tool = str(name or "")
        return self

    def record_sandbox_violation(self):
        self.sandbox_violations += 1
        return self

    def record_malformed_output_recovered(self):
        self.malformed_output_recovered += 1
        return self

    def record_talk(self):
        self.talk_steps += 1
        return self

    def record_model_usage(self, metadata):
        metadata = dict(metadata or {})
        self.input_tokens += max(0, int(metadata.get("input_tokens") or 0))
        self.output_tokens += max(0, int(metadata.get("output_tokens") or 0))
        return self

    def record_error(self, *, stage="", code="", retryable=False, attempts=0):
        self.error_stage = str(stage or "")[:64]
        self.error_code = str(code or "")[:100]
        self.error_retryable = bool(retryable)
        self.error_attempts = max(0, int(attempts or 0))
        return self

    def record_evidence(self, evidence):
        if not isinstance(evidence, dict):
            raise TypeError("evidence must be a dictionary")
        item = dict(evidence)
        item.setdefault("evidence_id", "evidence_" + uuid4().hex)
        item.setdefault("run_id", self.run_id)
        item.setdefault("task_id", self.task_id)
        item.setdefault("created_at", datetime.now().astimezone().isoformat())
        self.evidence.append(item)
        return item

    def complete_item(self, item):
        item = str(item or "").strip()
        if item and item in self.checklist and item not in self.completed_items:
            self.completed_items.append(item)
        return self

    def record_affected_paths(self, paths):
        normalized = {str(path).strip() for path in (paths or []) if str(path).strip()}
        self.affected_paths = sorted(set(self.affected_paths) | normalized)
        return self

    def stop(self, stop_reason, status=STATUS_STOPPED, final_answer=""):
        # stop_reason 和 status 分开存，是为了区分“怎么停的”和“停下时是什么状态”。
        self.status = status
        self.stop_reason = stop_reason
        if final_answer != "":
            self.final_answer = final_answer
        return self

    def stop_step_limit(self, final_answer=""):
        return self.stop(STOP_REASON_STEP_LIMIT_REACHED, final_answer=final_answer)

    def stop_retry_limit(self, final_answer=""):
        return self.stop(STOP_REASON_RETRY_LIMIT_REACHED, final_answer=final_answer)

    def stop_user_cancelled(self, final_answer=""):
        return self.stop(STOP_REASON_USER_CANCELLED, status=STATUS_STOPPED, final_answer=final_answer)

    def stop_model_error(self, final_answer=""):
        return self.stop(STOP_REASON_MODEL_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def finish_success(self, final_answer):
        self.status = STATUS_COMPLETED
        self.stop_reason = STOP_REASON_FINAL_ANSWER_RETURNED
        self.final_answer = str(final_answer)
        self.requires_post_tool_reasoning = False
        self.set_phase(PHASE_FINAL, next_step="Task complete")
        return self

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
            "sandbox_violations": self.sandbox_violations,
            "malformed_output_recovered": self.malformed_output_recovered,
            "affected_paths": list(self.affected_paths),
            "phase": self.phase,
            "checklist": list(self.checklist),
            "done_when": list(self.done_when),
            "completed_items": list(self.completed_items),
            "next_step": self.next_step,
            "requires_post_tool_reasoning": self.requires_post_tool_reasoning,
            "read_files": self.read_files,
            "max_tool_steps": self.max_tool_steps,
            "max_read_files": self.max_read_files,
            "max_total_steps": self.max_total_steps,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_history": [dict(item) for item in self.plan_history],
            "replan_reasons": list(self.replan_reasons),
            "intent": self.intent,
            "review_status": self.review_status,
            "budget_converged": self.budget_converged,
            "talk_steps": self.talk_steps,
            "evidence": [dict(item) for item in self.evidence],
            "rejected_finals": [dict(item) for item in self.rejected_finals],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error_stage": self.error_stage,
            "error_code": self.error_code,
            "error_retryable": self.error_retryable,
            "error_attempts": self.error_attempts,
            "stagnation_audit": [dict(item) for item in self.stagnation_audit],
            "review_audit": [dict(item) for item in self.review_audit],
        }

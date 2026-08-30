"""F5 一致性：未完成任务持久化 final_answer,已完成任务不落 task repo(旧设计)。"""

from threadforge_api.domain.enums import TaskStatus
from threadforge_api.infrastructure.worker_hub import (
    _set_terminal,
    _terminal_persisted_final_answer,
)


class _Task:
    def __init__(self):
        self.status = None
        self.stop_reason = None
        self.final_answer = None
        self.pending_approval = None
        self.error_stage = ""
        self.error_code = ""
        self.error_retryable = False
        self.error_attempts = 0
        self.updated_at = None


def test_completed_run_does_not_persist_final_answer():
    # 已完成任务的答案由会话消息 + message.completed 承载,不重复落 task repo。
    assert _terminal_persisted_final_answer(TaskStatus.COMPLETED, "done") == ""
    task = _Task()
    _set_terminal(task, TaskStatus.COMPLETED, "final_answer_returned", "")
    assert task.final_answer is None


def test_failed_run_persists_final_answer_for_history_consistency():
    # 失败/阻断/中断等未完成任务持久化 final_answer,让刷新后与实时流一致地显示收尾。
    summary = "⚠️ 运行中断：runtime_error。\n\n已收集到部分证据。"
    assert _terminal_persisted_final_answer(TaskStatus.FAILED, summary) == summary
    assert _terminal_persisted_final_answer(TaskStatus.BLOCKED, summary) == summary
    assert _terminal_persisted_final_answer(TaskStatus.CANCELLED, summary) == summary
    assert _terminal_persisted_final_answer(TaskStatus.INTERRUPTED, summary) == summary
    task = _Task()
    _set_terminal(task, TaskStatus.FAILED, "runtime_error", summary)
    assert task.final_answer == summary


def test_empty_failed_final_answer_stays_none_in_repo():
    assert _terminal_persisted_final_answer(TaskStatus.FAILED, "") == ""
    task = _Task()
    _set_terminal(task, TaskStatus.FAILED, "runtime_error", "")
    assert task.final_answer is None

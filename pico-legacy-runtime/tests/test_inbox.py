from langgraph_pico.graph import _drain_inbox
from langgraph_pico.inbox import InboxSource


def test_inbox_source_fifo_pop_wake():
    inbox = InboxSource()
    inbox.append("first")
    inbox.append("second")
    assert inbox.pending() == 2
    assert inbox.pop_wake().message == "first"
    assert inbox.pop_wake().message == "second"
    assert inbox.pop_wake() is None


def test_inbox_source_inject_item_defers_until_wake():
    inbox = InboxSource()
    inbox.append("inject", wake=False)
    assert inbox.has_wake() is False
    assert inbox.pop_wake() is None
    # followup wakes; the inject item stays queued behind it
    inbox.append("followup", wake=True)
    assert inbox.pop_wake().message == "followup"
    assert inbox.pop_wake() is None
    assert inbox.pending() == 1  # inject item still deferred


def test_inbox_source_initial_message_is_wake():
    inbox = InboxSource("hello")
    assert inbox.pop_wake().message == "hello"


def test_drain_inbox_resets_routing_and_sets_continuation():
    inbox = InboxSource()
    inbox.append("new request")
    state = {
        "task": "old task",
        "continuation_context": "",
        "resolved_intent": "read_only",
        "intent_source": "plan",
        "plan": {"plan_id": "p1"},
        "replan_requested": True,
        "replan_reason": "needs_fix",
        "replan_attempts": 1,
        "router_direct_answer": True,
        "requires_research": False,
        "research_result": "old",
        "execution_result": "old",
        "review_status": "needs_fix",
        "review_issues": "x",
        "terminal_reason": "",
    }
    new_state, consumed = _drain_inbox(state, {"inbox": inbox})
    assert consumed is True
    assert new_state["task"] == "new request"
    assert new_state["resolved_intent"] == ""
    assert new_state["plan"] == {}
    assert new_state["replan_attempts"] == 0
    assert new_state["requires_research"] is None
    assert "old task" in new_state["continuation_context"]


def test_drain_inbox_no_wake_item_is_noop():
    state = {"task": "t", "resolved_intent": "read_only"}
    new_state, consumed = _drain_inbox(state, {"inbox": InboxSource()})
    assert consumed is False
    assert new_state is state

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

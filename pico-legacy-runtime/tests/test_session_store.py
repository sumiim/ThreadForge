import json

from pico.session_store import InMemorySessionStore, SessionStore


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    first = {"id": "session_001", "history": [{"role": "user", "content": "first"}]}
    second = {"id": "session_002", "history": [{"role": "user", "content": "second"}]}

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(first_path.read_text(encoding="utf-8"))["id"] == "session_001"
    assert store.load("session_002") == second
    assert store.latest() == second_path.stem


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    assert store.latest() is None


def test_in_memory_session_store_deep_copies_sessions():
    store = InMemorySessionStore()
    session = {"id": "session-1", "history": [{"content": "original"}]}

    path = store.save(session)
    session["history"][0]["content"] = "mutated"
    loaded = store.load("session-1")
    loaded["history"][0]["content"] = "loaded mutation"

    assert path.as_posix() == ".memory-sessions/session-1.json"
    assert store.latest() == "session-1"
    assert store.load("session-1")["history"][0]["content"] == "original"

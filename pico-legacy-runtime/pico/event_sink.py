"""Pluggable sinks for runtime audit events."""

from copy import deepcopy


class EventSink:
    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        raise NotImplementedError


class JsonlSink(EventSink):
    def __init__(self, run_store):
        self.run_store = run_store

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        self.run_store.append_trace(task_state, payload)
        return payload


class NullSink(EventSink):
    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        return payload


class EventCollector(EventSink):
    def __init__(self):
        self._events = []

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        self._events.append(deepcopy(payload))
        return payload

    def snapshot(self):
        return tuple(deepcopy(event) for event in self._events)


class CompositeSink(EventSink):
    def __init__(self, collector, *sinks):
        self.collector = collector
        self.sinks = tuple(sink for sink in sinks if sink is not None)

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        self.collector.emit(task_state, event_type, payload)
        for sink in self.sinks:
            try:
                sink.emit(task_state, event_type, payload)
            except Exception as exc:
                self.collector.emit(
                    task_state,
                    "event_sink_failed",
                    {
                        "event": "event_sink_failed",
                        "created_at": payload.get("created_at"),
                        "source_event": event_type,
                        "sink": type(sink).__name__,
                        "error_type": type(exc).__name__,
                    },
                )
        return payload

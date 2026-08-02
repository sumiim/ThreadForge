"""Execution backends shared by the benchmark harness."""

from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import Protocol

from ..event_sink import CompositeSink, EventCollector, JsonlSink
from ..features import memory as memorylib
from ..run_lifecycle import finalize_failed_run
from ..task_state import STOP_REASON_MODEL_ERROR, STOP_REASON_RUNTIME_ERROR, TaskState


class BackendRunner(Protocol):
    def run_task(self, task, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        """Run one benchmark task and return a backend-neutral result."""
        ...


def default_event_sink_factory(run_store):
    return JsonlSink(run_store)


class ModelBoundaryError(RuntimeError):
    def __init__(self, message, stop_reason=STOP_REASON_MODEL_ERROR):
        super().__init__(message)
        self.stop_reason = stop_reason


class HarnessModelClientAdapter:
    """Classify model failures inside the harness without changing provider clients."""

    def __init__(self, delegate):
        self.delegate = delegate

    def complete(self, *args, **kwargs):
        try:
            return self.delegate.complete(*args, **kwargs)
        except ModelBoundaryError:
            raise
        except Exception as exc:
            raise ModelBoundaryError(f"model call failed: {type(exc).__name__}") from exc

    def __getattr__(self, name):
        return getattr(self.delegate, name)


@dataclass
class BackendRunResult:
    task_state: TaskState
    final_answer: str
    agent: object
    child_task_states: list[TaskState] = field(default_factory=list)
    budget_task_states: list[TaskState] = field(default_factory=list)
    initial_state: dict = field(default_factory=dict)
    events: tuple[dict, ...] = field(default_factory=tuple)
    run_metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.child_task_states = list(self.child_task_states or [])
        if not self.budget_task_states:
            self.budget_task_states = [self.task_state]
        else:
            self.budget_task_states = list(self.budget_task_states)
        self.initial_state = dict(self.initial_state or {})
        self.events = tuple(deepcopy(event) for event in (self.events or ()))
        self.run_metadata = deepcopy(dict(self.run_metadata or {}))


def _start_failed_task_state(agent, task):
    task_state = TaskState.create(
        run_id=agent.new_run_id(),
        task_id=agent.new_task_id(),
        user_request=str(task.get("prompt", "")),
    )
    agent.current_task_state = task_state
    agent.current_run_dir = agent.run_store.start_run(task_state)
    return task_state


class NativeBackendRunner:
    def __init__(self, max_new_tokens=64, model_client_factory=None, event_sink_factory=None):
        self.max_new_tokens = int(max_new_tokens)
        self.model_client_factory = model_client_factory
        self.event_sink_factory = event_sink_factory or default_event_sink_factory

    def run_task(self, task, workspace, session_store, run_store, fixture_copy_root, model_client=None):
        from ..runtime import Pico
        from .evaluator import _apply_task_setup

        if model_client is None and self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        if model_client is None:
            raise ValueError("NativeBackendRunner requires model_client or model_client_factory")

        collector = EventCollector()
        configured_sink = self.event_sink_factory(run_store)
        event_sink = CompositeSink(collector, configured_sink)
        agent = Pico(
            model_client=HarnessModelClientAdapter(model_client),
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            allowed_tools=task["allowed_tools"],
            event_sink=event_sink,
        )
        _apply_task_setup(agent, task, fixture_copy_root)

        memory_state = agent.memory.to_dict()
        initial_state = {
            "initial_history_empty": len(agent.session["history"]) == 0,
            "initial_memory_empty": memorylib.is_effectively_empty(memory_state),
            "initial_task_summary_empty": not str(memory_state["working"]["task_summary"]).strip(),
            "initial_episodic_notes_empty": not memory_state["episodic_notes"],
        }

        final_answer = ""
        started_at = time.monotonic()
        try:
            final_answer = agent.ask(task["prompt"])
        except Exception as exc:
            task_state = agent.current_task_state or _start_failed_task_state(agent, task)
            final_answer = f"harness execution failed: {type(exc).__name__}"
            finalize_failed_run(
                agent,
                task_state,
                error_type=type(exc).__name__,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                stop_reason=getattr(exc, "stop_reason", STOP_REASON_RUNTIME_ERROR),
            )
            final_answer = task_state.final_answer

        task_state = agent.current_task_state
        return BackendRunResult(
            task_state=task_state,
            final_answer=final_answer,
            agent=agent,
            child_task_states=agent.child_task_states,
            budget_task_states=[task_state],
            initial_state=initial_state,
            events=collector.snapshot(),
        )


def build_backend_runner(name, **kwargs):
    if name == "native":
        return NativeBackendRunner(**kwargs)
    if name == "langgraph":
        try:
            from langgraph_pico.backend import LangGraphBackendRunner
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "langgraph backend is optional; install examples/langgraph-pico first"
            ) from exc
        return LangGraphBackendRunner(**kwargs)
    raise ValueError(f"unknown backend: {name}")

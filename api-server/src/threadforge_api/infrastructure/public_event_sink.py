"""pico EventSink that forwards only the public events not already emitted by
the ExecutionBoundary / TaskRunner (per the execution doc §10.5)."""

from __future__ import annotations

from pico.event_sink import EventSink


class PublicEventSink(EventSink):
    def __init__(self, publisher, task_id: str, run_id: str, gate, token):
        self._publisher = publisher
        self._task_id = task_id
        self._run_id = run_id
        self._gate = gate
        self._token = token

    def emit(self, task_state, event_type: str, payload: dict) -> dict:
        if event_type == "agent_state_changed":
            # State projections are intentionally allow-listed and contain no
            # prompt, file contents, tool arguments, or model output.
            with self._gate:
                if self._gate.closed or self._token.is_cancelled():
                    return payload
                self._publisher.publish(
                    self._task_id,
                    self._run_id,
                    "agent.state",
                    {
                        "phase": str(payload.get("phase", ""))[:64],
                        "next_step": str(payload.get("next_step", ""))[:300],
                        "checklist": [str(item)[:300] for item in payload.get("checklist", [])[:20]],
                        "done_when": [str(item)[:300] for item in payload.get("done_when", [])[:20]],
                        "completed_items": [str(item)[:300] for item in payload.get("completed_items", [])[:20]],
                        "tool_steps": max(0, int(payload.get("tool_steps", 0))),
                        "read_files": max(0, int(payload.get("read_files", 0))),
                        "max_tool_steps": max(0, int(payload.get("max_tool_steps", 0))),
                        "max_read_files": max(0, int(payload.get("max_read_files", 0))),
                        "max_total_steps": max(0, int(payload.get("max_total_steps", 0))),
                        "reason": str(payload.get("reason", ""))[:100],
                    },
                )
            return payload
        if event_type == "sandbox_violation":
            with self._gate:
                if self._gate.closed or self._token.is_cancelled():
                    return payload
                self._publisher.publish(
                    self._task_id,
                    self._run_id,
                    "policy.violation",
                    {
                        "tool": payload.get("tool", ""),
                        "tool_error_code": payload.get("tool_error_code", ""),
                        "security_event_type": payload.get("security_event_type", ""),
                    },
                )
        public_type = {
            "plan_created": "plan.created",
            "plan_skipped": "plan.skipped",
            "review_started": "review.started",
            "review_completed": "review.completed",
        }.get(event_type)
        if public_type:
            with self._gate:
                if self._gate.closed or self._token.is_cancelled():
                    return payload
                steps = []
                raw_steps = payload.get("steps", [])
                if isinstance(raw_steps, list):
                    for raw in raw_steps[:20]:
                        if not isinstance(raw, dict):
                            continue
                        dependencies = raw.get("dependencies", [])
                        done_when = raw.get("done_when", [])
                        steps.append(
                            {
                                "id": str(raw.get("id", ""))[:64],
                                "goal": str(raw.get("goal", ""))[:300],
                                "dependencies": [str(item)[:64] for item in dependencies[:20]] if isinstance(dependencies, list) else [],
                                "done_when": [str(item)[:300] for item in done_when[:20]] if isinstance(done_when, list) else [],
                            }
                        )
                self._publisher.publish(
                    self._task_id,
                    self._run_id,
                    public_type,
                    {
                        "plan_id": str(payload.get("plan_id", ""))[:64],
                        "revision": max(0, int(payload.get("revision", 0))),
                        "intent": str(payload.get("intent", ""))[:32],
                        "summary": str(payload.get("summary", ""))[:500],
                        "risk_level": str(payload.get("risk_level", ""))[:16],
                        "step_count": max(0, int(payload.get("step_count", 0))),
                        "steps": steps,
                        "reason": str(payload.get("reason", ""))[:100],
                        "status": str(payload.get("status", ""))[:32],
                        "attempt": max(0, int(payload.get("attempt", 0))),
                        "issue_count": max(0, int(payload.get("issue_count", 0))),
                    },
                )
        # model.started/model.completed/tool.* are published by ExecutionBoundary;
        # task lifecycle by TaskRunner. Nothing else leaks legacy names.
        return payload

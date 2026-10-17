"""One task outcome rule shared by evidence, projection and injection settlement."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

TASK_OUTCOMES = frozenset({"success", "partial", "failure", "recovered"})
NORMAL_STOPS = frozenset({"completed", "success", "ok", "done"})
INCOMPLETE_STOPS = frozenset({"max_iterations", "empty_final_response", "completion_blocked", "cancelled"})
FAILED_STEPS = frozenset({"error", "blocked", "timeout"})


def resolve_task_outcome(
    *, recovery_status: str | None = None,
    steps: Iterable[Mapping[str, Any]] = (), stop_reason: str | None = None,
) -> str | None:
    if recovery_status in {"recovered", "failed"}:
        return "recovered" if recovery_status == "recovered" else "failure"
    if stop_reason in {"error", "tool_error"}:
        return "failure"
    statuses = {str(step.get("status") or "") for step in steps}
    if statuses & FAILED_STEPS:
        return "partial"
    if stop_reason in INCOMPLETE_STOPS:
        return "partial"
    if statuses & {"ok", "success"} and stop_reason in NORMAL_STOPS:
        return "success"
    return None
